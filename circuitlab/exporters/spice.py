"""Translate the compiler's circuit graph into a standalone LTspice netlist."""

import math
import re
from dataclasses import dataclass

from circuitlab.math.analysis import conflicts
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import Circuit, CircuitError, ground_references
from circuitlab.math.equations import solve_known
from circuitlab.math.nonlinear import SWITCH_TRANSITION
from circuitlab.language.syntax import AC_VOLTAGE, BJT_MODEL_NAME, BJT_TYPES, FREQUENCY

OPAMP_MODEL = "EC_IdealOpAmp"
OPAMP_SOURCES = (
    "Eoutput out return out mid 1",
    "Einput mid return plus minus 1",
)


@dataclass(frozen=True)
class SpicePart:
    """A SPICE element name and value shared by both LTspice export formats."""

    name: str
    kind: str
    value: str
    extra: str = ""


@dataclass(frozen=True)
class SpiceCircuit:
    """Validated SPICE names, element values, and analysis command."""

    nodes: dict[str, str]
    parts: dict[str, SpicePart]
    references: tuple[tuple[str, str], ...]
    directive: str
    comments: tuple[str, ...]
    models: tuple[str, ...] = ()


class SpiceNames:
    """Allocate case-insensitive SPICE names without merging distinct objects."""

    def __init__(self, *reserved):
        self.used = {name.casefold() for name in reserved}

    def claim(self, text, prefix=""):
        base = re.sub(r"[^A-Za-z0-9_]", "_", text)
        if prefix and base[:1].upper() != prefix:
            base = f"{prefix}_{base}"
        elif not prefix and not base[:1].isalpha():
            base = f"n_{base}"
        candidate = base
        count = 2
        while candidate.casefold() in self.used:
            candidate = f"{base}_{count}"
            count += 1
        self.used.add(candidate.casefold())
        return candidate


def export_spice(source: str, mode: str = "auto", *, path=None, read_source=None) -> str:
    """Compile editor text and export its resolved connections."""
    circuit = compile_circuit(source, mode, path=path, read_source=read_source)
    if circuit.reduce_groups:
        supply = {"V"} if circuit.mode == "dc" else AC_VOLTAGE | {"phaseV"}
        if circuit.known.keys() - FREQUENCY - supply:
            raise CircuitError(
                "Use an explicit source and components for SPICE export."
            )
        circuit = compile_circuit(source, mode, render_only=True, path=path, read_source=read_source)
    return spice_netlist(circuit)


def spice_netlist(circuit: Circuit) -> str:
    """Serialize the circuit as a standalone text netlist."""
    spice = spice_circuit(circuit)
    lines = ["* Electric Circuits SPICE export", *spice.comments]
    for part in circuit.components:
        model = spice.parts[part.name]
        p, n = spice.nodes[part.p], spice.nodes[part.n]
        if model.kind in {"X", "S"}:
            plus, minus = spice.nodes[part.in_plus], spice.nodes[part.in_minus]
            lines.append(f"{model.name} {p} {n} {plus} {minus} {model.value}")
        elif model.kind == "Q":
            base = spice.nodes[part.base]
            lines.append(f"{model.name} {p} {base} {n} {model.value}")
        else:
            dc = "DC " if model.kind in "VI" and part.pulse is None else ""
            lines.append(f"{model.name} {p} {n} {dc}{model.value}{model.extra}")
    lines.extend(f"{name} {node} 0 DC 0" for name, node in spice.references)
    if any(part.kind == "O" for part in circuit.components):
        lines.extend(("", *opamp_subcircuit("out return plus minus")))
    if spice.models:
        lines.extend(("", *spice.models))
    return "\n".join([*lines, "", spice.directive, ".end", ""])


def opamp_subcircuit(pins):
    # The two VCVSs enforce plus = minus without drawing input current
    # or fixing the output voltage. Additional symbol pins may be unused.
    return (f".subckt {OPAMP_MODEL} {pins}", *OPAMP_SOURCES, f".ends {OPAMP_MODEL}")


def _bjt_models(parts):
    named, defaults = {}, {}
    for part in parts:
        if part.kind != "Q":
            continue
        if part.transistor_type not in BJT_TYPES:
            raise CircuitError(f"{part.name} needs BJT type NPN or PNP for export.")
        if not part.model:
            continue
        if not BJT_MODEL_NAME.fullmatch(part.model):
            raise CircuitError(f"{part.name} needs a valid SPICE model name.")
        key = part.model.casefold()
        polarity = part.transistor_type.upper()
        _, existing = named.setdefault(key, (part.model, polarity))
        if existing != polarity:
            raise CircuitError(
                f"SPICE model {part.model} is used for both NPN and PNP transistors."
            )
    names = SpiceNames(*named)
    models = {}
    for part in parts:
        if part.kind != "Q":
            continue
        polarity = part.transistor_type.upper()
        if not part.model and polarity not in defaults:
            defaults[polarity] = names.claim(f"EC_BJT_{polarity}")
        models[part.name] = part.model or defaults[polarity]
    definitions = tuple(
        f".model {name} {polarity}" for polarity, name in defaults.items()
    )
    comments = [
        f"* Add a .model or .include for {name} ({polarity})."
        for name, polarity in named.values()
    ]
    return models, definitions, comments


