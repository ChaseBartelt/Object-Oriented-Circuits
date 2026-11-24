"""Explain unwired component assignments without changing quantity syntax."""

import unittest
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError


class ComponentConnectionDiagnosticsTest(unittest.TestCase):
    def test_chip_resistor_error_explains_connections_at_original_location(self):
        path = Path(__file__).resolve().parents[2] / "examples" / "unwired_jack.ckt"
        source = """chip HeadphoneJack5(sleeve, ring, aux3, aux4, tip) {
    R4 = 10;
}
main { HeadphoneJack5(0, right, unused3, unused4, left); }
"""
        with self.assertRaises(CircuitError) as caught:
            compile_circuit(source, path=path, render_only=True)
        error = caught.exception
        self.assertEqual(error.line, 2)
        self.assertEqual(Path(error.path), path)
        self.assertIn('Component "R4" has a value but no wire connections', str(error))
        self.assertIn("R4(10) between(wire1, wire2)", str(error))
        self.assertIn("series or parallel block", str(error))

    def test_named_components_in_top_level_and_main_get_connection_guidance(self):
        for assignment in ("R4 = 10", "C1 = 100n", "D.rect1 = 0.7V"):
            name, value = assignment.split(" = ")
            for source in (assignment + ";", "main { " + assignment + "; }"):
                with self.subTest(source=source), self.assertRaises(CircuitError) as caught:
                    compile_circuit(source, render_only=True)
                self.assertIn(f"{name}({value}) between(wire1, wire2)", str(caught.exception))

    def test_potentiometer_example_has_three_terminals(self):
        with self.assertRaises(CircuitError) as caught:
            compile_circuit("P1 = 10k;", render_only=True)
        self.assertIn("P1(10k) between(high, wiper, low)", str(caught.exception))
        self.assertNotIn("series or parallel", str(caught.exception))

    def test_underscored_component_assignments_get_the_same_guidance(self):
        for body in ("R_load = 10k;", "C_input = 100n;"):
            name, value = body.rstrip(";").split(" = ")
            source = f"chip Test(a,b) {{\n {body}\n}}\nmain {{ Test(x,0); }}"
            with self.subTest(body=body), self.assertRaises(CircuitError) as caught:
                compile_circuit(source, render_only=True)
            self.assertEqual(caught.exception.line, 2)
            self.assertIn(f"{name}({value}) between(wire1, wire2)", caught.exception.message)

    def test_bare_component_value_calls_report_missing_connections(self):
        for body in ("R4(10);", "R_load(10k);", "C1(100n);", "Sbutton(closed);"):
            name, rest = body.split("(", 1)
            value = rest.rstrip(");")
            source = f"chip Test(a,b) {{\n {body}\n}}\nmain {{ Test(x,0); }}"
            with self.subTest(body=body), self.assertRaises(CircuitError) as caught:
                compile_circuit(source, render_only=True)
            self.assertEqual(caught.exception.line, 2)
            self.assertIn(f"{name}({value}) between(wire1, wire2)", caught.exception.message)
            self.assertNotIn("Unknown section", caught.exception.message)
        with self.assertRaisesRegex(CircuitError, 'Unknown section "MissingChip"'):
            compile_circuit("main { MissingChip(a,b); }", render_only=True)

    def test_reactance_guidance_does_not_suggest_an_unsupported_explicit_form(self):
        with self.assertRaises(CircuitError) as caught:
            compile_circuit("X4 = 10;", mode="ac", render_only=True)
        self.assertIn("series(wire1, wire2) { X4 = 10; }", caught.exception.message)
        self.assertIn("Z1(j10) between(wire1, wire2)", caught.exception.message)
        self.assertNotIn("X4(10) between", caught.exception.message)
        with self.assertRaises(CircuitError) as caught:
            compile_circuit("X4(10) between(a,b);", mode="ac", render_only=True)
        self.assertIn('Reactance "X4"', caught.exception.message)
        self.assertIn("Z1(j10) between(wire1, wire2)", caught.exception.message)

    def test_invalid_placement_errors_identify_part_and_expected_terminals(self):
        for source, expected in (("R4(10) between(a);", "requires 2 wire names, but received 1"),
                                 ("R4(10) between(a,b,c);", "requires 2 wire names, but received 3"),
                                 ("R4(10) between(a,b+1);", "invalid or empty wire name"),
                                 ("P1(10k) between(a,b);", "requires 3 wire names, but received 2")):
            with self.subTest(source=source), self.assertRaises(CircuitError) as caught:
                compile_circuit(source, render_only=True)
            self.assertIn(expected, caught.exception.message)
            self.assertIn("between(", caught.exception.message)

    def test_component_value_and_unit_errors_name_the_offending_part(self):
        for body, expected in (("R_load(10V) between(a,b);", "Expected unit ohm"),
                               ("C1(bogus) between(a,b);", "Invalid number"),
                               ("series(a,b) { R4 = 10V; }", "Expected unit ohm")):
            with self.subTest(body=body), self.assertRaises(CircuitError) as caught:
                compile_circuit(body, render_only=True)
            self.assertIn('Component "', caught.exception.message)
            self.assertIn(expected, caught.exception.message)

    def test_valid_group_assignments_and_explicit_connections_still_compile(self):
        for body in ("R4(10) between(tip, sleeve);",
                     "series(tip, sleeve) { R4 = 10; }",
                     "parallel(tip, sleeve) { R4 = 10; }"):
            with self.subTest(body=body):
                circuit = compile_circuit(
                    f"chip Jack(tip, sleeve) {{ {body} }} main {{ Jack(left, 0); }}",
                    render_only=True,
                )
                resistor, = circuit.components
                self.assertEqual(resistor.value, 10)
                self.assertEqual({resistor.p, resistor.n}, {"left", "0"})

    def test_unknown_and_wrong_mode_quantities_keep_existing_diagnostic(self):
        for key in ("nonsense", "XL", "V0", "I0"):
            with self.subTest(key=key), self.assertRaises(CircuitError) as caught:
                compile_circuit(f"{key} = 10;", mode="dc", render_only=True)
            self.assertIn(f'Unknown quantity "{key}" for this analysis.', str(caught.exception))


if __name__ == "__main__":
    unittest.main()
