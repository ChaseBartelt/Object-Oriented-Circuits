"""Coordinate compilation, quantity analysis, and simulation."""
# This module is the coordinator; numerical stamping and equation propagation live in their
# own backends. Keep source interpretation out of the numerical result/reporting paths.

import cmath
import math
import re
from dataclasses import dataclass, field, replace

from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import Circuit, CircuitError
from circuitlab.math.equations import solve_known
from circuitlab.math.numbers import set_known
from circuitlab.math.linear import NodalBackend
from circuitlab.math.results import Network, SimulationBackend
from circuitlab.language.syntax import FREQUENCY


# Two input styles meet here: standalone electrical quantities and explicit
# component networks. Legacy series/parallel groups can also be reduced
# to an equivalent load before the quantity equations are solved.
@dataclass
class Analysis:
    """A circuit and the numeric and explanatory results shown in the UI."""

    # Keep the circuit alongside its results so reports can explain how each
    # number relates to the original components and requested measurements.
    circuit: Circuit
    # Derived quantities only; values supplied by the user stay in circuit.known.
    # Factories give every Analysis its own mutable containers.
    calculated: dict = field(default_factory=dict)
    # Solver messages are converted into source-located diagnostics below.
    diagnostics: list[str] = field(default_factory=list)
    # Each reading stores a label, explanation, value, unit, and phasor flag.
    readings: list = field(default_factory=list)
    # For a mixed DC+AC solve, network holds the AC solution and dc_network
    # holds the separate DC operating point. Ordinary solves use network only.
    network: Network | None = None
    dc_network: Network | None = None


def solve_input(source, mode="auto", *, backend: SimulationBackend | None = None):
    """Compile and solve circuit text without starting Qt."""
    # The compiler resolves source syntax first; analysis works on its graph.
    # Passing a backend lets callers substitute another numerical solver.
    return analyze(compile_circuit(source, mode), backend)


def analyze(circuit, backend=None):
    """Dispatch to quantity rules or the network backend as appropriate."""
    # Reject missing device models or unspecified component values before
    # attempting numerical analysis.
    circuit.require_models()
    circuit.require_values()
    # Copy the mutable quantity/equation dictionaries before adding derived
    # information, so analysis does not change those dictionaries on the caller.
    circuit = replace(
        circuit, known=dict(circuit.known), equations=dict(circuit.equations)
    )
    result = Analysis(circuit)
    mode = circuit.mode
    # Use nodal analysis by default, while honoring an explicitly supplied backend.
    backend = backend if backend is not None else NodalBackend()
    known = dict(circuit.known)
    # Legacy grouped calculations first become one R or Z value. A frequency
    # conflict produces diagnostics and stops this path before further inference.
    if circuit.reduce_groups and not reduce_groups(result, known):
        return result
    equations = dict(circuit.equations)
    # Quantity-only inputs use equation rules rather than a node matrix.
    # Ports and explicit probes require a connected component graph instead.
    if not circuit.components:
        if mode == "mixed" or circuit.port or circuit.probes:
            raise CircuitError("Enter connected components and sources.")
        values, notes, derived = solve_known(mode, known, circuit.model)
        result.diagnostics.extend(notes)
        # Keep newly inferred values separate from the givens. Do not publish
        # derived results when the input equations contradict one another.
        if not conflicts(notes):
            result.calculated = {
                key: value for key, value in values.items() if key not in known
            }
            equations.update(derived)
            if circuit.legacy_measure:
                legacy_measure(result, values)
        # Part values alone may leave the AC load arrangement unspecified.
        # Ask for series/parallel structure when no total impedance was inferred.
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
        # Real power and RMS voltage do not determine RMS current without
        # power factor or impedance; pf = 1 supplies the resistive-load assumption.
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
        # An absolute voltage waveform also needs a source phase. Legacy branch
        # measurements use a source-relative reference and take a different path.
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
        # Explain a valid but unproductive solve instead of leaving the UI empty.
        if not result.calculated and not result.diagnostics:
            result.diagnostics.append(
                "No additional values are determined by these inputs."
            )
        # Retain any reduced-load givens and the formulas used by the report.
        circuit.known, circuit.equations = known, equations
        return result

    # Explicit component networks carry their own source and part values.
    # Only global frequency quantities may accompany that representation.
    if set(known) - (FREQUENCY if mode in {"ac", "mixed"} else set()):
        raise CircuitError("Use only frequency quantities alongside a circuit list.")
    frequency = None
    # Normalize frequency inputs such as f, period T, and angular frequency w.
    # The backend decides whether a particular AC network needs a frequency.
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
    # Convert user-facing wire aliases into the canonical names used by the solver.
    port = tuple(map(circuit.resolve_wire, circuit.port)) if circuit.port else None
    # Mixed mode solves DC and AC independently. Keep both operating points
    # so reports and probes can distinguish the constant and phasor components.
    if mode == "mixed":
        if frequency is None:
            raise CircuitError("Enter the AC frequency, period, or angular frequency.")
        result.dc_network = backend.solve(circuit, ac=False, frequency=None, port=port)
    # Use real DC or complex AC analysis for the main operating point.
    try:
        result.network = backend.solve(
            circuit, ac=mode != "dc", frequency=frequency, port=port
        )
    except CircuitError as error:
        # Translate this grouped-network failure into an actionable wiring message
        # while retaining the original exception as its cause.
        if circuit.grouped and error.code == "InconsistentNetwork":
            raise CircuitError(
                "The ideal source voltages conflict. Check the impedance and connections between them.",
                code=error.code,
            ) from error
        raise
    # Evaluate measurements separately on each available operating point.
    # Equivalent-circuit limitations are reported alongside the numeric results.
    for network, ac in ((result.dc_network, False), (result.network, mode != "dc")):
        if network:
            add_probes(result, network, ac, backend)
            if network.equivalent_issue:
                result.diagnostics.append(network.equivalent_issue)
    return result


