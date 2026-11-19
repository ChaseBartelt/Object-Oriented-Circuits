"""Modified nodal analysis with an interchangeable simulation backend."""

import math

import numpy as np
from scipy import linalg

from circuitlab.model.graph import CircuitError, ground_references
from circuitlab.model.potentiometer import expand_potentiometers
from circuitlab.math.results import Network, SimulationBackend, fold_potentiometers


def solve_system(matrix, rhs, scale):
    """Scale the system and distinguish inconsistent from non-unique solutions."""
    if not all(np.isfinite(x).all() for x in (matrix, rhs, scale)):
        raise CircuitError(
            "The circuit equations exceed the numeric range.", code="NumericalNetwork"
        )
    if not len(matrix):
        return np.empty(0, complex)
    row = np.max(scale, axis=1)
    row[row == 0] = 1
    column = np.max(scale / row[:, None], axis=0)
    column[column == 0] = 1
    a = matrix / row[:, None] / column
    b = rhs / row
    tolerance = 16 * len(a) * np.finfo(float).eps
    left, singular, _ = linalg.svd(a, check_finite=False)
    if np.min(singular) <= tolerance:
        null = left[:, singular <= tolerance]
        if np.any(np.abs(null.conj().T @ b) > tolerance * (np.abs(null).T @ np.abs(b))):
            raise CircuitError(
                "Ideal source or connection constraints conflict.",
                code="InconsistentNetwork",
            )
        raise CircuitError(
            "No unique circuit solution. Check for floating nodes or redundant ideal sources.",
            code="UnderdeterminedNetwork",
        )
    answer = linalg.solve(a, b, check_finite=False) / column
    if not np.isfinite(answer).all():
        raise CircuitError(
            "The circuit solution exceeds the numeric range.", code="NumericalNetwork"
        )
    return answer


