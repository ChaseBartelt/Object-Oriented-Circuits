"""Use the same pulse source in the built-in simulator and SPICE export."""

import unittest

from circuitlab.math.analysis import solve_input
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.exporters.spice import export_spice


SOURCE = """VClock(pulse(0, 5, 100u, 100u, 100u, 200u, 800u)) between(clk, 0);
RLoad(1k) between(clk, 0);
"""


class PulseSourceTest(unittest.TestCase):
    def test_initial_level_and_finite_edges(self):
        result = solve_input(SOURCE).network
        clk = result.nodes.index("clk")
        self.assertEqual(result.voltage[clk], 0)
        scope = TimeDomain(compile_circuit(SOURCE), 50e-6)
        samples = [scope.advance()[clk] for _ in range(19)]
        for index, expected in ((1, 0), (2, 2.5), (3, 5), (7, 5), (8, 2.5), (9, 0), (18, 2.5)):
            self.assertAlmostEqual(samples[index], expected)

    def test_exports_source_and_transient_directive(self):
        deck = export_spice(SOURCE)
        self.assertIn("VClock clk 0 PULSE(0 5 0.0001 0.0001 0.0001 0.0002 0.0008)", deck)
        self.assertIn(".tran 0 ", deck)

    def test_rejects_invalid_timing_and_ac_analysis(self):
        with self.assertRaisesRegex(CircuitError, "period that fits"):
            compile_circuit(SOURCE.replace("800u", "300u"))
        with self.assertRaisesRegex(CircuitError, "positive rise"):
            compile_circuit(SOURCE.replace("100u, 100u, 100u", "100u, 0, 100u"))
        with self.assertRaisesRegex(CircuitError, "DC mode"):
            compile_circuit(SOURCE, "ac")
