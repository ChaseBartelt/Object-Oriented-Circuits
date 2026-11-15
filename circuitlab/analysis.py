"""Coordinate compilation, quantity analysis, and simulation."""

import cmath
import math
import re
from dataclasses import dataclass, field, replace

from .compiler import compile_circuit
from .graph import Circuit, CircuitError
from .math_engine import solve_known
from .numbers import set_known
from .simulation import Network, NodalBackend, SimulationBackend
from .syntax import FREQUENCY


@dataclass
class Analysis:
    """A circuit and the numeric and explanatory results shown in the UI."""

    circuit: Circuit
    calculated: dict = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    readings: list = field(default_factory=list)
    network: Network | None = None
    dc_network: Network | None = None


def solve_input(source, mode="auto", *, backend: SimulationBackend | None = None):
    """Compile and solve circuit text without starting Qt."""
    return analyze(compile_circuit(source, mode), backend)


def analyze(circuit, backend=None):
    """Dispatch to quantity rules or the network backend as appropriate."""
    circuit.require_models()
    circuit = replace(
        circuit, known=dict(circuit.known), equations=dict(circuit.equations)
    )
    result = Analysis(circuit)
    mode = circuit.mode
    backend = backend if backend is not None else NodalBackend()
    known = dict(circuit.known)
    if circuit.reduce_groups and not reduce_groups(result, known):
        return result
    equations = dict(circuit.equations)
    if not circuit.components:
        if mode == "mixed" or circuit.port or circuit.probes:
            raise CircuitError("Enter connected components and sources.")
        values, notes, derived = solve_known(mode, known, circuit.model)
        result.diagnostics.extend(notes)
        if not conflicts(notes):
            result.calculated = {
                key: value for key, value in values.items() if key not in known
            }
            equations.update(derived)
            if circuit.legacy_measure:
                legacy_measure(result, values)
        if (
            not result.diagnostics
            and mode == "ac"
            and set(known) & {"R", "C", "L"}
            and set(values) & {"Vmag", "Imag"}
            and "Z" not in values
        ):
            result.diagnostics.append(
                "Put the load components in a (series) or (parallel) block."
            )
        elif (
            not result.diagnostics
            and mode == "ac"
            and known.get("P", 0) > 0
            and values.get("Vmag", 0) > 0
            and not set(values) & {"Imag", "pf", "Z"}
        ):
            result.diagnostics += [
                "RMS current needs the load power factor or impedance.",
                "For a purely resistive load, enter pf = 1.",
            ]
        if (
            not conflicts(notes)
            and mode == "ac"
            and not circuit.legacy_measure
            and "w" in values
            and values.get("Vpk", 0) > 0
            and "phaseV" not in values
        ):
            result.diagnostics.append(
                "Waveforms need the source phase. For a 0° source, enter phaseV = 0."
            )
        if not result.calculated and not result.diagnostics:
            result.diagnostics.append(
                "No additional values are determined by these inputs."
            )
        circuit.known, circuit.equations = known, equations
        return result

    if set(known) - (FREQUENCY if mode in {"ac", "mixed"} else set()):
        raise CircuitError("Use only frequency quantities alongside a circuit list.")
    frequency = None
    if mode != "dc":
        values, notes, derived = solve_known("ac", known)
        result.diagnostics.extend(notes)
        circuit.equations.update(derived)
        if conflicts(notes):
            return result
        result.calculated = {
            key: value for key, value in values.items() if key not in known
        }
        frequency = values.get("f")
    port = tuple(map(circuit.resolve_wire, circuit.port)) if circuit.port else None
    if mode == "mixed":
        if frequency is None:
            raise CircuitError("Enter the AC frequency, period, or angular frequency.")
        result.dc_network = backend.solve(circuit, ac=False, frequency=None, port=port)
    try:
        result.network = backend.solve(
            circuit, ac=mode != "dc", frequency=frequency, port=port
        )
    except CircuitError as error:
        if circuit.grouped and error.code == "InconsistentNetwork":
            raise CircuitError(
                "The ideal source voltages conflict. Check the impedance and connections between them.",
                code=error.code,
            ) from error
        raise
    for network, ac in ((result.dc_network, False), (result.network, mode != "dc")):
        if network:
            add_probes(result, network, ac, backend)
            if network.equivalent_issue:
                result.diagnostics.append(network.equivalent_issue)
    return result


def conflicts(notes):
    return any("conflict" in note.lower() for note in notes)


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    line: int | None
    message: str
    path: object = None


