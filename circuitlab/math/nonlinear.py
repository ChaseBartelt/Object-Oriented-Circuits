"""Implicit time-domain simulation and nonlinear transistor operating points."""
# Time simulation retains capacitor voltage and inductor current between implicit steps.
# Nonlinear device currents are iterated against that stamped network, with bounded retries
# at difficult transitions rather than silently accepting an unconverged sample.

import math
import warnings

import numpy as np
from scipy import linalg

from circuitlab.model.graph import CircuitError, ground_references
from circuitlab.model.potentiometer import expand_potentiometers
from circuitlab.math.equations import solve_known
from circuitlab.math.results import Network, fold_potentiometers

# Generic room-temperature Ebers–Moll parameters.
# All native BJTs share these idealized parameters; named SPICE models
# are not evaluated here. Junction voltages are normalized by thermal
# voltage, and forward/reverse gains determine the associated base currents.
SATURATION_CURRENT = 1e-15
THERMAL_VOLTAGE = 0.02585
FORWARD_GAIN = 100
REVERSE_GAIN = 2
# Step refinement is bounded and is triggered by nonlinear convergence
# failure. It is not a local truncation-error estimator for waveform accuracy.
MAX_STEP_HALVINGS = 18
# Voltage-controlled switches use a smooth tanh transition around their
# threshold. This voltage scale sets its width, avoiding a discontinuous
# conductance jump in the Newton equations.
SWITCH_TRANSITION = 0.05


def source_frequency(circuit):
    """Resolve the shared AC source frequency without assuming a default waveform."""
    # DC sources need no oscillator. AC/mixed sources need one shared frequency
    # to turn their RMS phasors into instantaneous cosine waveforms.
    if circuit.mode == "dc":
        return None
    if circuit.mode not in {"ac", "mixed"}:
        raise CircuitError("Run time simulation on a connected DC, AC, or DC + AC circuit.")
    # Reuse reversible f, T, w inference without feeding unrelated circuit
    # quantities into that solve. Conflicting frequency descriptions are an
    # error, and a missing frequency is never replaced by an arbitrary default.
    known = {key: value for key, value in circuit.known.items() if key in {"f", "T", "w"}}
    values, notes, _ = solve_known("ac", known)
    if any("conflict" in note.lower() for note in notes):
        raise CircuitError("Resolve the conflicting AC frequency quantities before running.")
    if "f" not in values:
        raise CircuitError("Enter the AC frequency (f, T, or w) before running the oscilloscope.",
                           code="MissingFrequency")
    return values["f"]


