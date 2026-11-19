"""Live value edits retain circuit history instead of repeating startup transients."""
# Value-only edits restamp the existing topology instead of replaying startup conditions.
# Compare stored energy and elapsed time before/after reconfiguration and pause/resume.

from dataclasses import replace
import math
import unittest
from unittest.mock import patch

import numpy as np

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError


RC = "V1(5) between(vin,0);\nR1(1k) between(vin,out);\nC1(1u) between(out,0);"


def change(circuit, part_name, **values):
    return replace(circuit, components=[replace(part, **values) if part.name == part_name else part
                                        for part in circuit.components])


def branch_row(simulation, name):
    return len(simulation.unknown) + next(row for row, index in enumerate(simulation.voltage_rows)
                                          if simulation.parts[index].name == name)


class TimeDomainUpdatesTest(unittest.TestCase):
    def test_rc_value_and_step_edits_continue_from_charged_voltage(self):
        simulation = TimeDomain(compile_circuit(RC), 100e-6)
        for _ in range(9):
            voltage = simulation.advance()[simulation.nodes.index("out")]
        elapsed = simulation.time
        saved_state = simulation.state.copy()
        circuit = change(change(change(simulation.circuit, "R1", value=2000), "C1", value=2e-6), "V1", value=8)
        updated = simulation.reconfigure(circuit, 50e-6)
        self.assertEqual(updated.time, elapsed)
        capacitor, = updated.capacitors
        self.assertAlmostEqual(updated.previous_capacitor_voltage[capacitor], voltage)
        expected = (2e-6 / 50e-6 * voltage + 8 / 2000) / (2e-6 / 50e-6 + 1 / 2000)
        actual = updated.advance()[updated.nodes.index("out")]
        self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(updated.time, elapsed + 50e-6)
        np.testing.assert_array_equal(simulation.state, saved_state)
        self.assertEqual(simulation.time, elapsed)

    def test_reordered_rl_parts_preserve_current_and_node_guess(self):
        circuit = compile_circuit("V1(10) between(vin,0);\nR1(100) between(vin,out);\nL1(1) between(out,0);")
        simulation = TimeDomain(circuit, .001)
        for _ in range(8):
            simulation.advance()
        current = simulation.state[branch_row(simulation, "L1")]
        circuit = change(change(circuit, "R1", value=50), "L1", value=2)
        circuit = replace(circuit, components=list(reversed(circuit.components)))
        updated = simulation.reconfigure(circuit, .0005)
        self.assertAlmostEqual(updated.state[branch_row(updated, "L1")], current)
        old_voltages = dict(zip((simulation.nodes[i] for i in simulation.unknown), simulation.state))
        for row, index in enumerate(updated.unknown):
            self.assertEqual(updated.state[row], old_voltages[updated.nodes[index]])
        updated.advance()
        expected = (10 + 2 / .0005 * current) / (50 + 2 / .0005)
        self.assertAlmostEqual(updated.state[branch_row(updated, "L1")], expected)
        self.assertAlmostEqual(updated.time, simulation.time + .0005)

    def test_pot_end_stops_can_add_remove_rows_without_losing_storage(self):
        circuit = compile_circuit("V1(5) between(vin,0);\nP1(10k,25%) between(vin,tap,0);\n"
                                  "R1(1k) between(tap,out);\nC1(1u) between(out,0);\n"
                                  "L1(1) between(vin,rl);\nR2(100) between(rl,0);")
        simulation = TimeDomain(circuit, 100e-6)
        for _ in range(8):
            voltage = simulation.advance()[simulation.nodes.index("out")]
        for position in (.75, 0, 1, .5):
            with self.subTest(position=position):
                elapsed = simulation.time
                current = simulation.state[branch_row(simulation, "L1")]
                updated = simulation.reconfigure(change(circuit, "P1", position=position))
                self.assertEqual(updated.time, elapsed)
                self.assertEqual(updated.state[branch_row(updated, "L1")], current)
                resistance = 1000 + 10000 * position * (1 - position)
                expected = (1e-6 / updated.step * voltage + 5 * position / resistance) / (1e-6 / updated.step + 1 / resistance)
                voltage = updated.advance()[updated.nodes.index("out")]
                self.assertAlmostEqual(voltage, expected)
                self.assertAlmostEqual(updated.time, elapsed + simulation.step)
                simulation = updated

    def test_ac_amplitude_and_frequency_edits_preserve_phase_and_time(self):
        circuit = compile_circuit("f=1k;\nV1(2 Vpk) between(out,0);\nR1(1k) between(out,0);")
        simulation = TimeDomain(circuit, 123e-6)
        for _ in range(3):
            simulation.advance()
        elapsed = simulation.time
        updated = simulation.reconfigure(change(circuit, "V1", value=3 / math.sqrt(2)))
        actual = updated.advance()[updated.nodes.index("out")]
        self.assertAlmostEqual(actual, 3 * math.cos(math.tau * 1000 * (elapsed + updated.step)))
        circuit = replace(updated.circuit, known={**updated.circuit.known, "f": 750})
        phase = updated.source_phase(updated.time)
        changed_frequency = updated.reconfigure(circuit)
        self.assertAlmostEqual(changed_frequency.source_phase(updated.time), phase)
        angle = math.tau * 750 * updated.step
        actual = changed_frequency.advance()[changed_frequency.nodes.index("out")]
        self.assertAlmostEqual(actual, 3 * (phase * complex(math.cos(angle), math.sin(angle))).real)

    def test_topology_changes_are_rejected_without_touching_old_state(self):
        circuit = compile_circuit(RC)
        simulation = TimeDomain(circuit, 100e-6)
        simulation.advance()
        state, elapsed = simulation.state.copy(), simulation.time
        candidates = (change(circuit, "R1", p="out"), change(circuit, "R1", name="R2"),
                      change(circuit, "R1", kind="L"), replace(circuit, components=circuit.components[:-1]),
                      replace(circuit, grounds={"out"}))
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(CircuitError) as error:
                simulation.reconfigure(candidate)
            self.assertEqual(error.exception.code, "TopologyChanged")
        self.assertEqual(simulation.time, elapsed)
        np.testing.assert_array_equal(simulation.state, state)

    def test_invalid_value_leaves_old_simulation_usable(self):
        simulation = TimeDomain(compile_circuit(RC), 100e-6)
        simulation.advance()
        elapsed = simulation.time
        with self.assertRaises(CircuitError):
            simulation.reconfigure(change(simulation.circuit, "C1", value=None))
        self.assertEqual(simulation.time, elapsed)
        simulation.advance()
        self.assertAlmostEqual(simulation.time, elapsed + simulation.step)

    def test_adaptive_substeps_keep_live_frequency_phase_and_fresh_matrices(self):
        circuit = compile_circuit("f=1k;\nV1(2 Vpk) between(vin,0);\n"
                                  "switch1(vin,0,vin,out,-10,1000,1G);\nC1(100n) between(out,0);")
        simulation = TimeDomain(circuit, 100e-6)
        failure = CircuitError("Retry", code="NonlinearConvergence")
        with patch.object(simulation, "advance_once", side_effect=failure):
            simulation.advance()
        self.assertIsNotNone(simulation.half_step)
        circuit = replace(change(circuit, "C1", value=200e-9), known={**circuit.known, "f": 750})
        updated = simulation.reconfigure(circuit)
        self.assertIsNone(updated.half_step)
        reference = updated.reconfigure(circuit, updated.step / 2)
        reference.advance()
        expected = reference.advance()
        with patch.object(updated, "advance_once", side_effect=failure):
            actual = updated.advance()
        np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-12)
        self.assertAlmostEqual(updated.time, simulation.time + simulation.step)


if __name__ == "__main__":
    unittest.main()
