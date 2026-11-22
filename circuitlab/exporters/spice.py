"""Translate the compiler's circuit graph into a standalone LTspice netlist."""
# Validate the complete electrical graph before serializing device names or models.
# Name allocation is case-insensitive for SPICE, so distinct OOC identities must be renamed
# without merging their nets. Physical export models may differ from display-only packages.

import math
import re
from dataclasses import dataclass, field, replace

from circuitlab.math.analysis import conflicts
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import AudioAmpModel, Circuit, CircuitError, OpAmpModel, ground_references
from circuitlab.model.potentiometer import expand_potentiometers
from circuitlab.model.diode import DiodeModel
from circuitlab.exporters.diodes import diode_subcircuit
from circuitlab.math.equations import solve_known
from circuitlab.math.nonlinear import SWITCH_TRANSITION
from circuitlab.math.opamps import common_inverse
from circuitlab.language.syntax import AC_VOLTAGE, BJT_MODEL_NAME, BJT_TYPES, FREQUENCY

OPAMP_MODEL = "EC_IdealOpAmp"
POWERED_OPAMP_MODEL = "EC_IdealOpAmpPowered"
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
    potentiometers: dict = field(default_factory=dict)
    behavior_lines: tuple[str, ...] = ()


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
    parts = (leg for part in circuit.components
             for leg in spice.potentiometers.get(part.name, (part,)) if leg is not None)
    for part in parts:
        model = spice.parts[part.name]
        p, n = spice.nodes[part.p], spice.nodes[part.n]
        if part.kind == "A":
            pins = " ".join(spice.nodes[net] for net in part.terminal_nets)
            lines.append(f"{model.name} {pins} {model.value}")
        elif model.kind in {"X", "S"}:
            plus, minus = spice.nodes[part.in_plus], spice.nodes[part.in_minus]
            rails = (f" {spice.nodes[part.vplus]} {spice.nodes[part.vminus]}"
                     if part.kind == "O" and part.vplus else "")
            lines.append(f"{model.name} {p} {n} {plus} {minus}{rails} {model.value}")
        elif model.kind == "Q":
            base = spice.nodes[part.base]
            lines.append(f"{model.name} {p} {base} {n} {model.value}")
        else:
            dc = "DC " if model.kind in "VI" and part.pulse is None else ""
            lines.append(f"{model.name} {p} {n} {dc}{model.value}{model.extra}")
    lines.extend(f"{name} {node} 0 DC 0" for name, node in spice.references)
    lines.extend(spice.behavior_lines)
    if any(part.value == OPAMP_MODEL for part in spice.parts.values()):
        lines.extend(("", *opamp_subcircuit("out return plus minus")))
    if any(part.value == POWERED_OPAMP_MODEL for part in spice.parts.values()):
        lines.extend(("", *opamp_subcircuit(
            "out return plus minus vplus vminus", model=POWERED_OPAMP_MODEL,
        )))
    if spice.models:
        lines.extend(("", *spice.models))
    return "\n".join([*lines, "", spice.directive, ".end", ""])


# The ideal subcircuit enforces equal input voltage with zero input current.
# Optional supply terminals remain explicit identifiers without adding rail clipping.
def opamp_subcircuit(pins, *, model=OPAMP_MODEL, reference="return"):
    # The two VCVSs enforce plus = minus without drawing input current
    # or fixing the output voltage. Additional symbol pins may be unused.
    sources = tuple(source.replace("return", reference) for source in OPAMP_SOURCES)
    return (f".subckt {model} {pins}", *sources, f".ends {model}")