# Quantity-rule diagnostics are text messages. Centralize their existing
# conflict convention so dispatch and reporting classify them consistently.
def conflicts(notes):
    return any("conflict" in note.lower() for note in notes)


# Source-facing diagnostics are immutable records. line may be absent
# for a general issue; path can identify an included source file.
@dataclass(frozen=True)
class Diagnostic:
    severity: str
    line: int | None
    message: str
    path: object = None


def diagnostics(source, result):
    """Report both solver messages and dangling resistor banks."""
    # Promote contradictory inputs to errors; other solver messages remain warnings.
    # Messages without a more specific source location initially point to line 1.
    issues = [
        Diagnostic("error" if conflicts([note]) else "warning", 1, note)
        for note in result.diagnostics
    ]
    # Ignore comments when locating the code responsible for a wiring warning.
    lines = [line.partition("#")[0] for line in source.splitlines()]
    parts = result.circuit.components
    # Ideal op-amp inputs sense voltage without drawing input current; they
    # should not be mistaken for an accidentally unused resistor-bank node.
    inputs = {pin for p in parts if p.kind == "O" for pin in (p.in_plus, p.in_minus)}
    for node in result.circuit.nodes:
        if node == "0" or node in inputs:
            continue
        # Find every component touching this node, then look at the other ends.
        incident = [
            p for p in parts if node in p.terminal_nets
        ]
        neighbors = {pin for p in incident for pin in (p.p, p.n)} - {node}
        # Warn only about multiple resistors that all return to one other node.
        # That dangling bank has no separate return path and carries no current.
        if (
            len(incident) < 2
            or len(neighbors) != 1
            or any(p.kind != "R" for p in incident)
        ):
            continue
        # Match the complete wire name, including names containing dots/hyphens.
        # Escape it because a wire name must not become a regular expression.
        pattern = re.compile(r"(?<![\w.-])" + re.escape(node) + r"(?![\w.-])")
        # Attach the warning to the last code line mentioning this node.
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
    circuit.require_values()
    ac = circuit.mode == "ac"
    # equivalents maps group indices to total impedance; impedances preserves
    # each group's branch values for later divider-based measurements.
    equivalents = {}
    impedances = {}
    # Children follow their parents in this representation. Reverse traversal
    # therefore calculates nested groups before their parent needs the result.
    for i in reversed(range(len(circuit.groups))):
        group = circuit.groups[i]
        branches = []
        for kind, _, value in group.entries:
            # A G entry refers to another group by index rather than a physical part.
            if kind == "G":
                z = equivalents[value]
            elif kind in {"C", "L"}:
                if ac:
                    # Phasor impedances: an inductor is j*w*L; a capacitor is 1/(j*w*C).
                    product = omega * value
                    z = (
                        (1j * product if kind == "L" else -1j / product)
                        if product
                        else 0
                    )
                    # Guard against underflow, division limits, or non-finite impedance values.
                    if not z or not cmath.isfinite(z):
                        raise CircuitError(
                            "A component impedance is outside the numeric range.",
                            group.line,
                        )
                else:
                    # At DC steady state, an ideal capacitor is open and an ideal inductor is shorted.
                    z = math.inf if kind == "C" else 0
            # X is a specified reactance, while Z is an already-complex impedance.
            elif kind == "X":
                z = 1j * value
            elif kind == "Z":
                if not ac or not value or not cmath.isfinite(value):
                    raise CircuitError("Theoretical impedance Z requires a finite, nonzero value in AC steady state.",
                                       group.line, code="UnsupportedComponent")
                z = value
            else:
                z = value
            branches.append(z)
        # Series impedances add; any open branch makes the whole series path open.
        if group.mode == "series":
            if any(not cmath.isfinite(z) for z in branches):
                equivalents[i], impedances[i] = math.inf, branches
                continue
            answer = sum(branches)
        # A zero-impedance branch shorts a parallel combination.
        elif 0 in branches:
            answer = 0
        else:
            # Open parallel branches contribute zero admittance and can be omitted.
            finite = [z for z in branches if cmath.isfinite(z)]
            # Normalize admittances before summing very large or small values.
            scale = min(map(abs, finite), default=1)
            y = sum(scale / z for z in finite)
            # Zero total admittance means an open equivalent, including the case
            # where reactive branch admittances cancel exactly.
            if not y:
                equivalents[i], impedances[i] = math.inf, branches
                continue
            answer = scale / y
        # Physical opens were handled above. A non-finite arithmetic result here
        # is a numerical-range failure rather than a usable equivalent impedance.
        if not cmath.isfinite(answer):
            raise CircuitError("The equivalent impedance is outside the numeric range.")
        equivalents[i] = answer
        impedances[i] = branches
    # Group 0 is the root of the legacy grouped calculation.
    return equivalents[0], impedances


