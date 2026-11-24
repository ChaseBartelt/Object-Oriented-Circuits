"""Keep diagnostic lines and files attached through expansion and finalization."""

import unittest
from pathlib import Path

from circuitlab.language.builder import compile_program
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.document import CircuitProgram
from circuitlab.language.syntax import Binding
from circuitlab.model.graph import CircuitError


class DiagnosticOriginsTest(unittest.TestCase):
    def test_existing_error_line_gets_the_bound_source_path(self):
        path = Path("bad-child.ckt")
        program = CircuitProgram([
            ("transistor Q1 {", 17, Binding(path=path)),
            ("base = signal", 18, Binding(path=path)),
            ("}", 19, Binding(path=path)),
        ], {}, [], "")
        with self.assertRaises(CircuitError) as caught:
            compile_program(program, "dc", render_only=True)
        self.assertEqual(caught.exception.line, 17)
        self.assertEqual(caught.exception.path, path)
        self.assertNotIn("Line 17", caught.exception.message)

    def test_unclosed_group_uses_opening_file_when_line_numbers_repeat(self):
        path, other = Path("child.ckt"), Path("other.ckt")
        rows = [("(series)", 2, Binding(path=path)),
                ("R1 = 10", 2, Binding(path=other))]
        with self.assertRaises(CircuitError) as caught:
            compile_program(CircuitProgram(rows, {}, [], ""), "dc", render_only=True)
        self.assertEqual(caught.exception.line, 2)
        self.assertEqual(caught.exception.path, path)

    def test_probe_and_quantity_origins_keep_statement_locations(self):
        path = Path.cwd() / "origin.ckt"
        source = "f = 1k;\nV1(1V) between(a,0);\nR1(1k) between(a,0);\nmeasure = I R1;"
        circuit = compile_circuit(source, path=path, render_only=True)
        self.assertEqual(circuit.quantity_origins["f"], (1, path))
        self.assertEqual(circuit.probes[0].source_line, 4)
        self.assertEqual(circuit.probes[0].path, path)

    def test_group_probe_statement_is_not_replaced_by_group_index(self):
        source = "series(a,b) {\n R1 = 10;\n measure = V;\n}"
        circuit = compile_circuit(source, render_only=True)
        self.assertEqual(circuit.probes[0].source_line, 3)
        self.assertEqual(circuit.probes[0].line, 1)


if __name__ == "__main__":
    unittest.main()