def behavioral_opamp_subcircuit(name, model, powered):
    """One-pole/slew macromodel plus an adapter for LTspice's five-pin symbol.

    The internal 1 pF capacitor is mathematical state, not output capacitance.
    A slew-only override uses a 1e15 Hz pole for the otherwise unlimited GBW
    in SPICE; native time simulation handles the ideal-bandwidth limit exactly.
    """
    rails = " vplus vminus" if powered else ""
    lines = [f".subckt {name} out return plus minus{rails}"]
    offset = _number(model.offset)
    signal = "V(plus,minus)"
    if common_inverse(model):
        signal = f"({signal}+{_number(common_inverse(model))}*(V(plus,return)+V(minus,return))/2)"
    limited = model.output_min is not None or model.output_max is not None

    def bound(limit):
        terms = [_number(limit.constant)]
        terms.extend(f"{_number(coefficient)}*V({rail},return)"
                     for rail, coefficient in (("vplus", limit.vplus), ("vminus", limit.vminus)) if coefficient)
        return "(" + "+".join(terms) + ")"

    def clipped(expression):
        if model.output_min is not None and model.output_max is not None:
            return f"limit({expression},{bound(model.output_min)},{bound(model.output_max)})"
        if model.output_min is not None:
            return f"max({expression},{bound(model.output_min)})"
        return f"min({expression},{bound(model.output_max)})"

    if limited:
        if model.bandwidth is not None or model.slew is not None:
            omega = _number(math.tau * (model.bandwidth or 1e15))
            rate = f"{omega}*({signal}+({offset})-{_number(model.inverse_gain)}*V(out,return))"
            if model.slew is not None:
                slew = _number(model.slew)
                rate = f"limit({rate},-{slew},{slew})"
            # The derivative acts on the actual output, so saturation does
            # not leave an unbounded hidden pole state behind the clamp.
            projection = f"V(out,return)+({rate})-ddt(V(out,return))"
        elif model.gain is not None:
            projection = f"{_number(model.gain)}*({signal}+({offset}))"
        else:
            # This implicit voltage equation is the exact ideal-gain limit:
            # interior operation enforces zero input error; at a rail the
            # error may only point farther into saturation.
            projection = f"V(out,return)+{signal}+({offset})"
        lines.append(f"Boutput out return V={clipped(projection)}")
    elif model.bandwidth is not None or model.slew is not None:
        omega = _number(math.tau * (model.bandwidth or 1e15))
        drive = f"{omega}*({signal}+({offset})-{_number(model.inverse_gain)}*V(state,return))"
        if model.slew is not None:
            slew = _number(model.slew)
            drive = f"limit({drive},-{slew},{slew})"
        lines.extend(("Cstate state return 1p",
                      f"Bdrive return state I=1e-12*({drive})",
                      "Eoutput out return state return 1"))
    elif model.gain is not None:
        lines.append(f"Boutput out return V={_number(model.gain)}*({signal}+({offset}))")
    else:
        sensed = (f"Binput sensed return V=V(minus,return)+{signal}+({offset})"
                  if common_inverse(model) else f"Voffset sensed plus {offset}")
        lines.extend((sensed,
                      "Eoutput out return out mid 1", "Einput mid return sensed minus 1"))
    if model.common_impedance is not None:
        lines.extend((f"Rcommon_plus plus return {_number(model.common_impedance)}",
                      f"Rcommon_minus minus return {_number(model.common_impedance)}"))
    for property_name, limit in (("common.min", model.common_min), ("common.max", model.common_max)):
        if limit is not None:
            # These specify the model's validity region, not a clipping law.
            lines.append(f"* {property_name} = {_number(limit.constant)} + {_number(limit.vplus)}*V(vplus,return) "
                         f"+ {_number(limit.vminus)}*V(vminus,return); checked by native DC/time simulation")
    if powered and model.supply_current:
        lines.append(f"Iquiescent vplus vminus DC {_number(model.supply_current)}")
    lines.append(f".ends {name}")
    reference = "0" if powered else "vminus"
    lines.extend((f".subckt {name}_ASC plus minus vplus vminus out",
                  f"Xcore out {reference} plus minus{rails} {name}", f".ends {name}_ASC"))
    return tuple(lines)