def reduce_groups(result, known):
    c = result.circuit
    c.require_values()
    kinds = {kind for g in c.groups for kind, _, _ in g.entries}
    ac = c.mode == "ac"
    omega = 0
    # Only frequency-dependent capacitors/inductors need omega for reduction.
    # Pass just the frequency givens to this preliminary equation solve.
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
    # Replace the whole group tree with a single equivalent load quantity.
    load, _ = group_impedances(c, omega)
    # An open DC load fixes current at zero. A specified finite resistance
    # would contradict it; open AC groups use the explicit error path below.
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
        # Use complex impedance in AC and equivalent resistance in DC.
        key = "Z" if ac else "R"
        if key not in known:
            # Build the human-readable derivation shown alongside the inferred load.
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
        # Merge the inferred load through the same consistency checks as other givens.
        set_known(known, key, load)
    return True


def legacy_measure(result, values):
    """Follow a component's group path using series and parallel divider rules."""
    c = result.circuit
    name = c.legacy_measure
    # Legacy component measurements start from the source's RMS magnitude.
    if "Vmag" not in values:
        result.diagnostics.append(f"Enter the source voltage to measure {name}.")
        return
    load, impedances = group_impedances(c, values.get("w", 0))
    # Locate the requested component inside its containing group, then build
    # a path from that leaf group back through every enclosing parent.
    path = []
    for i, group in enumerate(c.groups):
        for j, (_, part, _) in enumerate(group.entries):
            if part.casefold() == name.casefold():
                path = [(i, j)]
    # Find the parent's G entry that represents the current child group.
    while (parent := c.groups[path[-1][0]].parent) is not None:
        index = next(
            j
            for j, (kind, _, value) in enumerate(c.groups[parent].entries)
            if kind == "G" and value == path[-1][0]
        )
        path.append((parent, index))
    # Treat the source magnitude as a zero-angle reference phasor. Branch
    # angles computed below are consequently relative to that source.
    voltage = values["Vmag"]
    # For a shorted total load, use a separately known current if available;
    # otherwise carry NaN to mark quantities that cannot be determined.
    current = voltage / load if load else values.get("Imag", math.nan)
    # Walk root-to-leaf, carrying the voltage/current into the selected branch.
    for i, j in reversed(path):
        branch = impedances[i][j]
        # All series branches share current; obtain the selected voltage from I*Z.
        if c.groups[i].mode == "series":
            if not branch:
                voltage = 0
            elif cmath.isfinite(branch):
                voltage = current * branch
            # Multiple series opens do not determine how the total voltage divides.
            elif sum(not cmath.isfinite(z) for z in impedances[i]) > 1:
                voltage = math.nan
        # Parallel branches share voltage. An open draws no current; a finite
        # nonzero branch draws V/Z. Multiple ideal shorts leave the split unknown.
        elif not cmath.isfinite(branch):
            current = 0
        elif branch:
            current = voltage / branch
        elif impedances[i].count(0) > 1:
            current = math.nan
    # Do not turn an indeterminate divider voltage into a misleading reading.
    if not cmath.isfinite(voltage):
        result.diagnostics.append(
            f"The voltage across {name} cannot be determined for this circuit."
        )
        return
    # Convert branch RMS magnitude to peak magnitude for this legacy display.
    result.readings.append(
        (
            f"{name} voltage peak",
            "Vpk = √2 x |Vbranch|",
            math.sqrt(2) * abs(voltage),
            "V",
            False,
        )
    )
    # A zero phasor has no useful phase; report an angle only for nonzero voltage.
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
    # Label mixed-mode readings so the two operating points remain distinguishable.
    prefix = ("AC " if ac else "DC ") if c.mode == "mixed" else ""
    for probe in c.probes:
        kind, targets = probe.kind, probe.targets
        # Current and power probes name a component rather than a pair of nodes.
        if kind in "IP":
            target = targets[0]
            # Backend branch arrays follow component order. Accept either the internal
            # component name or its display label, without case sensitivity.
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
            # A potentiometer exposes current entering its high terminal; its two
            # internal resistor legs have already been folded into one public component.
            if kind == "I":
                reading = (
                    f"Measured current through {target}",
                    "I = I_high (into high terminal)" if c.components[i].kind == "P" else "I = I_branch",
                    network.branch_current[i],
                    "A",
                    ac,
                )
            else:
                # For AC, report real (average) power rather than the full complex power.
                # A potentiometer's result includes dissipation from both resistor legs.
                equation = ("P = P_high_leg + P_low_leg" if c.components[i].kind == "P" else
                            "P = Re(V x conj(I))" if ac else "P = V x I")
                reading = (
                    f"Measured power of {target}",
                    equation,
                    network.branch_power[i].real,
                    "W",
                    False,
                )
        else:
            # Voltage and equivalent-resistance/impedance probes use canonical node names.
            resolved = tuple(map(c.resolve_wire, targets))
            # Require existing nodes. Different aliases of one net may measure 0 V,
            # but they cannot define a two-terminal equivalent resistance.
            if (
                len(set(targets)) != 2
                or (kind != "V" and resolved[0] == resolved[1])
                or any(node not in network.nodes for node in resolved)
            ):
                raise CircuitError(
                    "Use two distinct existing nodes to measure voltage or resistance."
                )
            # Grouped probes keep their source-line context; explicit probes show names.
            name = (
                f"group at line {probe.line}"
                if probe.line is not None
                else f"{targets[0]} → {targets[1]}"
            )
            p, n = resolved
            # Voltages from separate reference domains cannot be directly subtracted.
            if network.references.get(p) != network.references.get(n):
                raise CircuitError(
                    "Measure between two nodes in the same connected circuit."
                )
            if kind == "V":
                # The requested polarity is the first node minus the second node.
                voltage = (
                    network.voltage[network.nodes.index(p)]
                    - network.voltage[network.nodes.index(n)]
                )
                reading = f"Measured voltage ({name})", "V = V+ - V-", voltage, "V", ac
            else:
                # Ask the backend for the equivalent seen at this port, using the same
                # analysis mode and frequency as the operating point being measured.
                test = backend.solve(
                    c, ac=ac, frequency=network.frequency, port=resolved
                )
                # Propagate the backend's explanation if no valid equivalent can be obtained.
                if not test.equivalent:
                    raise CircuitError(test.equivalent_issue)
                reading = (
                    f"Measured {'impedance' if ac else 'resistance'} ({name})",
                    "Z_eq = V_test / 1 A (sources off)",
                    test.equivalent["impedance"],
                    "ohm",
                    ac,
                )
        # Apply the DC/AC label while retaining the standard reading tuple layout.
        result.readings.append((prefix + reading[0], *reading[1:]))
