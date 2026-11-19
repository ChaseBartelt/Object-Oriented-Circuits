"""Specified complex impedances in phasor-domain electrical circuits."""
# A specified complex impedance belongs to phasor analysis and retains its complex value.
# Unsupported DC, time-domain, or nonlinear combinations must fail rather than approximate it.

import cmath
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import solve_input
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import component_labels, pin_rows, result_rows
from circuitlab.model.graph import CircuitError


AMPLIFIER = """f = 1k;
Vac(1 Vpk) between(input,0);
Zs(2-j2) between(input,inv);
Zf(4+j4) between(output,inv);
op1(0,inv,output);
"""


class ImpedanceTest(unittest.TestCase):
    def test_complex_feedback_amplifier_gain_peak_phase_current_and_power(self):
        result = solve_input(AMPLIFIER)
        net = result.network
        voltage = dict(zip(net.nodes, net.voltage))
        current = dict(zip(net.names, net.branch_current))
        power = dict(zip(net.names, net.branch_power))
        self.assertEqual(result.circuit.mode, "ac")
        self.assertEqual(net.frequency, 1000)
        self.assertAlmostEqual(voltage["input"], 1 / math.sqrt(2))
        self.assertAlmostEqual(voltage["inv"], 0)
        self.assertAlmostEqual(voltage["output"] / voltage["input"], -2j)
        self.assertAlmostEqual(abs(voltage["output"]), math.sqrt(2))
        self.assertAlmostEqual(math.sqrt(2) * abs(voltage["output"]), 2)
        self.assertAlmostEqual(math.degrees(cmath.phase(voltage["output"])), -90)
        self.assertAlmostEqual(current["Zs"], (1 + 1j) / (4 * math.sqrt(2)))
        self.assertAlmostEqual(current["Zf"], -current["Zs"])
        self.assertAlmostEqual(power["Zs"], 0.125 - 0.125j)
        self.assertAlmostEqual(power["Zf"], 0.25 + 0.25j)
        self.assertAlmostEqual(sum(power.values()), 0)

    def test_ac_peak_source_selects_ac_without_explicit_frequency(self):
        result = solve_input(AMPLIFIER.split("\n", 1)[1])
        self.assertEqual(result.circuit.mode, "ac")
        self.assertIsNone(result.network.frequency)
        self.assertAlmostEqual(result.network.voltage[result.network.nodes.index("output")], -math.sqrt(2) * 1j)

    def test_preferred_wired_and_lowercase_names_keep_the_same_complex_value(self):
        for row in ("Z1(2-j2) between(a,0);", "Z1 a 0 2-j2", "z_theory(2-2i Ω) between(a,0);"):
            result = solve_input("V1(1 Vrms) between(a,0);\n" + row)
            z = next(p for p in result.circuit.components if p.kind == "Z")
            self.assertEqual(z.value, 2 - 2j)
            self.assertAlmostEqual(result.network.branch_current[1], 0.25 + 0.25j)

    def test_existing_rectangular_polar_and_engineering_number_formats(self):
        cases = (("2+j2", 2 + 2j), ("2+2j", 2 + 2j), ("2+2i", 2 + 2j),
                 ("2-j2 kΩ", 2000 - 2000j), ("2-2j kohm", 2000 - 2000j),
                 ("2e3-j2e3", 2000 - 2000j), ("4<90 ohm", 4j),
                 ("4∠-90° Ω", -4j), ("j2", 2j), ("-j", -1j), ("2k", 2000))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                circuit = compile_circuit(f"Z1({raw}) between(a,0);", "ac")
                self.assertAlmostEqual(circuit.components[0].value, expected)

    def test_real_and_negative_real_impedance_are_valid_theoretical_ac_values(self):
        for value in (1000, -1000):
            result = solve_input(f"V1(1 Vrms) between(a,0);\nZ1({value}) between(a,0);")
            self.assertAlmostEqual(result.network.branch_current[1], 1 / value)
            self.assertAlmostEqual(result.network.branch_power[1], 1 / value)

    def test_specified_impedance_stays_constant_when_analysis_frequency_changes(self):
        currents = []
        for rate in (100, 1000, 10000):
            result = solve_input(f"f={rate};\nV1(1 Vrms) between(a,0);\nZ1(2-j2) between(a,0);")
            currents.append(result.network.branch_current[1])
        self.assertTrue(all(abs(value - (0.25 + 0.25j)) < 1e-12 for value in currents))

    def test_zero_nonfinite_and_wrong_unit_values_have_source_lines(self):
        for raw, fragment in (("0", "nonzero"), ("0+j0", "nonzero"), ("0<90", "nonzero"),
                              ("1e999", "finite"), ("NaN", "Invalid number"), ("2F", "Expected unit ohm")):
            with self.subTest(raw=raw), self.assertRaises(CircuitError) as caught:
                compile_circuit(f"# header\nf=1k;\nZ1({raw}) between(a,0);")
            self.assertEqual(caught.exception.line, 3)
            self.assertIn(fragment, caught.exception.message)

    def test_zero_and_invalid_modes_remain_checked_for_programmatic_graphs(self):
        circuit = compile_circuit("Z1(1) between(a,0);", "ac")
        zero = replace(circuit.components[0], value=0)
        for mode, ac, components in (("ac", True, [zero]), ("dc", False, circuit.components),
                                      ("mixed", True, circuit.components)):
            with self.subTest(mode=mode), self.assertRaises(CircuitError):
                NodalBackend().solve(replace(circuit, mode=mode, components=components), ac=ac)

    def test_all_z_devices_are_explicitly_ac_only(self):
        for mode in ("dc", "mixed", "transient"):
            for value in ("2-j2", "2"):
                with self.subTest(mode=mode, value=value), self.assertRaisesRegex(CircuitError, "AC steady state"):
                    compile_circuit(f"Z1({value}) between(a,0);", mode)
        with self.assertRaisesRegex(CircuitError, "DC offset"):
            compile_circuit("""battery V1 { Vpk=1; Vdc=1; f=1k; wire=a; }
                main { series { Z1=2-j2; } }""")

    def test_time_domain_rejects_z_even_when_circuit_mode_is_changed(self):
        circuit = compile_circuit(AMPLIFIER)
        for candidate in (circuit, replace(circuit, mode="dc")):
            with self.assertRaisesRegex(CircuitError, "no time-domain model"):
                TimeDomain(candidate, 1e-6)

    def test_native_bjt_and_switch_combinations_fail_explicitly(self):
        for part in ("transQ1(a,a,0,npn);", "switch1(a,0,a,0,0.5,1,1M);"):
            with self.subTest(part=part), self.assertRaisesRegex(CircuitError, "cannot be combined with native BJTs or switches"):
                solve_input("V1(1 Vrms) between(a,0);\nZ1(2-j2) between(a,0);\n" + part)

    def test_tiny_impedance_reports_numeric_range_instead_of_dividing_by_zero(self):
        with self.assertRaisesRegex(CircuitError, "numeric range"):
            solve_input("V1(1 Vrms) between(a,0);\nZ1(1e-320) between(a,0);")

    def test_series_parallel_group_reduction_keeps_complex_values(self):
        for mode, z1, z2, expected in (("series", "2-j2", "4+j4", 6 + 2j),
                                      ("parallel", "2-j2", "2+j2", 2)):
            source = f"f=1k;\nVmag=1;\nphaseV=0;\n({mode})\nZ1={z1}\nZ2={z2}\n(end {mode})"
            result = solve_input(source)
            self.assertTrue(result.circuit.reduce_groups)
            self.assertAlmostEqual(result.circuit.known["Z"], expected)

    def test_battery_groups_and_typed_value_or_component_parameters(self):
        prefix = "battery V1 { Vrms=1; f=1k; wire=in; }\n"
        for declaration, call, expected in (
            ("series load(value) { value=Z1; }", "load(2-j2);", 2 - 2j),
            ("series pair(a,b) { a=part; b=part; }", "pair(Z(2-j2), R(2));", 4 - 2j),
        ):
            result = solve_input(prefix + declaration + "\nmain { " + call + " }")
            self.assertTrue(any(p.kind == "Z" for p in result.circuit.components))
            self.assertAlmostEqual(result.network.branch_current[1], 1 / expected)
        result = solve_input("""f=1k;
            V1(1 Vrms) between(in,0);
            circuit branch(part,input,output) { part between(input,output); }
            main { branch(Zs(2-j2),in,0); }
        """)
        self.assertAlmostEqual(result.network.branch_current[1], 0.25 + 0.25j)

    def test_quantity_z_keeps_its_existing_meaning(self):
        result = solve_input("f=1k;\nZ=2-j2;\nI=1;")
        self.assertEqual(result.circuit.components, [])
        self.assertEqual(result.circuit.known["Z"], 2 - 2j)
        self.assertAlmostEqual(result.calculated["V"], 2 - 2j)

    def test_reports_preserve_imaginary_values_and_short_rectangular_captions(self):
        result = solve_input(AMPLIFIER)
        labels = component_labels(result.circuit)
        self.assertEqual(labels["Zs"], ["Zs", "2 − j2 Ω"])
        self.assertEqual(labels["Zf"], ["Zf", "4 + j4 Ω"])
        rows = result_rows(result)
        self.assertTrue(any("Zs" in label and "j2" in value for label, _, value in rows))
        self.assertTrue(any("Zs" in label and formula == "I = Vbranch / Z" for label, formula, _ in rows))
        readings = pin_rows(result, {"output": "Output"})
        self.assertTrue(any("voltage peak" in label and value == "2 V" for label, _, value in readings))
        self.assertTrue(any("voltage phase" in label and value == "-90°" for label, _, value in readings))

    def test_linked_invalid_impedance_preserves_file_and_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.ckt"
            source = "# bad part\nf=1k;\nZbad(0) between(a,0);\n"
            path.write_text(source)
            with self.assertRaises(CircuitError) as caught:
                compile_circuit(source, path=path)
            self.assertEqual(caught.exception.path, path)
            self.assertEqual(caught.exception.line, 3)

    def test_real_z_matches_resistor_in_ac_and_regular_rc_stays_unchanged(self):
        source = "f=1k;\nV1(1 Vrms) between(a,0);\nR1(1k) between(a,b);\nC1(100n) between(b,0);"
        resistor = solve_input(source)
        impedance = solve_input(source.replace("R1(1k)", "Z1(1k)"))
        for values, expected in ((impedance.network.voltage, resistor.network.voltage),
                                 (impedance.network.branch_current, resistor.network.branch_current),
                                 (impedance.network.branch_power, resistor.network.branch_power)):
            for left, right in zip(values, expected):
                self.assertAlmostEqual(left, right)
        expected = 1 / (1 + 1j * 2 * math.pi * 1000 * 1000 * 100e-9)
        self.assertAlmostEqual(resistor.network.voltage[resistor.network.nodes.index("b")], expected)


if __name__ == "__main__":
    unittest.main()