def audio_subcircuit(name, model, gain_return=True):
    """Use the same gain feedback and bypass network as the native model."""
    gm = (model.gain_max / model.gain_default - 1) / model.gain_max / model.gain_resistor
    return (
        f".subckt {name} plus minus out vplus vminus gain1 gain8 bypass",
        f"Bbias bias vminus V={_number(model.output_bias)}*V(vplus,vminus)",
        f"Rbypass bias bypass {_number(model.bypass_resistance)}",
        *(("Vgain_return gain8 vminus 0",) if gain_return else ()),
        f"Rgain gain1 gain8 {_number(model.gain_resistor)}",
        f"Gfeedback gain8 gain1 out bypass {_number(gm)}",
        f"Boutput out vminus V=V(bypass,vminus)+{_number(model.gain_max)}*(V(plus,minus)-V(gain1,gain8))",
        f"Isupply vplus vminus DC {_number(model.supply_current)}",
        f".ends {name}",
    )


def math_source_lines(equations, nodes, names=None):
    """Serialize hidden equations as directives, never as drawing symbols."""
    names = names or SpiceNames()

    def boolean(tree):
        return (tree[0] in {"bit", "compare", "bool"} or tree[0] == "c" and tree[1] in {0, 1}
                or tree[0] == "unary" and tree[1] == "!"
                or tree[0] == "binary" and tree[1] in {"&", "|", "^"}
                and boolean(tree[2]) and boolean(tree[3]))

    def expression(tree):
        kind = tree[0]
        if kind == "state":
            raise CircuitError("Stateful analog equations require native time simulation; SPICE export of their history semantics is not implemented.",
                               code="UnsupportedStatefulExport")
        if kind == "c":
            return _number(tree[1])
        if kind == "time":
            return "time"
        if kind in {"v", "bit"}:
            value = f"V({nodes[tree[1]]},{nodes[tree[2]]})"
            return f"if({value}>=0.5,1,0)" if kind == "bit" else value
        if kind == "unary":
            value = expression(tree[2])
            return f"(-1-int({value}))" if tree[1] == "~" else f"({tree[1]}({value}))"
        if kind == "binary":
            op, a, b = tree[1], expression(tree[2]), expression(tree[3])
            if op in {"&", "|", "^"}:
                if not (boolean(tree[2]) and boolean(tree[3])):
                    raise CircuitError("SPICE supports Boolean bitwise math here; export wider integer math to Verilog.")
                check = f"(({a})!=0){'!=' if op == '^' else op}(({b})!=0)"
                return f"if({check},1,0)"
            if op in {"<<", ">>"}:
                raise CircuitError("Export integer shift behavior to Verilog.")
            if op == "%":
                return f"(int({a})-int({b})*floor(int({a})/int({b})))"
            if op == "//":
                return f"floor(int({a})/int({b}))"
            return f"(({a}){op}({b}))"
        if kind == "compare":
            values = [expression(node) for node in tree[2:]]
            checks = [f"(({a}){op}({b}))" for a, op, b in zip(values, tree[1], values[1:])]
            return f"if({'&'.join(checks)},1,0)"
        if kind == "bool":
            op = "&" if tree[1] == "and" else "|"
            return "if(" + op.join(f"(({expression(node)})!=0)" for node in tree[2:]) + ",1,0)"
        function = {"log": "ln", "clamp": "limit", "ifelse": "if"}.get(tree[1], tree[1])
        args = [expression(node) for node in tree[2:]]
        if function == "square":
            phase,duty = args
            return f"if(({phase})-floor({phase})<({duty}),1,0)"
        if function == "if":
            args[0] = f"(({args[0]})!=0)"
        if function in {"min", "max"}:
            result = args[0]
            for value in args[1:]:
                result = f"{function}({result},{value})"
            return result
        return f"{function}({','.join(args)})"

    lines = []
    for equation in equations:
        try:
            rendered = expression(equation.tree)
        except CircuitError as error:
            raise CircuitError(error.message, equation.line, code=error.code, path=equation.path) from None
        name = names.claim(equation.name, "B")
        lines.append(f"{name} {nodes[equation.output]} {nodes[equation.reference]} V={rendered}")
    return tuple(lines)


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


