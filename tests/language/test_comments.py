"""Source comments preserve literal paths and physical diagnostics."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.comments import mask_comments
from circuitlab.model.graph import CircuitError


class CommentTest(unittest.TestCase):
    def test_multiline_pin_list_quotes_preserve_literal_markers_and_outside_comments(self):
        literal = 'pins[2:0]="a,\n// literal, /* literal */ # literal\ny";'
        source = literal + ' // outside\ninclude("path#1.ckt"); // later\n'
        masked = mask_comments(source)
        self.assertTrue(masked.startswith(literal))
        self.assertNotIn("outside", masked)
        self.assertNotIn("later", masked)
        self.assertIn('include("path#1.ckt")', masked)
        self.assertEqual(masked.count("\n"), source.count("\n"))

    def test_unterminated_pin_list_does_not_hide_later_comments_or_include_paths(self):
        source = 'pins[1:0]="a,\n// outside\n}\ninclude("path#1.ckt"); // later\n'
        masked = mask_comments(source)
        self.assertNotIn("outside", masked)
        self.assertNotIn("later", masked)
        self.assertIn('include("path#1.ckt")', masked)
        self.assertEqual(masked.count("\n"), source.count("\n"))

    def test_block_comments_do_not_change_mode_or_diagnostic_line(self):
        source = "/* f = 1kHz\n} */\nR1(1k /* nominal */) between(in, 0); // Vdc = 2\n"
        circuit = compile_circuit(source)
        self.assertEqual(circuit.mode, "dc")
        self.assertEqual(circuit.components[0].value, 1000)
        with self.assertRaises(CircuitError) as failure:
            compile_circuit(source + "R2(bad) between(in, 0);")
        self.assertEqual(failure.exception.line, 4)

    def test_includes_ignore_comments_and_preserve_quoted_hashes(self):
        entry = Path.cwd() / "main.ckt"
        source = ('/* include("missing.ckt");\n'
                  '*/ include("divider#1.ckt"); /* another block\n'
                  '*/ main { Divider(in, 0); }\n')
        def read_source(path):
            self.assertEqual(path.name, "divider#1.ckt")
            return "circuit Divider(a,b) { R1(1k) between(a,b); }"
        circuit = compile_circuit(source, path=entry, read_source=read_source)
        self.assertEqual(circuit.components[0].terminal_nets, ("in", "0"))

    def test_digital_blocks_use_the_same_comment_syntax(self):
        verilog = export_verilog("""
            chip Logic(a,y) {
                a = input; y = output; // ignore unknown();
                /* y = nope; }
                   y = also_missing; */
                y = !a;
            }
        """)
        self.assertIn("module Logic", verilog)
        self.assertNotIn("missing", verilog)
