"""Modified nodal analysis with an interchangeable simulation backend."""
# Modified nodal analysis combines node-current equations with ideal voltage constraints.
# The same stamped system supports operating points and port equivalents; numerical scaling
# and rank checks distinguish contradictory constraints from a floating, nonunique solution.

import math

import numpy as np
from scipy import linalg

from circuitlab.model.graph import CircuitError, ground_references
from circuitlab.model.potentiometer import expand_potentiometers
from circuitlab.math.results import Network, SimulationBackend, fold_potentiometers


# Scale rows and columns before solving. A small singular value identifies a rank problem;
# the left-null-space test separates inconsistent equations from nonunique solutions.
def solve_system(matrix, rhs, scale):
    """Scale the system and distinguish inconsistent from non-unique solutions."""
    # matrix*x=rhs mixes node voltages and ideal-branch currents, whose
    # coefficients may differ greatly in size. scale contains nonnegative
    # magnitudes of the contributions before cancellation, not just |matrix|.
    # Reject overflow/NaN before passing unchecked arrays into SciPy.
    if not all(np.isfinite(x).all() for x in (matrix, rhs, scale)):
        raise CircuitError(
            "The circuit equations exceed the numeric range.", code="NumericalNetwork"
        )
    # An empty system has no unknowns; avoid reductions or SVD on an empty array.
    if not len(matrix):
        return np.empty(0, complex)
    # First normalize each equation, then each unknown column. Use 1 for an
    # all-zero row/column so a genuine rank defect remains zero rather than
    # turning into NaNs during scaling.
    row = np.max(scale, axis=1)
    row[row == 0] = 1
    column = np.max(scale / row[:, None], axis=0)
    column[column == 0] = 1
    # With Dr=diag(row) and Dc=diag(column), solve
    #     (Dr^-1 * matrix * Dc^-1) * z = Dr^-1 * rhs,  where z=Dc*x.
    # NumPy broadcasting applies the diagonal scalings without allocating
    # diagonal matrices. The final /column recovers physical unknowns x.
    a = matrix / row[:, None] / column
    b = rhs / row
    # Use a dimension-scaled multiple of double-precision roundoff as the
    # rank threshold after equilibration. No artificial conductance is added
    # to make a floating circuit appear solvable.
    tolerance = 16 * len(a) * np.finfo(float).eps
    # SVD provides singular values for rank detection and left singular
    # vectors for checking whether the right-hand side is attainable.
    left, singular, _ = linalg.svd(a, check_finite=False)
    if np.min(singular) <= tolerance:
        # Columns associated with tiny singular values approximate the left null
        # space. A consistent RHS must be orthogonal to all of them: U0^H*b=0.
        # The conjugate transpose is required for complex AC equations.
        null = left[:, singular <= tolerance]
        # Compare each projected residual to its own contribution-magnitude
        # scale |U0|^T*|b|. A significant nonzero projection means contradictory
        # equations, rather than merely an undetermined current or floating voltage.
        if np.any(np.abs(null.conj().T @ b) > tolerance * (np.abs(null).T @ np.abs(b))):
            raise CircuitError(
                "Ideal source or connection constraints conflict.",
                code="InconsistentNetwork",
            )
        # A rank-deficient system with a compatible RHS has no unique answer.
        # Redundant equal voltage sources, for example, can fix voltage while
        # leaving their individual currents indeterminate.
        raise CircuitError(
            "No unique circuit solution. Check for floating nodes or redundant ideal sources.",
            code="UnderdeterminedNetwork",
        )
    # Only the full-rank case reaches the direct solve. Its result is scaled
    # back to volts/amps; even finite coefficients can imply a result outside
    # the representable range, so check the recovered values too.
    answer = linalg.solve(a, b, check_finite=False) / column
    if not np.isfinite(answer).all():
        raise CircuitError(
            "The circuit solution exceeds the numeric range.", code="NumericalNetwork"
        )
    return answer


