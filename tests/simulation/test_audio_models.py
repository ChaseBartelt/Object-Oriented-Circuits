"""Electrical checks for the eight-argument op amp's audio gain/bias network."""

import unittest

import numpy as np

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain


SOURCE = """V1(10mV) between(input,0);
V2(6V) between(vcc,0);
Rload(8) between(out,0);
op1(input,0,out,vcc,0,g1,g8,bypass);
"""


class AudioModelTest(unittest.TestCase):
    def test_open_gain_pins_bias_and_public_arrays_match_both_backends(self):
        circuit = compile_circuit(SOURCE)
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 3.2)
            self.assertEqual(network.nodes, circuit.nodes)
            self.assertEqual(network.names, tuple(p.name for p in circuit.components))
            self.assertAlmostEqual(sum(network.branch_power), 0)
            self.assertAlmostEqual(sum(network.terminal_current["op1"]), 0)
            self.assertAlmostEqual(network.branch_current[network.names.index("V2")], -.004)

    def test_resistor_between_gain_pins_sets_intermediate_gain(self):
        circuit = compile_circuit(SOURCE + "Rg(1.35k) between(g1,g8);\n")
        # gm*R_eff = (gain.max/gain.default-1)/gain.max/2.
        gain = 200 / (1 + 9 / 2)
        for network in (NodalBackend().solve(circuit, ac=False), TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 3 + gain * .01)

    def test_shorting_gain_pins_gives_maximum_gain(self):
        circuit = compile_circuit(SOURCE.replace(",g1,g8,bypass)", ",g8,g8,bypass)"))
        self.assertAlmostEqual(NodalBackend().solve(circuit, ac=False).voltage[circuit.nodes.index("out")], 5)

    def test_ac_gain_capacitor_uses_its_frequency_dependent_impedance(self):
        source = SOURCE.replace("10mV", "10mVrms").replace("V2(6V)", "V2(0Vrms)")
        circuit = compile_circuit(source + "Cg(10uF) between(g1,g8);\nf=1kHz;\n")
        f = 1000
        impedance = 1 / (1 / 1350 + 1j * 2 * np.pi * f * 10e-6)
        expected = .01 * 200 / (1 + (9 / 200 / 1350) * 200 * impedance)
        network = NodalBackend().solve(circuit, ac=True, frequency=f)
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], expected)

    def test_bypass_capacitor_controls_bias_startup_and_live_reconfigure(self):
        source = SOURCE.replace("10mV", "0V") + "Cb(1uF) between(bypass,0);\n"
        sim = TimeDomain(compile_circuit(source), .01)
        for count in range(1, 4):
            voltage = sim.advance()
            self.assertAlmostEqual(voltage[sim.nodes.index("out")], 3 * (1 - (1 + .01 / .05) ** -count))
        sim = sim.reconfigure(compile_circuit(source.replace("V2(6V)", "V2(8V)")))
        self.assertAlmostEqual(sim.time, .03)
        self.assertTrue(np.isfinite(sim.advance()).all())

    def test_audio_properties_change_the_model(self):
        circuit = compile_circuit(SOURCE + "analog op1 {gain.default=10; gain.max=100; "
                                  "output.bias=.25; supply.current=2mA;}\n")
        network = NodalBackend().solve(circuit, ac=False)
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 1.6)
        self.assertAlmostEqual(network.branch_current[network.names.index("V2")], -.002)

    def test_ac_time_simulation_keeps_quiescent_current_as_a_dc_draw(self):
        source = SOURCE.replace("10mV", "10mVrms").replace("V2(6V)", "V2(0Vrms)") + "f=1kHz;\n"
        sim = TimeDomain(compile_circuit(source), 1e-5)
        network = sim.operating_point()
        self.assertAlmostEqual(network.branch_current[network.names.index("V2")], -.004)
