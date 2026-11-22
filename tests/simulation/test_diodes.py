"""Verify diode DC physics, power, nonlinear slopes and charge history."""
import unittest
import numpy as np
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.diodes import parse_diode
from circuitlab.math.analysis import solve_input
from circuitlab.math.diodes import diode_current, diode_charge
from circuitlab.math.nonlinear import TimeDomain


def circuit(name="D.rect1", raw="", reverse=False, supply=5):
    a, k = ("0", "out") if reverse else ("out", "0")
    return f"V1({supply}) between(supply,0);\nR1(1k) between(supply,out);\n{name}({raw}) between({a},{k});"


class DiodeSimulationTest(unittest.TestCase):
    def solve(self, **kwargs):
        result = solve_input(circuit(**kwargs), "dc")
        self.assertIsNotNone(result.network)
        return result.network

    def test_rectification_kcl_and_power(self):
        for reverse in (False, True):
            net = self.solve(reverse=reverse)
            voltage = dict(zip(net.nodes, net.voltage))["out"]
            if reverse:
                self.assertGreater(voltage, 4.999)
                self.assertLess(abs(net.branch_current[-1]), 2e-9)
            else:
                self.assertGreater(voltage, .5)
                self.assertLess(voltage, 1)
            self.assertAlmostEqual(net.branch_current[0] + net.branch_current[1], 0, places=10)
            self.assertAlmostEqual(net.branch_current[1] + (1 if reverse else -1)*net.branch_current[2], 0, places=10)
            self.assertAlmostEqual(sum(net.branch_power), 0, places=9)

    def test_types_have_distinct_forward_and_breakdown_behavior(self):
        schott = self.solve(name="D.Schot1")
        self.assertLess(schott.branch_voltage[-1], .4)
        led = self.solve(name="D.LED1", raw="2V")
        self.assertTrue(1.9 < led.branch_voltage[-1] < 2.2)
        blue = self.solve(name="D.LED1", raw="3.2V", supply=4.2)
        self.assertAlmostEqual(blue.branch_voltage[-1], 3.2, places=7)
        zener = self.solve(name="D.Zener1", raw="5.1V", reverse=True, supply=12)
        self.assertTrue(-5.3 < zener.branch_voltage[-1] < -5.1)
        self.assertTrue(-.007 < zener.branch_current[-1] < -.006)

    def test_tunnel_negative_resistance(self):
        model = parse_diode("D.Tun1", "")
        peak, _ = diode_current(model, model.peak_voltage)
        descending, slope = diode_current(model, 2*model.peak_voltage)
        self.assertLess(descending, peak)
        self.assertLess(slope, 0)
        for value in (.03,.14,.35,.6):
            net = TimeDomain(compile_circuit(f"V1({value}) between(a,0);\nD.Tun1(rs=0,cj=0) between(a,0);", "dc")).operating_point()
            self.assertAlmostEqual(net.branch_current[-1], diode_current(model, value)[0], places=9)

    def test_analytic_current_and_charge_slopes(self):
        for name in ("D1", "D.Schot1", "D.Zener1", "D.Tun1", "D.Varactor1", "D.Photo1"):
            model = parse_diode(name, "")
            for voltage in (-5.2, -.7, -.1, .02, .14, .4):
                with self.subTest(name=name, voltage=voltage):
                    for law in (diode_current, diode_charge):
                        value, slope = law(model, voltage)
                        h = 1e-6
                        difference = (law(model, voltage+h)[0] - law(model, voltage-h)[0])/(2*h)
                        self.assertAlmostEqual(difference, slope, delta=max(3e-15, abs(slope)*1e-5))

    def test_varactor_and_photo_models(self):
        model = parse_diode("D.Varactor1", "")
        self.assertGreater(diode_charge(model, -1)[1], diode_charge(model, -5)[1])
        photo = parse_diode("D.Photo1", "light=25uA")
        self.assertAlmostEqual(diode_current(photo,-1)[0], -25e-6, delta=2e-12)

    def test_transient_charge_and_reconfiguration(self):
        source = circuit(name="D.Varactor1", raw="cj=1uF", reverse=True)
        sim = TimeDomain(compile_circuit(source, "dc"), 1e-4)
        samples = [dict(zip(sim.nodes, sim.advance()))["out"] for _ in range(30)]
        self.assertGreater(samples[-1], samples[0])
        self.assertGreater(samples[-1], 4.9)
        updated = sim.reconfigure(compile_circuit(source, "dc"), 2e-4)
        self.assertEqual(updated.previous_diode_charge, sim.previous_diode_charge)
        state = updated.state.copy()
        residual,jacobian = updated.evaluate(state,updated.matrix,updated.target,jacobian=True)
        for index in range(len(state)):
            h=1e-7
            change=np.zeros(len(state)); change[index]=h
            finite=(updated.evaluate(state+change,updated.matrix,updated.target)[0]-updated.evaluate(state-change,updated.matrix,updated.target)[0])/(2*h)
            np.testing.assert_allclose(jacobian[:,index],finite,rtol=1e-5,atol=1e-9)

    def test_ac_waveform_rectifies_in_time_domain(self):
        source = "V1(2Vrms) between(input,0);\nf=1kHz;\nD.rect1(cj=0) between(input,out);\nR1(1k) between(out,0);"
        sim = TimeDomain(compile_circuit(source, "ac"), 1e-5)
        values = [dict(zip(sim.nodes,sim.advance()))["out"] for _ in range(200)]
        self.assertLess(abs(min(values)), 1e-4)
        self.assertGreater(max(values), 2)