def diagnostics(source, result):
    """Report both solver messages and dangling resistor banks."""
    issues = [
        Diagnostic("error" if conflicts([note]) else "warning", 1, note)
        for note in result.diagnostics
    ]
    lines = [line.partition("#")[0] for line in source.splitlines()]
    parts = result.circuit.components
    inputs = {pin for p in parts if p.kind == "O" for pin in (p.in_plus, p.in_minus)}
    for node in result.circuit.nodes:
        if node == "0" or node in inputs:
            continue
        incident = [
            p for p in parts if node in (p.p, p.n) or (p.kind == "Q" and node == p.base)
        ]
        neighbors = {pin for p in incident for pin in (p.p, p.n)} - {node}
        if (
            len(incident) < 2
            or len(neighbors) != 1
            or any(p.kind != "R" for p in incident)
        ):
            continue
        pattern = re.compile(r"(?<![\w.-])" + re.escape(node) + r"(?![\w.-])")
        line = max(
            (i for i, text in enumerate(lines, 1) if pattern.search(text)), default=1
        )
        neighbor = next(iter(neighbors))
        issues.append(
            Diagnostic(
                "warning",
                line,
                f'Node "{node}" only connects to "{neighbor}" through parallel resistors. '
                "The bank has no return path and carries no current.",
            )
        )
    return issues


def group_impedances(circuit, omega):
    """Reduce legacy groups from the deepest branch back to the root."""
    ac = circuit.mode == "ac"
    equivalents = {}
    impedances = {}
    for i in reversed(range(len(circuit.groups))):
        group = circuit.groups[i]
        branches = []
        for kind, _, value in group.entries:
            if kind == "G":
                z = equivalents[value]
            elif kind in {"C", "L"}:
                if ac:
                    product = omega * value
                    z = (
                        (1j * product if kind == "L" else -1j / product)
                        if product
                        else 0
                    )
                    if not z or not cmath.isfinite(z):
                        raise CircuitError(
                            "A component impedance is outside the numeric range.",
                            group.line,
                        )
                else:
                    z = math.inf if kind == "C" else 0
            elif kind == "X":
                z = 1j * value
            else:
                z = value
            branches.append(z)
        if group.mode == "series":
            if any(not cmath.isfinite(z) for z in branches):
                equivalents[i], impedances[i] = math.inf, branches
                continue
            answer = sum(branches)
        elif 0 in branches:
            answer = 0
        else:
            finite = [z for z in branches if cmath.isfinite(z)]
            # Normalize admittances before summing very large or small values.
            scale = min(map(abs, finite), default=1)
            y = sum(scale / z for z in finite)
            if not y:
                equivalents[i], impedances[i] = math.inf, branches
                continue
            answer = scale / y
        if not cmath.isfinite(answer):
            raise CircuitError("The equivalent impedance is outside the numeric range.")
        equivalents[i] = answer
        impedances[i] = branches
    return equivalents[0], impedances


def reduce_groups(result, known):
    c = result.circuit
    kinds = {kind for g in c.groups for kind, _, _ in g.entries}
    ac = c.mode == "ac"
    omega = 0
    if ac and kinds & {"C", "L"}:
        freq, notes, _ = solve_known(
            "ac", {k: v for k, v in known.items() if k in FREQUENCY}
        )
        result.diagnostics.extend(notes)
        if conflicts(notes):
            return False
        if "w" not in freq:
            raise CircuitError("Enter f, T, or w for capacitor and inductor groups.")
        omega = freq["w"]
    load, _ = group_impedances(c, omega)
    if not cmath.isfinite(load):
        if ac:
            raise CircuitError(
                "The group is open at this frequency (0 A for a finite source voltage)."
            )
        if "R" in known:
            raise CircuitError("A finite R conflicts with the open DC load.")
        if "I" not in known:
            c.equations["I"] = "I = 0 (open DC load)"
        set_known(known, "I", 0)
    else:
        key = "Z" if ac else "R"
        if key not in known:
            rules = []
            modes = {g.mode for g in c.groups}
            if "series" in modes:
                rules.append(f"{key}_series = Σ{key}_k")
            if "parallel" in modes:
                rules.append(f"{key}_parallel = 1 / Σ(1/{key}_k)")
            if "X" in kinds:
                rules.append("Z_X = jX")
            if not ac and "L" in kinds:
                rules.append("R_L = 0")
            if not ac and "C" in kinds:
                rules.append("R_C = ∞")
            c.equations[key] = " · ".join(rules)
        set_known(known, key, load)
    return True