# Validate models and values across the full graph before allocating SPICE names.
# This prevents an unsupported part inside a collapsed package from disappearing on export.
def spice_circuit(circuit: Circuit) -> SpiceCircuit:
    """Normalize values and names before writing a netlist or schematic."""
    if circuit.comparators:
        raise CircuitError("Configured comparator propagation models require native DC/time simulation; exporting their timing is not implemented.",
                           code="UnsupportedComparatorExport")
    circuit.require_values()
    components, indices = expand_potentiometers(circuit)
    potentiometers = {
        name: tuple(components[index] if index is not None else None for index in legs)
        for name, legs in indices.items()
    }
    circuit = replace(circuit, components=components)
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
    if not (circuit.components or circuit.behavior) or circuit.mode == "transient":
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
            if circuit.mode == "mixed" or any(
                part.kind in "CLXQS" or (part.kind == "O" and part.opamp and part.opamp.bandwidth)
                for part in parts
            ):
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
    # Explicit op-amp power contacts are visual connections in the ideal
    # model. Keep their names even when they are not part of the solver graph.
    power_nodes = tuple(net for part in parts if part.kind == "O" and part.vplus
                        for net in (part.vplus, part.vminus))
    for node in dict.fromkeys((*circuit.nodes, *power_nodes)):
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
    branches.extend((part.vplus, part.vminus) for part in parts
                    if part.kind == "A" or (part.kind == "O" and part.opamp and part.opamp.supply_current))
    branches.extend((eq.output, eq.reference) for eq in circuit.behavior)
    references = ground_references(circuit.grounds, branches)
    if not references:
        raise CircuitError("Connect the circuit to ground before exporting.")
    names = SpiceNames()
    elements = {}
    switches = {}
    model_names = SpiceNames(*models.values(), OPAMP_MODEL, POWERED_OPAMP_MODEL)
    amplifier_models = {}
    for part in parts:
        p, n = nodes[part.p], nodes[part.n]
        if p == n and part.kind not in {"Q", "S"}:
            raise CircuitError(f"{part.name} needs two distinct nodes.")
        kind, value = part.kind, part.value
        if kind == "K":
            # A mechanical contact has no control pins. Preserve its saved
            # state with the same finite resistance used by the native solver.
            kind = "R"
            state = "closed" if part.closed else "open"
            lines.append(
                f"* Mechanical switch {ascii(part.label or part.name)} is {state}; "
                "exported as its selected-state resistance."
            )
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
        elif kind == "A":
            kind = "X"
            model = part.audio or AudioAmpModel()
            gain_return = part.gain8 != part.vminus
            key = model, "audio", gain_return
            if key not in amplifier_models:
                amplifier_models[key] = model_names.claim(f"EC_AudAmp{len(amplifier_models) + 1}")
                definitions += audio_subcircuit(amplifier_models[key], model, gain_return)
            spec = amplifier_models[key]
        elif kind == "O":
            kind = "X"
            spec = POWERED_OPAMP_MODEL if part.vplus else OPAMP_MODEL
            if part.opamp is not None and part.opamp != OpAmpModel():
                key = part.opamp, bool(part.vplus)
                if key not in amplifier_models:
                    amplifier_models[key] = model_names.claim(f"EC_OpAmp{len(amplifier_models) + 1}")
                    definitions += behavioral_opamp_subcircuit(amplifier_models[key], *key)
                spec = amplifier_models[key]
        elif kind == "D":
            model = part.diode or DiodeModel()
            key = ("diode", model)
            if key not in amplifier_models:
                amplifier_models[key] = model_names.claim(f"EC_Diode{len(amplifier_models) + 1}")
                definitions += diode_subcircuit(amplifier_models[key], model)
            spec = amplifier_models[key]
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
        name = names.claim(preferred, "X" if kind == "D" else kind)
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
    behavior_lines = math_source_lines(circuit.behavior, nodes, names)
    return SpiceCircuit(nodes, elements, references, directive, tuple(lines), definitions, potentiometers, behavior_lines)


def _number(value):
    if value.imag or not math.isfinite(value.real):
        raise CircuitError("SPICE export requires finite real component values.")
    return repr(float(value.real or 0)).removesuffix(".0")
