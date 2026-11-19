"""Switch states affect DC/AC networks and preserve live stored energy."""

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze, solve_input
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import result_rows


def voltage(network, node):
    return network.voltage[network.nodes.index(node)]


class SchematicSwitchSimulationTest(unittest.TestCase):
    def test_inverting_amplifier_button_closes_and_opens_input(self):
        source = """
        V1(2) between(pos,0);
        Sbutton(closed) between(pos,sw);
        R2(10k) between(sw,inv);
        R1(10k) between(inv,out);
        op1(0,inv,out);
        measure = V out 0;
        """
        for state, resistance in (("closed", .001), ("open", 1e12)):
            with self.subTest(state=state):
                result = solve_input(source.replace("closed", state))
                expected = -2 * 10000 / (10000 + resistance)
                self.assertAlmostEqual(voltage(result.network, "out"), expected)
                self.assertAlmostEqual(result.readings[0][2], expected)
                rows = result_rows(result)
                self.assertIn(("Sbutton state", "Manual switch", state.capitalize()), rows)

    def test_both_states_handle_mixed_dc_and_ac(self):
        source = "V1(4 V | 2 Vrms) between(vin,0);\nf=1k;\nS1({state}) between(vin,out);\nR1(10k) between(out,0);"
        for state, resistance in (("closed", .001), ("open", 1e12)):
            with self.subTest(state=state):
                result = solve_input(source.format(state=state))
                for network, supply in ((result.dc_network, 4), (result.network, 2)):
                    expected = supply * 10000 / (10000 + resistance)
                    self.assertAlmostEqual(voltage(network, "out"), expected)
                    self.assertAlmostEqual(sum(network.branch_power), 0, places=10)

    def test_switch_reconfigure_preserves_capacitor_voltage_and_elapsed_time(self):
        source = """
        V1(5) between(vin,0);
        S1(closed) between(vin,sw);
        R1(1k) between(sw,out);
        C1(1u) between(out,0);
        Rload(10k) between(out,0);
        """
        circuit = compile_circuit(source)
        simulation = TimeDomain(circuit, 100e-6)
        for _ in range(9):
            before = simulation.advance()[simulation.nodes.index("out")]
        elapsed = simulation.time
        opened = simulation.reconfigure(compile_circuit(source.replace("closed", "open")))
        self.assertEqual(opened.time, elapsed)
        capacitor, = opened.capacitors
        self.assertAlmostEqual(opened.previous_capacitor_voltage[capacitor], before)
        after = opened.advance()[opened.nodes.index("out")]
        self.assertLess(after, before)
        self.assertGreater(after, 0)
        self.assertAlmostEqual(opened.time, elapsed + opened.step)
        closed = opened.reconfigure(circuit)
        resumed = closed.advance()[closed.nodes.index("out")]
        self.assertGreater(resumed, after)

    def test_grouped_switches_solve_without_explicit_node_sources(self):
        result = solve_input("V = 2;\n(series)\nS1 = closed;\nR1 = 10k;\n(end series)")
        self.assertAlmostEqual(result.calculated["I"], 2 / 10000.001)

    def test_manual_and_controlled_switches_share_native_time_solver(self):
        source = """
        V1(5) between(vin,0);
        S1(closed) between(vin,sw);
        switch1(vin,0,sw,out,2.5,100,1G);
        R1(1k) between(out,0);
        """
        result = analyze(compile_circuit(source))
        self.assertAlmostEqual(voltage(result.network, "out"), 5 * 1000 / 1100.001, places=6)


if __name__ == "__main__":
    unittest.main()
