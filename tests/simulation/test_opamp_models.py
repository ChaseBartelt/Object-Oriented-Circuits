"""Analytical checks for gain, input offset, dominant pole, slew and supply draw."""

import math
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError


def output(network):
    return network.voltage[network.nodes.index("out")]


class OpampModelTest(unittest.TestCase):
    def test_finite_dc_gain_and_offset_match_both_backends(self):
        circuit = compile_circuit("V1(2) between(input,0); opamp op1 { gain=100; offset=1mV; } "
                                  "op1(input,out,out);")
        expected = 100 * 2.001 / 101
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(output(network), expected)

    def test_inverting_amplifier_uses_finite_gain_and_offset(self):
        circuit = compile_circuit("V1(2) between(input,0); R1(1k) between(input,inv); "
                                  "R2(1k) between(inv,out); opamp op1 { gain=100; offset=1mV; } "
                                  "op1(0,inv,out);")
        self.assertAlmostEqual(output(NodalBackend().solve(circuit, ac=False)), (-2 + .002) / 1.02)

    def test_ac_pole_matches_closed_loop_transfer_and_ignores_dc_offset(self):
        circuit = compile_circuit("V1(1Vrms) between(input,0); f=1k; "
                                  "opamp op1 { gain=10000; bandwidth=1kHz; offset=20mV; } "
                                  "op1(input,out,out);")
        network = NodalBackend().solve(circuit, ac=True, frequency=1000)
        self.assertAlmostEqual(output(network), 1 / (1 + 1e-4 + 1j))
        with self.assertRaisesRegex(CircuitError, "frequency"):
            NodalBackend().solve(circuit, ac=True)

    def test_time_domain_pole_matches_backward_euler_response(self):
        circuit = compile_circuit("V1(1) between(input,0); opamp op1 { bandwidth=1kHz; } "
                                  "op1(input,out,out);")
        step = 1e-4
        sim = TimeDomain(circuit, step)
        coefficient = math.tau * 1000 * step
        for count in range(1, 11):
            voltage = sim.advance()[sim.nodes.index("out")]
            self.assertAlmostEqual(voltage, 1 - (1 + coefficient) ** -count)

    def test_slew_only_override_limits_both_directions_and_preserves_live_state(self):
        text = "V1(10) between(input,0); opamp op1 { slew=1V/s; } op1(input,out,out);"
        sim = TimeDomain(compile_circuit(text), .1)
        for count in range(1, 6):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], count / 10)
        sim = sim.reconfigure(compile_circuit(text.replace("V1(10)", "V1(-10)")))
        self.assertAlmostEqual(sim.time, .5)
        self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], .4)

    def test_bandwidth_and_slew_apply_together(self):
        sim = TimeDomain(compile_circuit("V1(10) between(input,0); "
                         "opamp op1 { gain=100; bandwidth=1kHz; slew=10V/s; } op1(input,out,out);"), .01)
        for count in range(1, 6):
            self.assertAlmostEqual(sim.advance()[sim.nodes.index("out")], count / 10)

    def test_quiescent_draw_loads_supplies_and_balances_power(self):
        circuit = compile_circuit("V1(1) between(input,0); Vp(5) between(pos,0); "
                                  "Vn(-5) between(neg,0); Rload(1k) between(out,0); "
                                  "opamp op1 { supply_current=2.75mA; } op1(input,out,out,pos,neg);")
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            with self.subTest(backend=type(network)):
                currents = dict(zip(network.names, network.branch_current))
                self.assertAlmostEqual(currents["Vp"], -.00275)
                self.assertAlmostEqual(currents["Vn"], .00275)
                self.assertAlmostEqual(sum(network.branch_power), 0)
                self.assertEqual(network.terminal_current["op1"][-2:], (.00275, -.00275))

    def test_ac_quiescent_draw_is_zero_small_signal(self):
        circuit = compile_circuit("V1(1Vrms) between(input,0); Vp(0Vrms) between(pos,0); "
                                  "Vn(0Vrms) between(neg,0); "
                                  "opamp op1 { supply_current=1mA; } op1(input,out,out,pos,neg);")
        network = NodalBackend().solve(circuit, ac=True, frequency=1000)
        self.assertEqual(network.branch_current[network.names.index("Vp")], 0)