class NodalBackend:
    """Solve linear DC or AC networks by modified nodal analysis."""

    def solve(self, circuit, *, ac, frequency=None, port=None):
        circuit.require_values()
        parts, potentiometers = expand_potentiometers(circuit)
        nonlinear = any(part.kind in {"Q", "S"} for part in parts)
        if any(part.kind == "Z" for part in parts):
            if not ac or circuit.mode != "ac":
                raise CircuitError("Theoretical impedance Z supports AC steady state only; "
                                   "use physical R/L/C components for DC or transient analysis.", code="UnsupportedComponent")
            if nonlinear:
                raise CircuitError("Theoretical impedance Z cannot be combined with native BJTs or switches: "
                                   "their native solver supports DC and time-domain analysis, not AC.", code="UnsupportedComponent")
        if nonlinear:
            if ac or port or circuit.mode != "dc":
                raise CircuitError("Use DC for the built-in nonlinear operating point. "
                                   "Run AC time-domain waveforms in the Results oscilloscope; "
                                   "use SPICE for small-signal AC or port analysis.",
                                   code="UnsupportedComponent")
            from circuitlab.math.nonlinear import TimeDomain

            return TimeDomain(circuit).operating_point()
        names = tuple(e.name for e in parts)
        if not parts or len({name.casefold() for name in names}) != len(names):
            raise CircuitError(
                "Enter elements with unique names and two nodes.", code="InvalidNetwork"
            )
        if not any({e.p, e.n} & circuit.grounds for e in parts):
            raise CircuitError(
                "Connect the circuit to ground node 0.", code="UnderdeterminedNetwork"
            )
        if ac and frequency is None and any(e.kind in "CL" for e in parts):
            raise CircuitError(
                "Enter frequency for AC capacitors and inductors.",
                code="MissingFrequency",
            )
        if frequency is not None and (not np.isfinite(frequency) or frequency <= 0):
            raise CircuitError("AC frequency must be positive.")
        nodes = circuit.nodes
        index = {name: i for i, name in enumerate(nodes)}
        # The op amp's output branch conducts. Its input pins only sense voltage.
        references = ground_references(
            circuit.grounds, ((part.p, part.n) for part in parts)
        )
        # Keep the unused standard reference out of the matrix. If 0 appears
        # only as an input pin, it is still a valid explicit voltage reference.
        references.setdefault("0", "0")
        unknown = [i for i, node in enumerate(nodes) if references.get(node) != node]
        incidence = np.zeros((len(nodes), len(parts)))
        y = np.zeros(len(parts), complex)
        currents = np.zeros(len(parts), complex)
        constrained = []
        senses = []
        targets = []
        # Each branch column points from its positive terminal to its negative
        # terminal. Voltage-defined branches add a current unknown and a constraint.
        for k, part in enumerate(parts):
            p, n = index[part.p], index[part.n]
            if p == n:
                raise CircuitError(f"{part.name} needs two distinct nodes.")
            incidence[p, k], incidence[n, k] = 1, -1
            kind = part.kind
            value = (
                part.dc
                if circuit.mode == "mixed" and not ac and kind in "VI"
                else part.value
            )
            if kind not in {"R", "K", "C", "L", "X", "Z", "V", "I", "O"}:
                raise CircuitError(
                    f"Unsupported component type {kind}.", code="UnsupportedComponent"
                )
            if kind in "RKCL" and (value.imag != 0 or value.real <= 0):
                raise CircuitError(f"{part.name} requires a positive real value.")
            if not ac and kind in "VI" and value.imag:
                raise CircuitError(f"{part.name} requires a real value in DC.")
            if kind in "VO" or (kind == "L" and not ac):
                constrained.append(k)
                row = incidence[:, k].copy()
                if kind == "O":
                    row[:] = 0
                    row[index[part.in_plus]] += 1
                    row[index[part.in_minus]] -= 1
                senses.append(row[unknown])
                targets.append(value if kind == "V" else 0)
            elif kind == "I":
                currents[k] = value
            elif kind in {"R", "K"}:
                y[k] = 1 / value
            elif kind == "Z":
                if not value or not np.isfinite(value):
                    raise CircuitError(f"{part.name} requires a finite, nonzero AC impedance.")
                try:
                    y[k] = 1 / value
                except (OverflowError, ZeroDivisionError):
                    raise CircuitError("An element admittance is outside the numeric range.",
                                       code="NumericalNetwork") from None
            elif kind == "X":
                if not ac or not value or value.imag:
                    raise CircuitError(
                        f"{part.name} requires a nonzero real reactance in AC."
                    )
                y[k] = -1j / value
            elif ac and kind == "C":
                y[k] = 1j * (math.tau * (frequency * value))
            elif ac and kind == "L":
                impedance = math.tau * (frequency * value)
                if impedance:
                    y[k] = -1j / impedance
            if (kind in "RKXZ" or (ac and kind in "CL")) and (
                not y[k] or not np.isfinite(y[k])
            ):
                raise CircuitError(
                    "An element admittance is outside the numeric range.",
                    code="NumericalNetwork",
                )
        # Stamp KCL in the upper rows and source constraints in the lower rows.
        a = incidence[unknown]
        cols = a[:, constrained]
        rows = np.array(senses).reshape(len(constrained), len(unknown))
        zeros = np.zeros((len(constrained), len(constrained)))
        matrix = np.block([[(a * y) @ a.T, cols], [rows, zeros]])
        scale = np.block(
            [[(abs(a) * abs(y)) @ abs(a).T, abs(cols)], [abs(rows), zeros]]
        )
        try:
            result = solve_system(matrix, np.r_[-a @ currents, targets], scale)
        except CircuitError as error:
            if not ac and error.code in {
                "UnderdeterminedNetwork",
                "InconsistentNetwork",
            }:
                capacitor_nodes = {
                    node for p in parts if p.kind == "C" for node in (p.p, p.n)
                }
                other_nodes = {
                    node for p in parts if p.kind != "C" for node in (p.p, p.n)
                }
                floating = sorted(
                    node
                    for node in circuit.grounds & (capacitor_nodes - other_nodes)
                    if references.get(node) not in {None, node}
                )
                if floating:
                    names = ", ".join(floating)
                    raise CircuitError(
                        f"Floating ground return in DC: {names}. Its only connection is through a capacitor.",
                        code="UnderdeterminedNetwork",
                    ) from error
            raise
        voltages = np.zeros(len(nodes), complex)
        voltages[unknown] = result[: len(unknown)]
        branch_v = incidence.T @ voltages
        currents += y * branch_v
        currents[constrained] = result[len(unknown) :]
        branch_power = branch_v * currents.conj()
        if not all(np.isfinite(x).all() for x in (branch_v, currents, branch_power)):
            raise CircuitError(
                "A branch result is outside the numeric range.", code="NumericalNetwork"
            )
        network = Network(
            nodes,
            names,
            voltages,
            branch_v,
            currents,
            branch_power,
            frequency,
            references=references,
        )
        if port:
            if any(node not in index for node in port) or port[0] == port[1]:
                raise CircuitError(
                    "The equivalent port needs two distinct existing nodes."
                )
            if references.get(port[0]) != references.get(port[1]):
                raise CircuitError("Use two port nodes in the same connected circuit.")
            p, n = (index[node] for node in port)
            # A unit test current with the independent sources zeroed gives Zth.
            test = np.zeros(len(nodes))
            test[p], test[n] = 1, -1
            response = np.zeros(len(nodes), complex)
            response[unknown] = solve_system(
                matrix, np.r_[test[unknown], np.zeros(len(constrained))], scale
            )[: len(unknown)]
            z = response[p] - response[n]
            v = voltages[p] - voltages[n]
            if z and abs(z) <= 32 * np.finfo(float).eps * max(
                abs(response[p]), abs(response[n])
            ):
                network.equivalent_issue = (
                    "The port impedance is below numerical precision."
                )
                return fold_potentiometers(network, circuit, potentiometers)
            if not z:
                network.equivalent_issue = (
                    "Zero port impedance leaves no unique finite short-circuit current."
                )
            network.equivalent = {
                "voltage": v,
                "impedance": z,
                "current": v / z if z else None,
            }
        return fold_potentiometers(network, circuit, potentiometers)
