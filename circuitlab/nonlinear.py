"""Nonlinear NPN and PNP operating points and implicit time steps."""

import math
import warnings

import numpy as np
from scipy import linalg

from .graph import CircuitError, ground_references

# Generic room-temperature Ebers–Moll parameters.
SATURATION_CURRENT = 1e-15
THERMAL_VOLTAGE = 0.02585
FORWARD_GAIN = 100
REVERSE_GAIN = 2
MAX_STEP_HALVINGS = 18


class TimeDomain:
    """Stamp a circuit once and advance its capacitor and inductor state."""

    def __init__(self, circuit, step=None):
        self.circuit = circuit
        self.parts = circuit.components
        self.nodes = circuit.nodes
        self.step = step
        if step is not None and (not math.isfinite(step) or step <= 0):
            raise CircuitError("Enter a positive time step.")
        self.time = 0.0
        if not self.parts or circuit.mode != "dc":
            raise CircuitError("Run time simulation on a circuit with DC sources.")
        if len({part.name.casefold() for part in self.parts}) != len(self.parts):
            raise CircuitError("Give each component a unique name before simulating.")
        if any(part.kind not in {"R", "C", "L", "V", "I", "O", "Q"} for part in self.parts):
            raise CircuitError("Use R, C, L, DC sources, ideal op amps, or BJTs in time simulation.")
        if any(part.kind == "Q" and part.model for part in self.parts):
            raise CircuitError("Named BJT models require SPICE. Omit the model to use the built-in generic transistor.")
        if not any(set(part.terminal_nets) & circuit.grounds for part in self.parts):
            raise CircuitError("Connect the circuit to a ground node.")
        self.references = ground_references(circuit.grounds, (
            part.terminal_nets if part.kind == "Q" else (part.p, part.n)
            for part in self.parts
        ))
        self.references.setdefault("0", "0")
        self.unknown = [i for i, net in enumerate(self.nodes) if self.references.get(net) != net]
        indices = {net: i for i, net in enumerate(self.nodes)}
        self.incidence = np.zeros((len(self.parts), len(self.nodes)))
        self.voltage_rows = [
            i for i, part in enumerate(self.parts) if part.kind in {"V", "O", "L"}
        ]
        rows = {i: len(self.unknown) + n for n, i in enumerate(self.voltage_rows)}
        size = len(self.unknown) + len(rows)
        if not size:
            raise CircuitError("Connect the circuit to at least one non-ground node.")
        self.matrix = np.zeros((size, size))
        self.target = np.zeros(size)
        self.capacitors = {}
        self.inductors = {}
        self.transistors = []
        self.direct_current = np.zeros(len(self.parts))
        self.conductance = np.zeros(len(self.parts))
        self.base_current = {}
        inputs, outputs = [], []

        for i, part in enumerate(self.parts):
            p, n = indices[part.p], indices[part.n]
            self.incidence[i, p] += 1
            self.incidence[i, n] -= 1
            line = self.incidence[i, self.unknown]
            if part.kind != "Q" and p == n:
                raise CircuitError(f"{part.label} needs two distinct nodes.")
            value = part.value
            if part.kind in {"R", "C", "L", "V", "I"} and (
                value.imag or not math.isfinite(value.real) or (part.kind in "RCL" and value.real <= 0)
            ):
                raise CircuitError(f"{part.label} requires a valid real component value.")
            if part.kind in "RC":
                admittance = 1 / value.real if part.kind == "R" else (value.real / step if step else 0)
                self.conductance[i] = admittance
                self.matrix[:len(line), :len(line)] += admittance * np.outer(line, line)
                if step and part.kind == "C":
                    self.capacitors[i] = admittance
            elif part.kind == "I":
                self.target[:len(line)] -= value.real * line
                self.direct_current[i] = value.real
            elif i in rows:
                row = rows[i]
                self.matrix[:len(line), row] = line
                if part.kind == "O":
                    sense = np.zeros(len(self.nodes))
                    sense[indices[part.in_plus]] += 1
                    sense[indices[part.in_minus]] -= 1
                    self.matrix[row, :len(line)] = sense[self.unknown]
                else:
                    self.matrix[row, :len(line)] = line
                    if part.kind == "V":
                        self.target[row] = value.real
                    elif step:
                        self.inductors[row] = value.real / step
                        self.matrix[row, row] = -self.inductors[row]
            elif part.kind == "Q":
                base = np.zeros(len(self.nodes))
                base[indices[part.base]] += 1
                sign = -1 if part.transistor_type == "pnp" else 1
                be, bc = base.copy(), base.copy()
                be[n] -= 1
                bc[p] -= 1
                tests = np.zeros((2, size))
                tests[:, :len(line)] = sign * np.array([be[self.unknown], bc[self.unknown]])
                inputs.extend(tests)
                flows = np.zeros((2, size))
                flows[:, :len(line)] = sign * np.array([
                    line + be[self.unknown] / FORWARD_GAIN,
                    -line + bc[self.unknown] / REVERSE_GAIN,
                ])
                outputs.extend(flows)
                self.transistors.append((i, sign, indices[part.base], p, n))

        self.inputs = np.asarray(inputs).reshape(len(inputs), size) / THERMAL_VOLTAGE
        self.outputs = np.asarray(outputs).reshape(len(outputs), size).T
        self.state = np.zeros(size)
        self.previous_capacitor_voltage = np.zeros(len(self.parts))
        self.half_step = None

    def set_initial_voltage(self, capacitor, voltage):
        for i in self.capacitors:
            if capacitor in {self.parts[i].name, self.parts[i].label}:
                if not math.isfinite(voltage):
                    break
                self.previous_capacitor_voltage[i] = voltage
                return
        raise CircuitError("Choose a capacitor and a finite initial voltage.")

    @staticmethod
    def junctions(values):
        # A linear extension above the exponential limit keeps Newton finite.
        limited = np.minimum(values, 40)
        slope = SATURATION_CURRENT * np.exp(limited)
        return SATURATION_CURRENT * np.expm1(limited) + slope * (values - limited), slope

    def iterate(self, state, rhs, leakage=0.0, *, matrix=None):
        count = len(self.unknown)
        matrix = self.matrix if matrix is None else matrix
        if leakage:
            matrix = matrix.copy()
            matrix[np.arange(count), np.arange(count)] += leakage
        for _ in range(130):
            values = self.inputs @ state
            currents, slope = self.junctions(values)
            residual = matrix @ state + self.outputs @ currents - rhs
            error = np.linalg.norm(residual, np.inf)
            jacobian = matrix + (self.outputs * slope) @ self.inputs
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", linalg.LinAlgWarning)
                    delta = linalg.solve(jacobian, -residual, check_finite=False)
            except (ValueError, linalg.LinAlgError):
                break
            if not np.isfinite(delta).all():
                break
            if error < 2e-10 and np.linalg.norm(delta, np.inf) < 1e-7:
                return state
            # Limit growth of a forward junction. Reverse-biased movement
            # and steps that turn a junction off can proceed freely.
            change = self.inputs @ delta
            available = np.maximum(8, 26 - values)
            scale = 1 / np.max(np.maximum(change / available, 1), initial=1)
            for _ in range(14):
                trial = state + scale * delta
                trial_current, _ = self.junctions(self.inputs @ trial)
                trial_error = np.linalg.norm(matrix @ trial + self.outputs @ trial_current - rhs, np.inf)
                if trial_error < error * (1 + 1e-8) or trial_error < 2e-10:
                    state = trial
                    break
                scale /= 2
            else:
                break
        return None

    def solve(self, rhs):
        state = self.iterate(self.state, rhs)
        if state is None and self.step is None:
            state = self.bias_from_capacitors(rhs)
        if state is None:
            state = self.state
            for leakage in (1e-2, 1e-3, 1e-4, 1e-5, 1e-7, 1e-9, 0):
                state = self.iterate(state, rhs, leakage)
                if state is None:
                    raise CircuitError(
                        "The built-in nonlinear solver could not find an operating point."
                        if self.step is None else
                        f"The built-in nonlinear solver could not advance past {self.time * 1000:.4g} ms.",
                        code="NonlinearConvergence",
                    )
        self.state = state
        voltage = np.zeros(len(self.nodes))
        voltage[self.unknown] = state[:len(self.unknown)]
        return voltage

    def bias_from_capacitors(self, rhs):
        """Use one implicit capacitor step as a starting guess for the DC equations."""
        count = len(self.unknown)
        matrix = self.matrix.copy()
        bias_step = 1e-3
        has_capacitors = False
        for i, part in enumerate(self.parts):
            if part.kind == "C":
                has_capacitors = True
                lead = self.incidence[i, self.unknown]
                matrix[:count, :count] += (part.value.real / bias_step) * np.outer(lead, lead)
        if not has_capacitors:
            return None
        guess = self.iterate(self.state, rhs, matrix=matrix)
        return None if guess is None else self.iterate(guess, rhs)

    def advance(self, remaining_halvings=MAX_STEP_HALVINGS):
        """Advance by the requested interval, retrying difficult switching edges."""
        try:
            return self.advance_once()
        except CircuitError as error:
            if (
                error.code != "NonlinearConvergence" or self.step is None
                or remaining_halvings == 0 or not self.transistors
            ):
                raise
        if self.half_step is None:
            self.half_step = TimeDomain(self.circuit, self.step / 2)
        half = self.half_step
        half.time = self.time
        half.state = self.state
        half.previous_capacitor_voltage = self.previous_capacitor_voltage
        half.advance(remaining_halvings - 1)
        voltage = half.advance(remaining_halvings - 1)
        self.time += self.step
        self.state = half.state
        self.previous_capacitor_voltage = half.previous_capacitor_voltage
        return voltage

    def advance_once(self):
        rhs = self.target.copy()
        for i, admittance in self.capacitors.items():
            rhs[:len(self.unknown)] += (
                self.incidence[i, self.unknown] * admittance * self.previous_capacitor_voltage[i]
            )
        for row, impedance in self.inductors.items():
            rhs[row] = -impedance * self.state[row]
        voltage = self.solve(rhs)
        self.previous_capacitor_voltage = self.incidence @ voltage
        if self.step is not None:
            self.time += self.step
        return voltage

    def operating_point(self):
        from .simulation import Network

        voltage = self.advance()
        branch_v = self.incidence @ voltage
        currents = self.direct_current + self.conductance * branch_v
        currents[self.voltage_rows] = self.state[len(self.unknown):]
        power = branch_v * currents
        for i, sign, b, c, e in self.transistors:
            values = sign * np.array([voltage[b] - voltage[e], voltage[b] - voltage[c]])
            (forward, reverse), _ = self.junctions(values / THERMAL_VOLTAGE)
            base = sign * (forward / FORWARD_GAIN + reverse / REVERSE_GAIN)
            collector = sign * (forward - reverse * (1 + 1 / REVERSE_GAIN))
            currents[i] = collector
            power[i] = branch_v[i] * collector + (voltage[b] - voltage[e]) * base
            self.base_current[self.parts[i].name] = base
        return Network(
            self.nodes, tuple(part.name for part in self.parts), voltage, branch_v, currents,
            power, None, references=self.references, base_current=self.base_current,
        )