class TimeDomain:
    """Stamp a circuit once and advance its capacitor and inductor state."""

    # Prepare fixed topology, branch indices, and matrix storage once so each sample only
    # updates source targets, history terms, and nonlinear device contributions.
    def __init__(self, circuit, step=None):
        # Validate schematic-only models and missing values before allocating a
        # numerical system, preserving the compiler's useful source diagnostics.
        circuit.require_models()
        circuit.require_values()
        self.circuit = circuit
        # Expand potentiometers into electrical legs, retaining a map for reports.
        # The generated leg count/type can change at end stops, so live state
        # transfer below uses identities rather than assuming fixed array indexes.
        self.parts, self.potentiometers = expand_potentiometers(circuit)
        self.nodes = circuit.nodes
        # step=None builds a DC-bias system: capacitors are open and inductors
        # have zero voltage drop. A positive step h builds backward-Euler storage
        # companions; their history terms are supplied on each advance.
        self.step = step
        if step is not None and (not math.isfinite(step) or step <= 0):
            raise CircuitError("Enter a positive time step.")
        self.time = 0.0
        # Keep oscillator phase separate from elapsed time so a frequency edit
        # can preserve the phase at the edit instant instead of restarting it.
        self.phase_offset = 0.0
        # A specified frequency-domain impedance has no native time evolution.
        # The native model accepts physical R/C/L plus its supported ideal sources
        # and devices; it does not invent a realization for a theoretical Z.
        if any(part.kind == "Z" for part in self.parts):
            raise CircuitError("Theoretical impedance Z has no time-domain model. "
                               "Use physical R/L/C components for transient or nonlinear simulation.", code="UnsupportedComponent")
        if not self.parts:
            raise CircuitError("Run time simulation on a connected circuit.")
        self.frequency = source_frequency(circuit)
        if len({part.name.casefold() for part in self.parts}) != len(self.parts):
            raise CircuitError("Give each component a unique name before simulating.")
        if any(part.kind not in {"R", "K", "C", "L", "V", "I", "O", "Q", "S"} for part in self.parts):
            raise CircuitError("Use R, C, L, voltage/current sources, ideal op amps, BJTs, or switches in time simulation.")
        if any(part.kind == "Q" and part.model for part in self.parts):
            raise CircuitError("Named BJT models require SPICE. Omit the model to use the built-in generic transistor.")
        if not any(set(part.terminal_nets) & circuit.grounds for part in self.parts):
            raise CircuitError("Connect the circuit to a ground node.")
        # Choose one voltage reference per detected domain. Q/S terminal sets
        # participate in that domain detection; ideal op amps use only their
        # output branch p/n here, so sensing pins do not join return domains.
        self.references = ground_references(circuit.grounds, (
            part.terminal_nets if part.kind in {"Q", "S"} else (part.p, part.n)
            for part in self.parts
        ))
        self.references.setdefault("0", "0")
        self.unknown = [i for i, net in enumerate(self.nodes) if self.references.get(net) != net]
        indices = {net: i for i, net in enumerate(self.nodes)}
        # Here incidence is branch-by-node: each row gives Vp-Vn when multiplied
        # by the full node-voltage vector. This orientation also makes capacitor
        # history arrays naturally indexable by expanded component position.
        self.incidence = np.zeros((len(self.parts), len(self.nodes)))
        # State order is [non-reference node voltages; currents through V/O/L].
        # Voltage sources, ideal op-amp outputs, and inductors require branch-current
        # unknowns. rows maps their component indexes to full matrix/state positions.
        self.voltage_rows = [
            i for i, part in enumerate(self.parts) if part.kind in {"V", "O", "L"}
        ]
        rows = {i: len(self.unknown) + n for n, i in enumerate(self.voltage_rows)}
        size = len(self.unknown) + len(rows)
        if not size:
            raise CircuitError("Connect the circuit to at least one non-ground node.")
        # The fixed real matrix contains linear KCL and voltage constraints.
        # target is the fixed source RHS. Per-step waveforms/history and nonlinear
        # device currents are evaluated separately rather than accumulating into it.
        self.matrix = np.zeros((size, size))
        self.target = np.zeros(size)
        # capacitors maps component index -> C/h; inductors maps current row -> L/h.
        # Waveform dictionaries identify which RHS rows or KCL branches change.
        # Transistor/switch records hold their fixed voltage/current projection data.
        self.capacitors = {}
        self.inductors = {}
        self.pulses = {}
        self.ac_voltage = {}
        self.ac_current = {}
        self.transistors = []
        self.switches = []
        self.direct_current = np.zeros(len(self.parts))
        self.conductance = np.zeros(len(self.parts))
        self.base_current = {}
        inputs, outputs = [], []

        # Each branch current is positive from p to n. A reduced incidence row
        # projects node unknowns to branch voltage and branch current back to KCL.
        # For a BJT, p/n are collector/emitter; its base is handled separately.
        for i, part in enumerate(self.parts):
            p, n = indices[part.p], indices[part.n]
            self.incidence[i, p] += 1
            self.incidence[i, n] -= 1
            line = self.incidence[i, self.unknown]
            # Ordinary two-terminal branches need distinct nodes. Nonlinear devices
            # may have tied terminals, whose projections cancel through the +=/-= stamps.
            if part.kind not in {"Q", "S"} and p == n:
                raise CircuitError(f"{part.label} needs two distinct nodes.")
            value = part.value
            if part.kind in "RKCL" and (value.imag or not math.isfinite(value.real) or value.real <= 0):
                raise CircuitError(f"{part.label} requires a valid real component value.")
            source = value.real
            # Sources are real DC values or RMS AC phasors plus a real mixed-mode
            # offset. Store sqrt(2)*phasor as a complex peak coefficient; its real
            # projection at the step endpoint supplies the instantaneous waveform.
            if part.kind in "VI":
                if not all(math.isfinite(v) for v in (value.real, value.imag, part.dc.real, part.dc.imag)):
                    raise CircuitError(f"{part.label} requires finite source values.")
                if (self.frequency is None and value.imag) or (circuit.mode == "mixed" and part.dc.imag):
                    raise CircuitError(f"{part.label} requires a real DC value.")
                if self.frequency is not None:
                    source = part.dc.real if circuit.mode == "mixed" else 0
                    peak = math.sqrt(2) * value
                    if not math.isfinite(peak.real) or not math.isfinite(peak.imag):
                        raise CircuitError(f"{part.label} peak source value exceeds the numeric range.")
            # Sense in_plus minus in_minus without assigning input-pin current.
            # The ideal op amp constrains this voltage to zero; a controlled switch
            # uses it to determine output conductance. Op-amp supply pins do not clip output.
            if part.kind in {"O", "S"}:
                sense = np.zeros(len(self.nodes))
                sense[indices[part.in_plus]] += 1
                sense[indices[part.in_minus]] -= 1
            # R and manual switch K contribute G=1/R. For a capacitor and finite h,
            # backward Euler gives i_new=(C/h)*(v_new-v_old): stamp C/h here and
            # place +(C/h)*v_old on the KCL RHS during advance_once().
            if part.kind in "RKC":
                admittance = 1 / value.real if part.kind in {"R", "K"} else (value.real / step if step else 0)
                self.conductance[i] = admittance
                self.matrix[:len(line), :len(line)] += admittance * np.outer(line, line)
                if step and part.kind == "C":
                    self.capacitors[i] = admittance
            # Move a prescribed p->n current to the KCL RHS with a minus sign.
            # direct_current retains its DC contribution for operating-point reports.
            elif part.kind == "I":
                self.target[:len(line)] -= source * line
                self.direct_current[i] = source
                if self.frequency is not None and value:
                    self.ac_current[i] = peak
            # A voltage-defined branch adds its unknown current to node KCL. Its
            # extra equation is either a specified branch voltage, ideal op-amp input
            # equality, or the implicit inductor relation.
            elif i in rows:
                row = rows[i]
                self.matrix[:len(line), row] = line
                if part.kind == "O":
                    self.matrix[row, :len(line)] = sense[self.unknown]
                else:
                    self.matrix[row, :len(line)] = line
                    if part.kind == "V":
                        self.target[row] = source
                        if self.frequency is not None and value:
                            self.ac_voltage[row] = peak
                        if part.pulse is not None:
                            self.pulses[row] = part.pulse
                    # For an inductor, v_new=(L/h)*(i_new-i_old). The row therefore has
                    # coefficient -L/h on i_new and later RHS -(L/h)*i_old. With no step,
                    # the diagonal stays zero and this becomes the exact DC short constraint.
                    elif step:
                        self.inductors[row] = value.real / step
                        self.matrix[row, row] = -self.inductors[row]
            # Pad voltage projections to the complete state size. Their auxiliary
            # current entries are zero: switch control/output voltages read node
            # voltages, while branch injects the computed output current into KCL.
            elif part.kind == "S":
                control = np.zeros(size)
                control[:len(line)] = sense[self.unknown]
                branch = np.zeros(size)
                branch[:len(line)] = line
                self.switches.append((i, branch, control))
            # Build base-emitter and base-collector voltage tests. Multiplying both
            # voltages and current-flow vectors by sign lets the same junction model
            # handle NPN (+1) and PNP (-1), including tied transistor pins.
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
                # Each junction current contributes collector/emitter transport plus
                # gain-limited base current. These fixed vectors distribute it among
                # the nodal KCL equations; their terminal currents sum to zero.
                flows = np.zeros((2, size))
                flows[:, :len(line)] = sign * np.array([
                    line + be[self.unknown] / FORWARD_GAIN,
                    -line + bc[self.unknown] / REVERSE_GAIN,
                ])
                outputs.extend(flows)
                self.transistors.append((i, sign, indices[part.base], p, n))

        # For N BJTs, inputs has shape (2*N, size) and outputs (size, 2*N).
        # inputs@state is already normalized junction voltage Vjunction/VT.
        # outputs@junction_current then adds all transistor KCL terms at once.
        # Empty dimensions keep the same algebra valid for circuits without BJTs.
        self.inputs = np.asarray(inputs).reshape(len(inputs), size) / THERMAL_VOLTAGE
        self.outputs = np.asarray(outputs).reshape(len(outputs), size).T
        # Start node/current guesses and capacitor history at zero. Node entries
        # are Newton guesses; inductor-current entries also carry physical history.
        # A capacitor's initial voltage can be set independently before advancing.
        self.state = np.zeros(size)
        self.previous_capacitor_voltage = np.zeros(len(self.parts))
        self.half_step = None

    # Compare named terminal identities rather than declaration order, allowing value-only
    # source edits to preserve physical simulation history.
    def same_topology(self, circuit):
        """Value edits may reorder declarations, but must retain named connections."""
        # Compare original component names, kinds, and terminal tuples after sorting.
        # This permits declaration reordering and value/position edits while rejecting
        # changed connections or reference declarations.
        def terminals(model):
            return sorted((part.name, part.kind, part.terminal_nets) for part in model.components)
        return circuit.grounds == self.circuit.grounds and terminals(circuit) == terminals(self.circuit)

    # Reuse stored capacitor voltage and inductor current only when the topology agrees.
    # A source/value change must not invent a new initial condition for an already running circuit.
    def reconfigure(self, circuit, step=None):
        """Restamp values while retaining physical history; call between sample batches.

        Capacitor voltage and inductor current are carried into the new equations.
        Generated potentiometer short branches may appear or disappear at end stops,
        so state is transferred by node/component identity rather than array index.
        """
        if not self.same_topology(circuit):
            raise CircuitError("Circuit connections changed. Reset the oscilloscope to run the new circuit.",
                               code="TopologyChanged")
        # Restamp into a separate object so validation failures leave this running
        # simulation usable. New matrix sizes/companion coefficients may differ;
        # copy only compatible physical history after construction succeeds.
        updated = TimeDomain(circuit, self.step if step is None else step)
        # Stored voltages are relative to these anchors. Identical terminal names
        # alone are insufficient if restamping selects different voltage references.
        if self.references != updated.references:
            raise CircuitError("Circuit ground references changed. Reset the oscilloscope.", code="TopologyChanged")
        updated.time = self.time
        updated.phase_offset = self.phase_offset
        # Maintain theta=offset+2*pi*f*time at the edit instant:
        # new_offset=old_offset+2*pi*(old_f-new_f)*time, modulo a full turn.
        # Subsequent steps use the new rate without a phase reset.
        if self.frequency is not None and updated.frequency is not None:
            updated.phase_offset = math.remainder(
                self.phase_offset + math.tau * (self.frequency - updated.frequency) * self.time, math.tau,
            )
        # Remap node guesses by net name, not by the old unknown ordering.
        # Equal reference maps ensure both systems use the same voltage origins.
        voltages = {self.nodes[index]: self.state[row] for row, index in enumerate(self.unknown)}
        for row, index in enumerate(updated.unknown):
            updated.state[row] = voltages[updated.nodes[index]]
        # Auxiliary rows may move, appear, or disappear when a potentiometer leg
        # becomes an exact short. Preserve matching branch currents by name/type;
        # new voltage-constraint currents can start at zero as Newton guesses.
        currents = {(self.parts[index].name, self.parts[index].kind): self.state[len(self.unknown) + row]
                    for row, index in enumerate(self.voltage_rows)}
        for row, index in enumerate(updated.voltage_rows):
            part = updated.parts[index]
            updated.state[len(updated.unknown) + row] = currents.get((part.name, part.kind), 0.0)
        # Carry capacitor voltage by component name even when branch indexes move.
        # Inductor currents were preserved in the auxiliary-row transfer above.
        # Changing C/L preserves voltage/current history, not necessarily stored energy.
        capacitors = {self.parts[index].name: self.previous_capacitor_voltage[index] for index in self.capacitors}
        for index in updated.capacitors:
            updated.previous_capacitor_voltage[index] = capacitors.get(updated.parts[index].name, 0.0)
        return updated

    # Set the pre-step capacitor history used by the next implicit equation.
    # This does not solve the current node voltages or overwrite a node guess.
    # Only capacitors with finite-step companion stamps are selectable.
    def set_initial_voltage(self, capacitor, voltage):
        for i in self.capacitors:
            if capacitor in {self.parts[i].name, self.parts[i].label}:
                if not math.isfinite(voltage):
                    break
                self.previous_capacitor_voltage[i] = voltage
                return
        raise CircuitError("Choose a capacitor and a finite initial voltage.")

    # values is normalized voltage u=Vjunction/VT. Return junction current
    # IS*(exp(u)-1) and its slope with respect to u, not directly to volts.
    # The chain-rule voltage scaling is already included in self.inputs.
    @staticmethod
    def junctions(values):
        # A linear extension above the exponential limit keeps Newton finite.
        # Above u=40, continue along the exponential's tangent instead of clipping
        # current or exponentiating arbitrarily large values. Current and slope
        # remain continuous at the join. expm1 also preserves accuracy near u=0.
        limited = np.minimum(values, 40)
        slope = SATURATION_CURRENT * np.exp(limited)
        return SATURATION_CURRENT * np.expm1(limited) + slope * (values - limited), slope

    # Return output current and its gradient with respect to the full state.
    # The tanh blends off/on conductances, so threshold is the midpoint in
    # conductance rather than the midpoint between the two resistance values.
    def switch_current(self, i, branch, control, state):
        part = self.parts[i]
        fraction = np.tanh((control @ state - part.threshold) / SWITCH_TRANSITION)
        low, high = 1 / part.off_resistance, 1 / part.value
        conductance = low + (high - low) * (1 + fraction) / 2
        voltage = branch @ state
        # Differentiate I=G(Vcontrol)*Vbranch through both voltage dependencies:
        # dI/dstate = G*branch + (dG/dVcontrol)*Vbranch*control.
        derivative = (high - low) * (1 - fraction ** 2) / (2 * SWITCH_TRANSITION)
        return conductance * voltage, conductance * branch + derivative * voltage * control

    # Solve F(x)=M*x + outputs*j(inputs*x) + switch_KCL(x) - rhs = 0.
    # The optional Jacobian combines M with device derivatives. Current-flow
    # projections have zero auxiliary-row entries, so nonlinear currents
    # modify node KCL without replacing ideal voltage equations.
    def evaluate(self, state, matrix, rhs, *, jacobian=False):
        currents, slope = self.junctions(self.inputs @ state)
        residual = matrix @ state + self.outputs @ currents - rhs
        # Broadcast slope over the outputs columns, then multiply by inputs for
        # the junction chain rule. This avoids forming a diagonal slope matrix.
        derivative = matrix + (self.outputs * slope) @ self.inputs if jacobian else None
        for i, branch, control in self.switches:
            current, gradient = self.switch_current(i, branch, control, state)
            residual += current * branch
            if jacobian:
                # The output branch distributes current into KCL; gradient describes how
                # that current changes with each state unknown, giving an outer-product stamp.
                derivative += np.outer(branch, gradient)
        return residual, derivative

    # Require the nonlinear residual to converge before treating the candidate as a valid sample.
    def iterate(self, state, rhs, leakage=0.0, *, matrix=None):
        # Work on a local candidate: accepted Newton trials rebind state rather
        # than editing the caller's array in place. None means the attempt failed;
        # self.state is committed by solve() only after convergence.
        count = len(self.unknown)
        matrix = self.matrix if matrix is None else matrix
        # Temporary gmin conductance shunts node-voltage unknowns to their voltage
        # reference, helping difficult bias searches. Copy the matrix so this
        # regularization does not accumulate in the permanent circuit stamp.
        if leakage:
            matrix = matrix.copy()
            matrix[np.arange(count), np.arange(count)] += leakage
        # Newton solves J*delta=-F for a correction. Bound iteration count so a
        # failed local solve returns control to bias initialization or step retry.
        for _ in range(130):
            values = self.inputs @ state
            residual, jacobian = self.evaluate(state, matrix, rhs, jacobian=True)
            error = np.linalg.norm(residual, np.inf)
            try:
                with warnings.catch_warnings():
                    # An ill-conditioning warning alone does not decide convergence. Invalid
                    # or singular solves abandon this attempt; finite corrections must still
                    # pass both residual and update-size checks below.
                    warnings.simplefilter("ignore", linalg.LinAlgWarning)
                    delta = linalg.solve(jacobian, -residual, check_finite=False)
            except (ValueError, linalg.LinAlgError):
                break
            if not np.isfinite(delta).all():
                break
            # Require a small equation residual and a small correction. Either alone
            # could accept a poor state near a steep junction or a weakly determined node.
            if error < 2e-10 and np.linalg.norm(delta, np.inf) < 1e-7:
                return state
            # Limit growth of a forward junction. Reverse-biased movement
            # and steps that turn a junction off can proceed freely.
            change = self.inputs @ delta
            # Limit positive changes in normalized junction voltage using the current
            # bias; negative changes are not restricted by this limiter. With no BJTs,
            # the empty reduction's initial=1 leaves a full Newton step available.
            available = np.maximum(8, 26 - values)
            scale = 1 / np.max(np.maximum(change / available, 1), initial=1)
            # Backtrack by halving the proposed correction. Accept non-increasing
            # residual within tiny roundoff slack, or a residual already below tolerance.
            # Failure to find an acceptable trial rejects this Newton attempt.
            for _ in range(14):
                trial = state + scale * delta
                trial_error = np.linalg.norm(self.evaluate(trial, matrix, rhs)[0], np.inf)
                if trial_error < error * (1 + 1e-8) or trial_error < 2e-10:
                    state = trial
                    break
                scale /= 2
            else:
                break
        return None

    # Try the last accepted state first. DC-only capacitor initialization and
    # gmin continuation are fallback starting strategies; they do not replace
    # the requirement to converge against the original equations.
    def solve(self, rhs):
        state = self.iterate(self.state, rhs)
        if state is None and self.step is None:
            state = self.bias_from_capacitors(rhs)
        if state is None:
            state = self.state
            # Warm-start successively weaker shunts, ending with exactly zero leakage.
            # Every continuation stage must converge; the returned state therefore
            # satisfies the unmodified circuit equations, not a permanently leaky model.
            for leakage in (1e-2, 1e-3, 1e-4, 1e-5, 1e-7, 1e-9, 0):
                state = self.iterate(state, rhs, leakage)
                if state is None:
                    raise CircuitError(
                        "The built-in nonlinear solver could not find an operating point."
                        if self.step is None else
                        f"The built-in nonlinear solver could not advance past {self.time * 1000:.4g} ms.",
                        code="NonlinearConvergence",
                    )
        # Commit only a converged state and restore omitted reference-node voltages
        # as zero in the full node array returned to the sampler.
        self.state = state
        voltage = np.zeros(len(self.nodes))
        voltage[self.unknown] = state[:len(self.unknown)]
        return voltage

    def bias_from_capacitors(self, rhs):
        """Use one implicit capacitor step as a starting guess for the DC equations."""
        count = len(self.unknown)
        matrix = self.matrix.copy()
        # Temporary C/bias_step conductances act like one implicit step from zero
        # capacitor history. This can supply a better initial bias guess where
        # the open-capacitor DC system is hard to approach from all-zero state.
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
        # Remove the temporary capacitor conductances and solve the original DC
        # equations again. The bias-step state alone is never accepted as the DC result.
        return None if guess is None else self.iterate(guess, rhs)

    # Difficult transitions may need smaller implicit steps. Bound retries so an oscillating
    # or unsolvable numerical state cannot block the UI indefinitely.
    def advance(self, remaining_halvings=MAX_STEP_HALVINGS):
        """Advance by the requested interval, retrying difficult switching edges."""
        # Only a convergence failure in a finite-step nonlinear circuit triggers
        # refinement. Other errors propagate directly; this retry path is not a
        # general accuracy controller or a way to repair invalid circuit data.
        try:
            return self.advance_once()
        except CircuitError as error:
            if (
                error.code != "NonlinearConvergence" or self.step is None
                or remaining_halvings == 0 or not (self.transistors or self.switches)
            ):
                raise
        # Cache a freshly stamped h/2 simulator. C/h and L/h depend on step size,
        # so retrying with a smaller time increment alone would use wrong equations.
        if self.half_step is None:
            self.half_step = TimeDomain(self.circuit, self.step / 2)
        half = self.half_step
        # Start the child from this interval's accepted time, oscillator phase,
        # source targets, and storage history. Newton/advance replace state/history
        # arrays rather than mutating these shared starting arrays in place.
        half.time = self.time
        half.phase_offset = self.phase_offset
        half.target[:] = self.target
        half.state = self.state
        half.previous_capacitor_voltage = self.previous_capacitor_voltage
        # Both half intervals must succeed, with recursive refinement if needed.
        # The second begins from the first's committed state. Commit their endpoint
        # back to the parent only after the whole requested interval completes.
        half.advance(remaining_halvings - 1)
        voltage = half.advance(remaining_halvings - 1)
        self.time += self.step
        self.state = half.state
        self.previous_capacitor_voltage = half.previous_capacitor_voltage
        return voltage

    def advance_once(self):
        # Build a fresh endpoint RHS, leaving fixed source targets untouched.
        # Backward Euler evaluates drives at t+h; a DC operating-point solve with
        # step=None uses the present time without advancing the simulation clock.
        rhs = self.target.copy()
        phase = self.source_phase(self.time + (self.step or 0))
        # For RMS phasor V, the instantaneous waveform is
        # sqrt(2)*Re(V*exp(j*theta)). Stored peak coefficients already include
        # sqrt(2) and the source's own phase; mixed DC offsets remain in target.
        for row, peak in self.ac_voltage.items():
            rhs[row] += (peak * phase).real
        # Current-source waveforms use the same cosine phase, with the p->n KCL
        # sign used by the fixed DC component.
        for i, peak in self.ac_current.items():
            rhs[:len(self.unknown)] -= self.incidence[i, self.unknown] * (peak * phase).real
        # A pulse voltage sets the source target at this actual step endpoint.
        # During a half-step retry, each child therefore samples its own t+h/2,
        # not the failed parent step's waveform value.
        if self.step is not None:
            for row, pulse in self.pulses.items():
                rhs[row] = pulse.at(self.time + self.step)
        # Move -(C/h)*v_old from the capacitor companion current to the KCL RHS.
        # History is branch voltage, so capacitors between two non-ground nodes
        # retain the correct differential voltage as well.
        for i, admittance in self.capacitors.items():
            rhs[:len(self.unknown)] += (
                self.incidence[i, self.unknown] * admittance * self.previous_capacitor_voltage[i]
            )
        # The inductor row reads v_new-(L/h)*i_new=-(L/h)*i_old.
        # Its old current is already stored at the same auxiliary state row.
        for row, impedance in self.inductors.items():
            rhs[row] = -impedance * self.state[row]
        voltage = self.solve(rhs)
        # After a successful solve, store the new branch voltages for the next
        # interval. This array covers all branches for simple indexing; only
        # capacitor entries are read as capacitor history. Then advance time.
        self.previous_capacitor_voltage = self.incidence @ voltage
        if self.step is not None:
            self.time += self.step
        return voltage

    def source_phase(self, time):
        # AC graph values are RMS phasors with the same cosine convention as reports.
        # The shared oscillator contributes exp(j*theta); individual source phase
        # is carried by its complex peak coefficient. Frequency/phase are sampled
        # at the supplied time, including intermediate adaptive endpoints.
        angle = self.phase_offset + math.tau * self.frequency * time if self.frequency is not None else 0
        return complex(math.cos(angle), math.sin(angle))

    # The DC backend calls this on a simulator with step=None: capacitors
    # are open, inductors are shorts, and advance() does not consume time.
    # Wrap the solved bias in the common Network result, including currents
    # that are not directly available as linear branch admittances.
    def operating_point(self):
        voltage = self.advance()
        branch_v = self.incidence @ voltage
        # Recover prescribed current-source and resistive currents first.
        # Voltage-source/op-amp/inductor currents come from auxiliary unknowns;
        # controlled switches and BJTs are evaluated from their device laws below.
        currents = self.direct_current + self.conductance * branch_v
        phase = self.source_phase(self.time)
        for i, peak in self.ac_current.items():
            currents[i] += (peak * phase).real
        currents[self.voltage_rows] = self.state[len(self.unknown):]
        power = branch_v * currents
        for i, branch, control in self.switches:
            currents[i], _ = self.switch_current(i, branch, control, self.state)
            power[i] = branch_v[i] * currents[i]
        # Report a BJT's branch current as current entering its collector.
        # Store the separately signed base current too; PNP uses the same terminal
        # directions with the polarity transform applied to voltages and currents.
        for i, sign, b, c, e in self.transistors:
            values = sign * np.array([voltage[b] - voltage[e], voltage[b] - voltage[c]])
            (forward, reverse), _ = self.junctions(values / THERMAL_VOLTAGE)
            base = sign * (forward / FORWARD_GAIN + reverse / REVERSE_GAIN)
            collector = sign * (forward - reverse * (1 + 1 / REVERSE_GAIN))
            currents[i] = collector
            # A three-terminal transistor's absorbed power is Vce*Ic+Vbe*Ib.
            # Using only its collector/emitter branch power would omit base input power.
            power[i] = branch_v[i] * collector + (voltage[b] - voltage[e]) * base
            self.base_current[self.parts[i].name] = base
        network = Network(
            self.nodes, tuple(part.name for part in self.parts), voltage, branch_v, currents,
            power, None, references=self.references, base_current=self.base_current,
        )
        # Fold generated leg currents/powers back into original component order,
        # retaining potentiometer terminal-current details for probes and reports.
        return fold_potentiometers(network, self.circuit, self.potentiometers)
