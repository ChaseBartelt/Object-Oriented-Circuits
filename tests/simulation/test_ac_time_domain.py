"""AC sources drive the real time-domain equations using the RMS cosine convention."""

from dataclasses import replace
import math
import unittest
from unittest.mock import patch

import numpy as np

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import solve_input
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError


class AcTimeDomainTest(unittest.TestCase):
    def test_peak_rms_and_phasor_sources_follow_cosine_phase(self):
        for raw, peak, phase in (("2 Vrms", math.sqrt(8), 0), ("2 Vpk", 2, 0),
                                 ("2<90 Vrms", math.sqrt(8), math.pi / 2),
                                 ("-2 Vrms", math.sqrt(8), math.pi)):
            with self.subTest(raw=raw):
                sim = TimeDomain(compile_circuit(f"f=1k;\nV1({raw}) between(out,0);\nR1(1k) between(out,0);"), 125e-6)
                for _ in range(8):
                    voltage = sim.advance()[sim.nodes.index("out")]
                    self.assertAlmostEqual(voltage, peak * math.cos(math.tau * 1000 * sim.time + phase))

    def test_battery_blocks_use_their_phase_and_frequency_units(self):
        for frequency in ("f=1k;", "T=1m;", f"w={math.tau * 1000};"):
            source = f"""battery test {{ Vrms=2; phaseV=30; {frequency} wire=vin; }}
main {{ series {{ R1=1k; }} }}
"""
            with self.subTest(frequency=frequency):
                sim = TimeDomain(compile_circuit(source), 1e-4)
                self.assertAlmostEqual(sim.frequency, 1000)
                for _ in range(5):
                    value = sim.advance()[sim.nodes.index("vin")]
                    self.assertAlmostEqual(value, math.sqrt(8) * math.cos(math.tau * 1000 * sim.time + math.pi / 6))

    def test_mixed_battery_has_dc_offset_and_potentiometer_loading(self):
        source = """battery test { Vdc=3; Vrms=2; phaseV=-90; f=1k; wire=vin; }
P1(200,25%) between(vin,out,0);
Rload(50) between(out,0);
"""
        sim = TimeDomain(compile_circuit(source), 125e-6)
        for _ in range(8):
            value = sim.advance()[sim.nodes.index("out")]
            self.assertAlmostEqual(value, (3 + math.sqrt(8) * math.sin(math.tau * 1000 * sim.time)) / 7)

    def test_ac_and_mixed_current_sources_use_the_same_waveform_and_orientation(self):
        for raw, offset in (("2<90 mArms", 0), ("1mA | 2<90 mArms", .001)):
            source = f"f=1k;\nI1({raw}) between(0,out);\nR1(1k) between(out,0);"
            sim = TimeDomain(compile_circuit(source), 125e-6)
            for _ in range(8):
                network = sim.operating_point()
                amps = offset - math.sqrt(2) * .002 * math.sin(math.tau * 1000 * sim.time)
                self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 1000 * amps)
                self.assertAlmostEqual(network.branch_current[0], amps)
                self.assertAlmostEqual(sum(network.branch_power), 0)

    def test_rc_waveform_keeps_capacitor_initial_state_and_matches_integration(self):
        source = "f=1k;\nV1(2 Vpk) between(vin,0);\nR1(1k) between(vin,out);\nC1(100n) between(out,0);"
        step, capacitance, resistance = 5e-6, 100e-9, 1000
        sim = TimeDomain(compile_circuit(source), step)
        sim.set_initial_voltage("C1", .75)
        expected = .75
        for _ in range(600):
            values = sim.advance()
            vin = 2 * math.cos(math.tau * 1000 * sim.time)
            expected = (capacitance / step * expected + vin / resistance) / (capacitance / step + 1 / resistance)
            self.assertAlmostEqual(values[sim.nodes.index("out")], expected)
        phasor = solve_input(source).network
        rms = phasor.voltage[phasor.nodes.index("out")]
        steady = math.sqrt(2) * (rms * complex(math.cos(math.tau * 1000 * sim.time), math.sin(math.tau * 1000 * sim.time))).real
        self.assertAlmostEqual(expected, steady, delta=.02)

    def test_adaptive_half_steps_evaluate_ac_at_each_actual_sample_time(self):
        source = """f=1k;
V1(2 Vpk) between(vin,0);
switch1(vin,0,vin,out,-10,1000,1G);
C1(100n) between(out,0);
"""
        circuit = compile_circuit(source)
        sim, reference = TimeDomain(circuit, 1e-4), TimeDomain(circuit, 5e-5)
        for item in (sim, reference):
            item.set_initial_voltage("C1", .75)
        # Force the same retry path used when a nonlinear switching edge fails to converge.
        failure = CircuitError("Retry this step", code="NonlinearConvergence")
        with patch.object(sim, "advance_once", side_effect=failure):
            actual = sim.advance()
        reference.advance()
        expected = reference.advance()
        np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-12)
        self.assertAlmostEqual(sim.time, 1e-4)
        self.assertAlmostEqual(sim.half_step.time, sim.time)
        value = sim.advance()[sim.nodes.index("vin")]
        self.assertAlmostEqual(value, 2 * math.cos(math.tau * 1000 * sim.time))

    def test_mixed_ac_bjt_bias_matches_instantaneous_dc_without_storage(self):
        source = """f=1k;
Vcc(5 | 0) between(vcc,0);
Vb(.65 | .05 Vpk) between(drive,0);
Rb(1k) between(drive,base);
Rc(1k) between(vcc,out);
transQ1(base,out,0,npn);
"""
        sim = TimeDomain(compile_circuit(source), 125e-6)
        for _ in range(8):
            actual = sim.advance()[sim.nodes.index("out")]
            bias = .65 + .05 * math.cos(math.tau * 1000 * sim.time)
            dc = source.split("\n", 1)[1].replace("5 | 0", "5").replace(".65 | .05 Vpk", str(bias))
            net = solve_input(dc).network
            self.assertAlmostEqual(actual, net.voltage[net.nodes.index("out")], places=7)

    def test_missing_conflicting_invalid_frequency_and_theoretical_z_are_explicit(self):
        source = "V1(2 Vrms) between(out,0);\nR1(1k) between(out,0);"
        for prefix, message in (("", "frequency"), ("f=0;\n", "positive"),
                                ("f=1k;\nT=2m;\n", "conflicting")):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(CircuitError, message):
                TimeDomain(compile_circuit(prefix + source), 1e-5)
        with self.assertRaisesRegex(CircuitError, "no time-domain model"):
            TimeDomain(compile_circuit("f=1k;\n" + source.replace("R1(1k)", "Z1(1k)")), 1e-5)
        circuit = compile_circuit("f=1k;\n" + source)
        source_part = replace(circuit.components[0], dc=1j)
        with self.assertRaisesRegex(CircuitError, "real DC"):
            TimeDomain(replace(circuit, mode="mixed", components=[source_part, circuit.components[1]]), 1e-5)


if __name__ == "__main__":
    unittest.main()