# This backend implements the shared solve() interface structurally;
# callers receive the same Network contract used by other solver backends.
class NodalBackend:
    """Solve linear DC or AC networks by modified nodal analysis."""

    # Expand multi-branch devices first, then stamp branch admittances and voltage constraints.
    # Native nonlinear devices use the time-domain operating-point solver instead of an
    # invalid linear approximation. Fold expanded devices back before returning public arrays.
    def solve(self, circuit, *, ac, frequency=None, port=None):
        # A schematic placeholder is missing data, not a zero-valued element.
        # Validate it before topology/frequency errors can obscure that problem.
        circuit.require_values()
        # Replace each potentiometer with its high/wiper and wiper/low legs.
        # Endpoint legs become exact zero-volt constraints. Keep the index map
        # so public currents/powers can later be folded into source component order.
        parts, potentiometers = expand_potentiometers(circuit)
        # Q is a native BJT and S a voltage-controlled switch. A manual schematic
        # switch is kind K, already represented by its selected contact resistance
        # and therefore handled by the ordinary linear stamps.
        nonlinear = any(part.kind in {"Q", "S"} for part in parts)
        # A specified complex Z is a frequency-domain element with no DC or
        # time-domain realization. Require a pure AC design and keep it separate
        # from native nonlinear devices; this code does not linearize their bias point.
        if any(part.kind == "Z" for part in parts):
            if not ac or circuit.mode != "ac":
                raise CircuitError("Theoretical impedance Z supports AC steady state only; "
                                   "use physical R/L/C components for DC or transient analysis.", code="UnsupportedComponent")
            if nonlinear:
                raise CircuitError("Theoretical impedance Z cannot be combined with native BJTs or switches: "
                                   "their native solver supports DC and time-domain analysis, not AC.", code="UnsupportedComponent")
        # Delegate supported DC bias calculations using the original circuit.
        # The time-domain backend handles its own device expansion and result
        # folding. Small-signal AC and port equivalents are not inferred here.
        if nonlinear:
            if ac or port or circuit.mode != "dc":
                raise CircuitError("Use DC for the built-in nonlinear operating point. "
                                   "Run AC time-domain waveforms in the Results oscilloscope; "
                                   "use SPICE for small-signal AC or port analysis.",
                                   code="UnsupportedComponent")
            from circuitlab.math.nonlinear import TimeDomain

            return TimeDomain(circuit).operating_point()
        # Branch arrays follow this expanded order. Names must be unique ignoring
        # case so result lookup and later folding cannot select the wrong branch.
        names = tuple(e.name for e in parts)
        if not parts or len({name.casefold() for name in names}) != len(names):
            raise CircuitError(
                "Enter elements with unique names and two nodes.", code="InvalidNetwork"
            )
        # At least one branch terminal must connect to a declared voltage return.
        # A reference appearing only on a voltage-sensing pin does not satisfy
        # this initial branch-connectivity check.
        if not any({e.p, e.n} & circuit.grounds for e in parts):
            raise CircuitError(
                "Connect the circuit to ground node 0.", code="UnderdeterminedNetwork"
            )
        # R and specified X/Z can be solved at their given admittances without a
        # frequency. Physical C/L need a positive frequency to form j*w*C or 1/(j*w*L).
        if ac and frequency is None and any(e.kind in "CL" for e in parts):
            raise CircuitError(
                "Enter frequency for AC capacitors and inductors.",
                code="MissingFrequency",
            )
        if frequency is not None and (not np.isfinite(frequency) or frequency <= 0):
            raise CircuitError("AC frequency must be positive.")
        # Use the compiled node order for all returned voltage arrays. Expanded
        # potentiometer legs reuse the existing high, wiper, and low node names.
        nodes = circuit.nodes
        index = {name: i for i, name in enumerate(nodes)}
        # The op amp's output branch conducts. Its input pins only sense voltage.
        # Reference domains follow branch p/n connections. Op-amp input and supply
        # pins do not electrically join domains through their sensing role.
        # Choose one anchor per connected domain; separate named returns are not
        # all forcibly shorted together or assigned the same physical potential.
        references = ground_references(
            circuit.grounds, ((part.p, part.n) for part in parts)
        )
        # Keep the unused standard reference out of the matrix. If 0 appears
        # only as an input pin, it is still a valid explicit voltage reference.
        references.setdefault("0", "0")
        # Remove only each domain's chosen reference voltage from the unknowns.
        # Other named-ground nodes can remain unknown voltages relative to that
        # anchor, which matters when their connection is open in a DC solve.
        unknown = [i for i, node in enumerate(nodes) if references.get(node) != node]
        # incidence has one row per node and one column per branch. For a branch
        # p->n it stores +1 at p and -1 at n, making branch voltage Vp-Vn and
        # positive branch current flow from p to n. Complex arrays serve both DC
        # and AC so their result shapes and sign conventions remain identical.
        incidence = np.zeros((len(nodes), len(parts)))
        y = np.zeros(len(parts), complex)
        currents = np.zeros(len(parts), complex)
        # Admittance y and prescribed currents start at zero. constrained lists
        # branches requiring an extra current unknown; senses and targets give
        # their voltage equations, which need not sense their own output terminals.
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
            # A mixed analysis solves its DC and AC systems separately. For the DC
            # pass, independent source branches use their stored offset part.dc;
            # the AC pass uses the phasor in part.value. Passive values are unchanged.
            value = (
                part.dc
                if circuit.mode == "mixed" and not ac and kind in "VI"
                else part.value
            )
            if kind not in {"R", "K", "C", "L", "X", "Z", "V", "I", "O"}:
                raise CircuitError(
                    f"Unsupported component type {kind}.", code="UnsupportedComponent"
                )
            # Physical resistances, manual-contact resistances, C, and L require
            # positive real values. A theoretical Z may instead have a negative real
            # part; signed real X is reserved for a reactance in AC.
            if kind in "RKCL" and (value.imag != 0 or value.real <= 0):
                raise CircuitError(f"{part.name} requires a positive real value.")
            if not ac and kind in "VI" and value.imag:
                raise CircuitError(f"{part.name} requires a real value in DC.")
            # Ideal voltage sources, op-amp outputs, and DC inductors cannot be
            # represented by a finite branch admittance. Give each a current unknown
            # and a voltage constraint. A DC inductor is an exact zero-volt branch.
            if kind in "VO" or (kind == "L" and not ac):
                constrained.append(k)
                row = incidence[:, k].copy()
                # An ideal op amp enforces V(in+)-V(in-)=0, while its unknown output
                # current flows through p/n. Its input pins draw no stamped current.
                # Optional supply pins do not add rail clipping to this ideal model.
                if kind == "O":
                    row[:] = 0
                    row[index[part.in_plus]] += 1
                    row[index[part.in_minus]] -= 1
                senses.append(row[unknown])
                # Independent voltage sources set their specified voltage difference;
                # an op-amp input difference and a DC inductor drop both target zero.
                targets.append(value if kind == "V" else 0)
            # A current source contributes its prescribed p->n current to the KCL
            # right-hand side; its branch voltage remains determined by the network.
            elif kind == "I":
                currents[k] = value
            # Resistor/contact current is y*(Vp-Vn), with y=1/R. Manual switch state
            # was already converted to a finite on/off resistance by the compiler.
            elif kind in {"R", "K"}:
                y[k] = 1 / value
            # Use the supplied complex impedance directly; changing the analysis
            # frequency does not reinterpret it as an inferred physical R/L/C model.
            # An inverse outside numeric range is a numerical error, not an open circuit.
            elif kind == "Z":
                if not value or not np.isfinite(value):
                    raise CircuitError(f"{part.name} requires a finite, nonzero AC impedance.")
                try:
                    y[k] = 1 / value
                except (OverflowError, ZeroDivisionError):
                    raise CircuitError("An element admittance is outside the numeric range.",
                                       code="NumericalNetwork") from None
            # Z=j*X gives y=1/(j*X)=-j/X. Positive X is inductive; negative X is
            # capacitive. Zero X would need a voltage constraint instead of this inverse.
            elif kind == "X":
                if not ac or not value or value.imag:
                    raise CircuitError(
                        f"{part.name} requires a nonzero real reactance in AC."
                    )
                y[k] = -1j / value
            # AC capacitor admittance is j*2*pi*f*C. In DC there is no C stamp, so
            # its default y=0 leaves it open with zero steady branch current.
            elif ac and kind == "C":
                y[k] = 1j * (math.tau * (frequency * value))
            # AC inductor admittance is -j/(2*pi*f*L). If the intermediate impedance
            # underflows to zero, leave y at zero for the explicit range check below
            # instead of accidentally treating it as a valid open or dividing by zero.
            elif ac and kind == "L":
                impedance = math.tau * (frequency * value)
                if impedance:
                    y[k] = -1j / impedance
            # Every finite conducting/AC reactive branch here must retain a finite,
            # nonzero admittance. Overflow or underflow must not silently change the
            # circuit topology by replacing a finite element with a short/open.
            if (kind in "RKXZ" or (ac and kind in "CL")) and (
                not y[k] or not np.isfinite(y[k])
            ):
                raise CircuitError(
                    "An element admittance is outside the numeric range.",
                    code="NumericalNetwork",
                )
        # Stamp KCL in the upper rows and source constraints in the lower rows.
        # Let A be the incidence rows for unknown node voltages, C the incidence
        # columns of voltage-defined branches, D their voltage-sensing rows, and
        # Y=diag(y). Solve for [v; j], with j the constrained-branch currents:
        #     [ A*Y*A^T   C ] [ v ] = [ -A*prescribed_currents ]
        #     [    D      0 ] [ j ]   [       targets          ]
        # The minus sign moves prescribed p->n currents out of the KCL left side.
        # For an op amp D senses its inputs, so D is not generally C^T.
        a = incidence[unknown]
        cols = a[:, constrained]
        rows = np.array(senses).reshape(len(constrained), len(unknown))
        zeros = np.zeros((len(constrained), len(constrained)))
        # Multiplying A*y scales each branch column by its admittance without
        # allocating Y. A is real incidence data, so ordinary transpose is correct
        # even though the admittances and unknowns can be complex.
        matrix = np.block([[(a * y) @ a.T, cols], [rows, zeros]])
        # Accumulate absolute contribution magnitudes separately from signed
        # stamps. Opposing contributions may cancel in matrix, but their original
        # size is still the appropriate reference for numerical equilibration.
        scale = np.block(
            [[(abs(a) * abs(y)) @ abs(a).T, abs(cols)], [abs(rows), zeros]]
        )
        try:
            result = solve_system(matrix, np.r_[-a @ currents, targets], scale)
        except CircuitError as error:
            # For DC rank failures, check a more specific return-path problem: a
            # named ground may belong to a reference domain topologically but connect
            # to it only through capacitors, which are open in the DC equations.
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
                # Select declared returns touching only capacitor branch terminals and
                # using some other node as their domain reference. Such a return was not
                # fixed to zero itself and has lost its DC path; retain that distinction
                # rather than merging all ground labels to repair the singular system.
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
        # Restore the complete node vector, including zero-valued reference
        # anchors. Ordinary branch currents combine prescribed source current
        # with admittance current; constrained currents come directly from MNA.
        voltages = np.zeros(len(nodes), complex)
        voltages[unknown] = result[: len(unknown)]
        branch_v = incidence.T @ voltages
        currents += y * branch_v
        currents[constrained] = result[len(unknown) :]
        # Passive sign convention: positive real power is absorbed when current
        # enters p. For RMS AC phasors this is complex power P+jQ; the same
        # formula reduces to signed V*I for real DC values.
        branch_power = branch_v * currents.conj()
        # Postprocessing can overflow even when node voltages/current unknowns
        # were finite, for example in V*conj(I). Reject unusable branch results.
        if not all(np.isfinite(x).all() for x in (branch_v, currents, branch_power)):
            raise CircuitError(
                "A branch result is outside the numeric range.", code="NumericalNetwork"
            )
        # Build the expanded result first. Potentiometer folding at every return
        # path restores source component ordering and preserves terminal currents.
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
        # The original solve supplies the open-port voltage. Compute the port
        # impedance with a separate unit-current experiment on the same linear
        # system; do not alter or overwrite the actual operating-point results.
        if port:
            if any(node not in index for node in port) or port[0] == port[1]:
                raise CircuitError(
                    "The equivalent port needs two distinct existing nodes."
                )
            # Voltages referenced to different independent domains cannot define the
            # requested two-terminal equivalent without an electrical connection.
            if references.get(port[0]) != references.get(port[1]):
                raise CircuitError("Use two port nodes in the same connected circuit.")
            p, n = (index[node] for node in port)
            # A unit test current with the independent sources zeroed gives Zth.
            # Inject 1 A into p and withdraw 1 A at n. Zero the rest of the RHS:
            # independent voltage sources become 0-V constraints and independent
            # current sources become 0 A. Retaining matrix keeps the passive elements
            # and dependent op-amp constraints active during this test.
            test = np.zeros(len(nodes))
            test[p], test[n] = 1, -1
            response = np.zeros(len(nodes), complex)
            response[unknown] = solve_system(
                matrix, np.r_[test[unknown], np.zeros(len(constrained))], scale
            )[: len(unknown)]
            # A 1-A test makes the port voltage difference numerically equal to Zth.
            # Use the original node solution for Vth; Vth/Zth gives the Norton current
            # when the impedance is finite and nonzero.
            z = response[p] - response[n]
            v = voltages[p] - voltages[n]
            # If almost equal test-node voltages cancel to a much smaller difference,
            # that residual may be below floating-point precision. Report the issue
            # instead of dividing by an unreliable near-zero impedance.
            if z and abs(z) <= 32 * np.finfo(float).eps * max(
                abs(response[p]), abs(response[n])
            ):
                network.equivalent_issue = (
                    "The port impedance is below numerical precision."
                )
                return fold_potentiometers(network, circuit, potentiometers)
            # An exactly zero port impedance still permits storing Vth and Zth, but
            # does not define a unique finite Norton/short-circuit current. Store None
            # for that current rather than dividing by zero.
            if not z:
                network.equivalent_issue = (
                    "Zero port impedance leaves no unique finite short-circuit current."
                )
            network.equivalent = {
                "voltage": v,
                "impedance": z,
                "current": v / z if z else None,
            }
        # Sum expanded leg powers and restore the original component names and
        # terminal-current interpretation before callers index public branch arrays.
        return fold_potentiometers(network, circuit, potentiometers)
