"""Analytical common-mode gain/loading and rail-relative validity checks."""

import unittest

import numpy as np

from circuitlab.exporters.spice import export_spice
from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import pin_rows, result_rows
from circuitlab.model.graph import CircuitError


SOURCE = """V1(1V) between(input,0);
Vp(5V) between(pos,0);
Vn(-5V) between(neg,0);
op1(input,out,out,pos,neg);
analog op1 {
    common.min = vminus + 3V;
    common.max = vplus;
    common.rejection = 100dB;
    common.impedance = 1G;
}
"""


class CommonModeTest(unittest.TestCase):
    def test_rejection_and_input_loading_match_both_backends(self):
        circuit = compile_circuit(SOURCE)
        k = 10 ** (-100 / 20)
        expected = (1 + k / 2) / (1 - k / 2)
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], expected)
            self.assertAlmostEqual(network.branch_current[network.names.index("V1")], -1e-9)
            self.assertAlmostEqual(network.terminal_current["op1"][1], expected / 1e9)
            self.assertAlmostEqual(sum(network.terminal_current["op1"]), 0)
            self.assertAlmostEqual(sum(network.branch_power), 0)

    def test_impedance_loads_the_external_source_and_accounts_for_terminal_power(self):
        circuit = compile_circuit("""V1(1V) between(source,0);
            R1(1k) between(source,input);
            op1(input,out,out);
            analog op1 {common.impedance=1k;}
        """)
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.voltage[network.nodes.index("input")], .5)
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], .5)
            self.assertAlmostEqual(network.terminal_current["op1"][0], .0005)
            self.assertAlmostEqual(sum(network.branch_power), 0)

    def test_ac_cmrr_combines_with_gain_bandwidth(self):
        circuit = compile_circuit("V1(1Vrms) between(input,0);\nop1(input,out,out);\n"
                                  "analog op1 {gain=1000; gbw=1kHz; common.rejection=40dB; common.impedance=1G;}\n")
        network = NodalBackend().solve(circuit, ac=True, frequency=100)
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], (1+.01/2)/(1-.01/2+.001+.1j))
        self.assertAlmostEqual(sum(network.branch_power), 0)

    def test_rail_relative_bounds_report_invalid_operation_in_both_backends(self):
        circuit = compile_circuit(SOURCE.replace("V1(1V)", "V1(-3V)"))
        for solve in (lambda: NodalBackend().solve(circuit, ac=False), lambda: TimeDomain(circuit).operating_point()):
            with self.assertRaisesRegex(CircuitError, "outside.*declared range") as caught:
                solve()
            self.assertEqual(caught.exception.code, "CommonModeRange")
            self.assertEqual(caught.exception.line, 4)

    def test_reversed_bounds_are_invalid_at_actual_supply_voltages(self):
        circuit = compile_circuit(SOURCE.replace("vminus + 3V", "vplus + 1V"))
        with self.assertRaisesRegex(CircuitError, "common.min exceeds common.max"):
            NodalBackend().solve(circuit, ac=False)

    def test_constant_bounds_do_not_require_numerical_supply_nodes(self):
        for pins in ("input,out,out", "input,out,out,unused_high,unused_low"):
            circuit = compile_circuit(f"V1(1) between(input,0);\nop1({pins});\n"
                                      "analog op1 {common.min=0V; common.max=1V;}\n")
            self.assertAlmostEqual(NodalBackend().solve(circuit, ac=False).voltage[circuit.nodes.index("out")], 1)

    def test_only_the_rail_referenced_by_a_bound_becomes_a_numerical_node(self):
        circuit = compile_circuit("V1(1V) between(input,0);\nVp(5V) between(high,0);\n"
                                  "op1(input,out,out,high,unused_low);\nanalog op1 {common.max=vplus;}\n")
        self.assertNotIn("unused_low", circuit.nodes)
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 1)

    def test_failed_transient_range_check_does_not_commit_state_or_time(self):
        circuit = compile_circuit(SOURCE.replace("V1(1V)", "V1(-3V)"))
        sim = TimeDomain(circuit, .001)
        before = sim.state.copy()
        with self.assertRaisesRegex(CircuitError, "common-mode"):
            sim.advance()
        self.assertEqual(sim.time, 0)
        np.testing.assert_array_equal(sim.state, before)

    def test_spice_retains_input_loads_rejection_and_validity_comments(self):
        netlist = export_spice(SOURCE)
        self.assertIn("Rcommon_plus plus return 1000000000", netlist)
        self.assertIn("Rcommon_minus minus return 1000000000", netlist)
        self.assertIn("1e-05*(V(plus,return)+V(minus,return))/2", netlist)
        self.assertIn("* common.min", netlist)
        self.assertIn("checked by native DC/time simulation", netlist)

    def test_results_and_pin_reports_include_input_loading(self):
        result = analyze(compile_circuit(SOURCE))
        rows = result_rows(result)
        self.assertTrue(rows)
        self.assertTrue(pin_rows(result, {"input": "input"}))