def spice_circuit(circuit: Circuit) -> SpiceCircuit:
    """Normalize values and names before writing a netlist or schematic."""
    circuit.require_values()
    # Both flat and hierarchical LTspice drawings use this same full-graph
    # validation. A constant AC impedance has no general physical SPICE model.
    for part in circuit.components:
        if part.kind == "Z":
            raise CircuitError(
                f"{part.label or part.name} is a theoretical AC impedance (Z). "
                "SPICE/LTspice export requires an explicit circuit model; "
                "replace it with R/L/C or other modeled components.",
                circuit.component_lines.get(part.name), "UnsupportedImpedanceExport",
                path=circuit.component_files.get(part.name),
            )
    circuit.require_models()
    if not circuit.components or circuit.mode == "transient":
        raise CircuitError("SPICE export requires a DC or AC circuit with components.")
    parts = circuit.components
    if len({p.name.casefold() for p in parts}) != len(parts):
        raise CircuitError("Give each component a unique name before exporting.")
    if circuit.known.keys() - (FREQUENCY if circuit.mode != "dc" else set()):
        raise CircuitError("Use only frequency quantities alongside a circuit list.")

    models, definitions, lines = _bjt_models(parts)
    frequency = None
    pulses = [part.pulse for part in parts if part.pulse is not None]
    directive = ".op"
    if pulses:
        stop = max(pulse.delay + 2 * pulse.period for pulse in pulses)
        step = min(pulse.period for pulse in pulses) / 100
        directive = f".tran 0 {_number(stop)} 0 {_number(step)} uic"
    if circuit.mode != "dc":
        values, notes, _ = solve_known("ac", circuit.known)
        if conflicts(notes):
            raise CircuitError(
                "Resolve the conflicting frequency quantities before exporting."
            )
        frequency = values.get("f")
        if frequency is None:
            if circuit.mode == "mixed" or any(part.kind in "CLXQS" for part in parts):
                raise CircuitError(
                    "Enter a frequency (f, T, or w) for SPICE AC export."
                )
            frequency = 1
            lines.append("* Frequency-independent circuit exported at 1 Hz.")
        if frequency <= 0:
            raise CircuitError("AC frequency must be positive.")
        directive = f".ac list {_number(frequency)}"
        lines.append("* AC source amplitudes use the circuit's RMS phasors.")
        if circuit.mode == "mixed":
            lines.append(
                "* DC offsets are included. Use .op in place of .ac for DC results."
            )

    node_names = SpiceNames("0", "gnd")
    nodes = {"0": "0"}
    for node in circuit.nodes:
        if node == "0":
            continue
        nodes[node] = node_names.claim(node)
        if nodes[node] != node:
            lines.append(f"* Node {ascii(node)} = {nodes[node]}")
    for tap in circuit.wire_taps:
        if tap.name != tap.net:
            lines.append(f"* Wire alias {ascii(tap.name)} = {nodes[tap.net]}")

    branches = [
        part.terminal_nets if part.kind in {"Q", "S"} else (part.p, part.n)
        for part in parts
    ]
    references = ground_references(circuit.grounds, branches)
    if not references:
        raise CircuitError("Connect the circuit to ground before exporting.")
    names = SpiceNames()
    elements = {}
    switches = {}
    model_names = SpiceNames(*models.values())
    for part in parts:
        p, n = nodes[part.p], nodes[part.n]
        if p == n and part.kind not in {"Q", "S"}:
            raise CircuitError(f"{part.name} needs two distinct nodes.")
        kind, value = part.kind, part.value
        if kind == "X":
            # A fixed reactance is equivalent to this L or C at the export frequency.
            if frequency is None or value.imag or not value.real:
                raise CircuitError(f"{part.name} needs a nonzero real reactance in AC.")
            reactance = value.real
            kind = "L" if reactance > 0 else "C"
            value = (
                (reactance if reactance > 0 else -1 / reactance) / frequency / math.tau
            )
        extra = ""
        if kind in {"R", "L", "C"}:
            if value.imag or value.real <= 0:
                raise CircuitError(f"{part.name} requires a positive real value.")
            spec = _number(value)
            if kind == "L":
                extra = " Rser=0"
        elif kind in {"V", "I"}:
            dc = part.value if circuit.mode == "dc" else part.dc
            spec = part.pulse.spice() if part.pulse is not None else _number(dc)
            if frequency is not None:
                phase = math.degrees(math.atan2(value.imag, value.real)) if value else 0
                amplitude = math.hypot(value.real, value.imag)
                extra = f" AC {_number(amplitude)} {_number(phase)}"
        elif kind == "O":
            kind = "X"
            spec = OPAMP_MODEL
        elif kind == "Q":
            spec = models[part.name]
        elif kind == "S":
            key = part.value, part.off_resistance, part.threshold
            if key not in switches:
                switches[key] = model_names.claim(f"EC_SW{len(switches) + 1}")
            spec = switches[key]
        else:
            raise CircuitError(f"SPICE export does not support component type {kind}.")
        label = part.label or part.name
        preferred = label if label[:1].upper() == kind else part.name
        name = names.claim(preferred, kind)
        if name != label:
            lines.append(f"* Component {ascii(label)} = {name}")
        if part.kind == "X":
            lines.append(
                f"* {name} matches {_number(part.value)} ohms at {_number(frequency)} Hz."
            )
        elements[part.name] = SpicePart(name, kind, spec, extra)

    # Reference each disconnected domain as the native solver does.
    references = tuple(
        (names.claim("V_EC_ref", "V"), nodes[node])
        for node in sorted(set(references.values()) - {"0"})
    )
    definitions += tuple(
        f".model {name} SW(Ron={_number(on)} Roff={_number(off)} "
        f"Vt={_number(threshold)} Vh=-{SWITCH_TRANSITION})"
        for (on, off, threshold), name in switches.items()
    )
    return SpiceCircuit(nodes, elements, references, directive, tuple(lines), definitions)


def _number(value):
    if value.imag or not math.isfinite(value.real):
        raise CircuitError("SPICE export requires finite real component values.")
    return repr(float(value.real or 0)).removesuffix(".0")