def legacy_measure(result, values):
    """Follow a component's group path using series and parallel divider rules."""
    c = result.circuit
    name = c.legacy_measure
    if "Vmag" not in values:
        result.diagnostics.append(f"Enter the source voltage to measure {name}.")
        return
    load, impedances = group_impedances(c, values.get("w", 0))
    path = []
    for i, group in enumerate(c.groups):
        for j, (_, part, _) in enumerate(group.entries):
            if part.casefold() == name.casefold():
                path = [(i, j)]
    while (parent := c.groups[path[-1][0]].parent) is not None:
        index = next(
            j
            for j, (kind, _, value) in enumerate(c.groups[parent].entries)
            if kind == "G" and value == path[-1][0]
        )
        path.append((parent, index))
    voltage = values["Vmag"]
    current = voltage / load if load else values.get("Imag", math.nan)
    for i, j in reversed(path):
        branch = impedances[i][j]
        if c.groups[i].mode == "series":
            if not branch:
                voltage = 0
            elif cmath.isfinite(branch):
                voltage = current * branch
            elif sum(not cmath.isfinite(z) for z in impedances[i]) > 1:
                voltage = math.nan
        elif not cmath.isfinite(branch):
            current = 0
        elif branch:
            current = voltage / branch
        elif impedances[i].count(0) > 1:
            current = math.nan
    if not cmath.isfinite(voltage):
        result.diagnostics.append(
            f"The voltage across {name} cannot be determined for this circuit."
        )
        return
    result.readings.append(
        (
            f"{name} voltage peak",
            "Vpk = √2 x |Vbranch|",
            math.sqrt(2) * abs(voltage),
            "V",
            False,
        )
    )
    if voltage and values["Vmag"]:
        result.readings.append(
            (
                f"{name} phase vs source",
                "Phase = ∠(Vbranch / Vsource)",
                math.degrees(cmath.phase(voltage)),
                "deg",
                False,
            )
        )


def add_probes(result, network, ac, backend):
    """Evaluate explicit measurements on one solved operating point."""
    c = result.circuit
    prefix = ("AC " if ac else "DC ") if c.mode == "mixed" else ""
    for probe in c.probes:
        kind, targets = probe.kind, probe.targets
        if kind in "IP":
            target = targets[0]
            i = next(
                (
                    i
                    for i, e in enumerate(c.components)
                    if target.casefold() in (e.name.casefold(), e.label.casefold())
                ),
                None,
            )
            if i is None:
                raise CircuitError(f'Unknown component "{target}".')
            if kind == "I":
                reading = (
                    f"Measured current through {target}",
                    "I = I_branch",
                    network.branch_current[i],
                    "A",
                    ac,
                )
            else:
                equation = "P = Re(V x conj(I))" if ac else "P = V x I"
                reading = (
                    f"Measured power of {target}",
                    equation,
                    network.branch_power[i].real,
                    "W",
                    False,
                )
        else:
            resolved = tuple(map(c.resolve_wire, targets))
            if (
                len(set(targets)) != 2
                or (kind != "V" and resolved[0] == resolved[1])
                or any(node not in network.nodes for node in resolved)
            ):
                raise CircuitError(
                    "Use two distinct existing nodes to measure voltage or resistance."
                )
            name = (
                f"group at line {probe.line}"
                if probe.line is not None
                else f"{targets[0]} → {targets[1]}"
            )
            p, n = resolved
            if network.references.get(p) != network.references.get(n):
                raise CircuitError(
                    "Measure between two nodes in the same connected circuit."
                )
            if kind == "V":
                voltage = (
                    network.voltage[network.nodes.index(p)]
                    - network.voltage[network.nodes.index(n)]
                )
                reading = f"Measured voltage ({name})", "V = V+ - V-", voltage, "V", ac
            else:
                test = backend.solve(
                    c, ac=ac, frequency=network.frequency, port=resolved
                )
                if not test.equivalent:
                    raise CircuitError(test.equivalent_issue)
                reading = (
                    f"Measured {'impedance' if ac else 'resistance'} ({name})",
                    "Z_eq = V_test / 1 A (sources off)",
                    test.equivalent["impedance"],
                    "ohm",
                    ac,
                )
        result.readings.append((prefix + reading[0], *reading[1:]))
