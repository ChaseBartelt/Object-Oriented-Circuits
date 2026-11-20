"""Unspecified component values can be drawn but cannot be simulated."""
# A missing value is a schematic placeholder, not a numeric zero or open circuit.
# Drawing can succeed while solving/exporting must request the absent physical information.

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import Analysis, analyze, group_impedances, reduce_groups, solve_input
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError


class UnspecifiedImpedanceTest(unittest.TestCase):
    def assert_missing(self, action, name="Z1", *, line=None, path=None):
        with self.assertRaises(CircuitError) as caught:
            action()
        error = caught.exception
        self.assertEqual(error.code, "MissingComponentValue")
        self.assertIn(name, error.message)
        self.assertIn("schematic placeholders", error.message)
        if line is not None:
            self.assertEqual(error.line, line)
        if path is not None:
            self.assertEqual(error.path, path)

    def test_exact_empty_placement_renders_as_unknown_in_auto_ac(self):
        for source in ("Z1() between(output,0);", "z1(   ) between(output,0);"):
            for render_only in (False, True):
                with self.subTest(source=source, render_only=render_only):
                    circuit = compile_circuit(source, render_only=render_only)
                    part = circuit.components[0]
                    self.assertEqual(circuit.mode, "ac")
                    self.assertEqual((part.kind, part.p, part.n, part.value), ("Z", "output", "0", None))
                    self.assertEqual(component_labels(circuit)[part.name], [part.name])

    def test_missing_value_precedes_floating_network_or_missing_frequency_errors(self):
        self.assert_missing(lambda: solve_input("# incomplete circuit\nZ1() between(a,b);"), line=2)
        self.assert_missing(lambda: solve_input("Z1() between(a,0);\nC1(10n) between(a,0);"), line=1)

    def test_all_numerical_entry_points_reject_unknown_even_when_mode_changes(self):
        circuit = compile_circuit("Z1() between(a,0);")
        for mode in ("ac", "dc", "mixed", "transient"):
            candidate = replace(circuit, mode=mode)
            for action in (lambda: analyze(candidate),
                           lambda: NodalBackend().solve(candidate, ac=mode == "ac"),
                           lambda: TimeDomain(candidate, 1e-6)):
                with self.subTest(mode=mode):
                    self.assert_missing(action, line=1)

    def test_simulation_cannot_bypass_missing_values_with_a_custom_backend(self):
        class UnexpectedBackend:
            def solve(self, *args, **kwargs):
                raise AssertionError("An incomplete graph reached the backend")

        self.assert_missing(lambda: analyze(compile_circuit("Z1() between(a,0);"), UnexpectedBackend()))

    def test_unknown_is_distinct_from_zero_and_cannot_short_a_group(self):
        source = "(series)\nZ1 = ;\nR1 = 10\n(end series)"
        reduced = compile_circuit(source)
        self.assertTrue(reduced.reduce_groups)
        self.assertIsNone(reduced.groups[0].entries[0][2])
        self.assert_missing(lambda: analyze(reduced), line=2)
        self.assert_missing(lambda: group_impedances(reduced, 0), line=2)
        drawing = compile_circuit(source, render_only=True)
        unknown = next(part for part in drawing.components if part.name == "Z1")
        self.assertEqual(unknown.kind, "Z")
        self.assertIsNone(unknown.value)
        self.assertNotEqual(unknown.equation, "V = I x 0 = 0")
        self.assert_missing(lambda: analyze(drawing), line=2)
        with self.assertRaisesRegex(CircuitError, "nonzero"):
            compile_circuit("Z1(0) between(a,0);")

    def test_parallel_and_braced_groups_keep_named_placeholders(self):
        for source in ("(parallel)\nZ1 =\nR1 = 10\n(end parallel)",
                       "main { parallel(a,0) { Z1 = ; R1 = 10; } }"):
            circuit = compile_circuit(source)
            self.assert_missing(lambda: analyze(circuit))

    def test_multiline_group_value_and_placeholder_before_closing_marker(self):
        circuit = compile_circuit("(parallel)\nR1 =\n10\nZ1 =\n(end parallel)")
        self.assertEqual([entry[2] for entry in circuit.groups[0].entries], [10, None])
        self.assert_missing(lambda: analyze(circuit), line=4)

    def test_multiple_missing_components_are_named_together(self):
        circuit = compile_circuit("Z1() between(a,0);\nZ2() between(b,0);")
        self.assert_missing(lambda: analyze(circuit), "Z1, Z2", line=1)

    def test_typed_component_arguments_accept_bare_or_empty_placeholders(self):
        template = """circuit branch(part,input,output) { part between(input,output); }
main { branch(%s,a,0); }"""
        circuit = compile_circuit(template % "Z1()")
        self.assertEqual(circuit.mode, "ac")
        self.assertIsNone(circuit.components[0].value)
        self.assert_missing(lambda: analyze(circuit), "Z1_branch_1", line=1)
        for kind in "RCLZ":
            for part in (f"{kind}1()", f"{kind}1"):
                with self.subTest(part=part):
                    circuit = compile_circuit(template % part)
                    self.assertIsNone(circuit.components[0].value)
                    self.assert_missing(lambda: analyze(circuit), f"{kind}1_branch_1", line=1)

    def test_all_valued_parts_accept_bare_or_empty_placements_with_unit_captions(self):
        for kind, unit in (("R", "Ω"), ("C", "F"), ("L", "H"), ("V", "V"), ("I", "A"), ("Z", "Ω")):
            for suffix in ("", "()", "(  )"):
                source = f"{kind}1{suffix} between(a,0);"
                with self.subTest(source=source):
                    circuit = compile_circuit(source, render_only=True)
                    self.assertIsNone(circuit.components[0].value)
                    caption = [f"{kind}1"] if kind == "Z" else [f"{kind}1", f"? {unit}"]
                    self.assertEqual(component_labels(circuit)[f"{kind}1"], caption)
                    self.assert_missing(lambda: analyze(circuit), f"{kind}1", line=1)
                    self.assert_missing(lambda: NodalBackend().solve(circuit, ac=circuit.mode == "ac"), f"{kind}1")
                    self.assert_missing(lambda: TimeDomain(circuit), f"{kind}1")

    def test_all_valued_parts_accept_empty_group_assignments(self):
        for kind in "RCLVIZ":
            source = f"# group\n(series)\n{kind}1 = ;\n(end series)"
            for render_only in (False, True):
                with self.subTest(kind=kind, render_only=render_only):
                    circuit = compile_circuit(source, render_only=render_only)
                    self.assert_missing(lambda: analyze(circuit), f"{kind}1", line=3)
                    self.assert_missing(lambda: reduce_groups(Analysis(circuit), {}), f"{kind}1", line=3)

    def test_bare_placements_keep_method_scope_and_typed_bindings(self):
        source = """circuit stage(input,output) { R1 between(input,output); }
main { stage(a,b); stage(b,0); }"""
        circuit = compile_circuit(source)
        self.assertEqual([(p.name, p.p, p.n, p.value) for p in circuit.components],
                         [("R1_stage_1", "a", "b", None), ("R1_stage_2", "b", "0", None)])
        known = compile_circuit("""circuit branch(part,input,output) { part between(input,output); }
main { branch(R1(10),a,0); }""")
        self.assertEqual(known.components[0].value, 10)
        known.require_values()

    def test_unknown_source_is_never_solved_as_zero_in_dc_or_ac(self):
        for kind in "VI":
            for mode in ("dc", "ac", "mixed"):
                with self.subTest(kind=kind, mode=mode):
                    circuit = compile_circuit(f"{kind}1 between(a,0);\nR1(1k) between(a,0);", mode)
                    self.assert_missing(lambda: analyze(circuit), f"{kind}1")
                    self.assert_missing(lambda: NodalBackend().solve(circuit, ac=False), f"{kind}1")

    def test_explicit_non_ac_modes_still_reject_theoretical_impedances(self):
        for mode in ("dc", "mixed", "transient"):
            with self.subTest(mode=mode), self.assertRaisesRegex(CircuitError, "AC steady state"):
                compile_circuit("Z1() between(a,0);", mode)

    def test_numeric_z_also_selects_ac_without_frequency(self):
        for statement in ("Z1(2-j2) between(a,0);", "Z1 a 0 2-j2"):
            result = solve_input("V1(1) between(a,0);\n" + statement)
            self.assertEqual(result.circuit.mode, "ac")
            self.assertAlmostEqual(result.network.branch_current[1], 0.25 + 0.25j)

    def test_reduced_and_expanded_groups_preserve_missing_source_location(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "load.ckt"
            source = "# load\n(series)\nZ1 = ;\n(end series)"
            path.write_text(source)
            for render_only in (False, True):
                circuit = compile_circuit(source, path=path, render_only=render_only)
                self.assert_missing(lambda: analyze(circuit), line=3, path=path)

    def test_imported_definition_selects_ac_and_keeps_original_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "main.ckt"
            library = Path(directory) / "parts.ckt"
            library.write_text("circuit load(a,b) {\n    Z1() between(a,b);\n}\n")
            source = 'include("parts.ckt");\nmain { load(a,0); }'
            root.write_text(source)
            circuit = compile_circuit(source, path=root)
            self.assertEqual(circuit.mode, "ac")
            self.assert_missing(lambda: analyze(circuit), "Z1_load_1", line=2, path=library)

    def test_filling_the_placeholder_produces_real_results(self):
        source = "V1(1 Vrms) between(a,0);\nZ1() between(a,0);"
        self.assert_missing(lambda: solve_input(source), line=2)
        result = solve_input(source.replace("Z1()", "Z1(2-j2)"))
        self.assertAlmostEqual(result.network.branch_current[1], 0.25 + 0.25j)
        self.assertEqual(component_labels(result.circuit)["Z1"], ["Z1", "2 − j2 Ω"])


if __name__ == "__main__":
    unittest.main()
