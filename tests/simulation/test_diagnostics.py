"""Warnings describe actual electrical topology and point to its source."""

import unittest
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze, diagnostics, solve_input
from circuitlab.model.graph import CircuitError


class DiagnosticAccuracyTest(unittest.TestCase):
    def test_analog_driven_or_sensed_bank_is_not_reported_as_unused(self):
        base = "V1(5) between(bus,0);\nR1(10k) between(bus,sense);\nR2(20k) between(bus,sense);\n"
        for equation in ("analog V(sense) = 2V;", "analog V(out) = V(sense);"):
            with self.subTest(equation=equation):
                source = base + equation
                result = solve_input(source)
                self.assertFalse(diagnostics(source, result))
                if "2V" in equation:
                    self.assertGreater(abs(result.network.branch_current[1]), 1e-6)

    def test_dangling_bank_warning_uses_component_declaration_not_later_measurement(self):
        source = ("V1(5) between(bus,0);\nR1(10k) between(bus,unused);\n"
                  "R2(20k) between(bus,unused);\nmeasure = V unused 0;\n// unused is a test point")
        issue, = diagnostics(source, solve_input(source))
        self.assertEqual((issue.severity, issue.line), ("warning", 3))
        self.assertIn('Node "unused"', issue.message)
        self.assertIn("connect to another component or return wire", issue.message)

    def test_included_private_bank_warning_keeps_definition_file_and_line(self):
        path = Path.cwd() / "warning-main.ckt"
        source = 'include("bank.ckt");\nV1(5) between(bus,0);\nmain { Bank(bus); }'
        child = "circuit Bank(input) {\n Rfirst(10k) between(input,unused);\n Rlast(20k) between(input,unused);\n}"
        circuit = compile_circuit(source, path=path, read_source=lambda _: child)
        issue, = diagnostics(source, analyze(circuit))
        self.assertEqual(issue.path, path.with_name("bank.ckt"))
        self.assertEqual(issue.line, 3)
        self.assertIn("unused", issue.message)

    def test_only_actual_ground_references_are_exempt(self):
        bank = "parallel(bus,agnd) { R1=10k; R2=20k; ground=agnd; }"
        actual_ground = "V1(5) between(bus,agnd);\n" + bank
        self.assertFalse(diagnostics(actual_ground, solve_input(actual_ground)))
        separate_name = "V1(5) between(bus,0);\n" + bank
        issue, = diagnostics(separate_name, solve_input(separate_name))
        self.assertIn('Node "agnd"', issue.message)

    def test_explicit_connector_is_not_an_accidentally_unused_bank(self):
        source = "connector(output);\nV1(5) between(bus,0);\nR1(10k) between(bus,output);\nR2(20k) between(bus,output);"
        self.assertFalse(diagnostics(source, solve_input(source)))

    def test_general_advice_has_no_invented_line_one(self):
        source = "// Only a power value is known.\n\nP = 1;"
        issues = diagnostics(source, solve_input(source))
        self.assertTrue(issues)
        self.assertTrue(all(issue.line is None for issue in issues))

    def test_quantity_conflicts_use_original_lines_and_paths(self):
        path = Path.cwd() / "quantities.ckt"
        source = "// conflicting givens\n\nV = 5;\nI = 1;\nR = 10;"
        circuit = compile_circuit(source, path=path)
        issues = diagnostics(source, analyze(circuit))
        self.assertTrue(issues)
        for issue in issues:
            self.assertEqual(issue.severity, "error")
            self.assertIn(issue.line, {None, 3, 4, 5})
            self.assertEqual(issue.path, path)
        direct = next(issue for issue in issues if "values for V." in issue.message)
        self.assertEqual(direct.line, 3)

    def test_measurement_errors_name_the_target_and_measurement_line(self):
        path = Path.cwd() / "measure.ckt"
        base = "V1(5) between(out,0);\nR1(1k) between(out,0);\n"
        for measure, expected in (("I missing", 'Unknown component "missing"'),
                                  ("V missing 0", "Unknown measurement node: missing"),
                                  ("R out out", "Equivalent resistance needs two distinct nodes")):
            with self.subTest(measure=measure), self.assertRaises(CircuitError) as caught:
                analyze(compile_circuit(base + f"measure = {measure};", path=path))
            self.assertEqual((caught.exception.line, caught.exception.path), (3, path))
            self.assertIn(expected, caught.exception.message)

    def test_voltage_measurement_between_the_same_wire_is_zero(self):
        result = solve_input("V1(5) between(out,0);\nR1(1k) between(out,0);\nmeasure = V out out;")
        self.assertEqual(result.readings[-1][2], 0)
        self.assertIn("out", result.readings[-1][0])
        self.assertNotIn("group at line", result.readings[-1][0])

    def test_imported_measurement_error_keeps_its_own_source(self):
        path = Path.cwd() / "main.ckt"
        source = 'include("load.ckt");\nV1(5) between(bus,0);\nmain { Load(bus); }'
        child = "circuit Load(input) {\n R1(1k) between(input,0);\n measure = I absent;\n}"
        with self.assertRaises(CircuitError) as caught:
            analyze(compile_circuit(source, path=path, read_source=lambda _: child))
        self.assertEqual((caught.exception.line, caught.exception.path), (3, path.with_name("load.ckt")))

    def test_mixed_quantity_and_wired_inputs_explain_where_values_belong(self):
        source = "// misplaced scalar\nV = 5;\nR1(1k) between(a,0);"
        with self.assertRaises(CircuitError) as caught:
            solve_input(source)
        self.assertEqual(caught.exception.line, 2)
        self.assertIn('Global quantity "V"', caught.exception.message)
        self.assertIn("V1(5V) between(a, 0)", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
