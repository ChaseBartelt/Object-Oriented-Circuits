"""Exercise chip declarations and the synthesizable Verilog they generate."""

import re
import unittest

from circuitlab.math.analysis import solve_input
from circuitlab.model.graph import CircuitError
from circuitlab.exporters.spice import export_spice
from circuitlab.language.syntax import has_chip
from circuitlab.exporters.verilog import export_verilog


NAND = """chip NAND(a, b, y) {
    a, b = input;
    y = output;
    y = !(a & b);
}
"""


class VerilogExportTest(unittest.TestCase):
    def test_single_and_grouped_input_declarations_are_equivalent(self):
        verilog = export_verilog(NAND)
        singles = NAND.replace("a, b = input;", "a = input;\n    b = input;")
        self.assertEqual(verilog, export_verilog(singles))
        self.assertRegex(
            verilog,
            r"module NAND\s*\(\s*input(?:\s+wire)?\s+a\s*,\s*"
            r"(?:input(?:\s+wire)?\s+)?b\s*,\s*output(?:\s+wire)?\s+y\s*\)\s*;",
        )
        self.assertRegex(verilog, r"assign\s+y\s*=\s*!\s*\(\s*a\s*&\s*b\s*\)\s*;")
        self.assertEqual(verilog.rstrip().splitlines()[-1], "endmodule")

    def test_multiple_chips_preserve_port_order_and_one_bit_constants(self):
        source = NAND + """
        chip Combine(y, a, b, carry) {
            b, a = input;
            carry, y = output;
            y = a ^ b | carry & 0;
            carry = a & b & 1;
        }
        """
        verilog = export_verilog(source)
        self.assertEqual(re.findall(r"\bmodule\s+(\w+)", verilog), ["NAND", "Combine"])
        self.assertRegex(
            verilog,
            r"module Combine\s*\(\s*output(?:\s+wire)?\s+y\s*,\s*"
            r"input(?:\s+wire)?\s+a\s*,\s*(?:input(?:\s+wire)?\s+)?b\s*,\s*"
            r"output(?:\s+wire)?\s+carry\s*\)",
        )
        self.assertIn("1'b0", verilog)
        self.assertIn("1'b1", verilog)
        self.assertRegex(
            verilog,
            r"assign\s+y\s*=\s*[^;]*a\s*\^\s*b[^;]*\|[^;]*carry\s*&\s*1'b0",
        )

    def test_reports_invalid_statements_on_source_line(self):
        for statement in (
            "a = output;",
            "z = input;",
            "y = a & missing;",
            "a = a & b; y = b;",
            "y = !a + b;",
            "y = a && b;",
            "y = 2;",
            "y = (a & b;",
        ):
            source = NAND.replace("y = !(a & b);", statement)
            with self.subTest(statement=statement):
                with self.assertRaises(CircuitError) as failure:
                    export_verilog(source)
                self.assertEqual(failure.exception.line, 4)

    def test_logical_not_requires_exclamation_mark(self):
        source = NAND.replace("!(a & b)", "!a & b")
        self.assertIn("assign y = !a & b;", export_verilog(source))
        with self.assertRaisesRegex(CircuitError, "Use ! for logical NOT"):
            export_verilog(source.replace("!a", "~a"))

    def test_rejects_invalid_chip_definitions(self):
        bad_sources = (
            NAND.replace("(a, b, y)", "(a, a, y)"),
            NAND.replace("a, b = input;", "a = input;"),
            NAND.replace("    y = !(a & b);\n", ""),
            NAND.replace("y = !(a & b);", "y = a; y = b;"),
            NAND.replace("y = !(a & b);", "y = register(a); y = !y;"),
            NAND.replace("y = !(a & b);", "y = !y;"),
            "chip Feedback(a,b) { a,b = output; a = !b; b = !a; }",
            NAND.replace("chip NAND", "chip module"),
            "chip Gate(wire, y) { wire = input; y = output; y = wire; }",
            NAND + NAND,
            NAND.rstrip().removesuffix("}"),
            NAND + "R1 a y 1k",
            "V1 a 0 5\nR1 a 0 1k",
        )
        for source in bad_sources:
            with self.subTest(source=source):
                with self.assertRaises(CircuitError):
                    export_verilog(source)

    def test_chip_detection_preserves_legacy_uses_of_the_name_chip(self):
        self.assertTrue(has_chip("# First chip\n" + NAND))
        analog_method = """# chip NAND(a, b, y) { }
        V1 in 0 6
        circuit chip(a, b) { R1(1k) between(a, b); }
        main { chip(in, 0); }
        """
        analog_part = "V1 in 0 6\nchip in 0 1u\n"
        analog_wire = "V = 5\n(series)\nR1 = 1k\nchip\nR2 = 1k\n(end series)"
        self.assertFalse(has_chip("(series)\nchip =1u\n(end series)"))
        for analog in (analog_method, analog_part, analog_wire):
            with self.subTest(analog=analog):
                self.assertFalse(has_chip(analog))
                self.assertIsNotNone(solve_input(analog).network)

    def test_analog_backend_directs_chip_files_to_verilog(self):
        with self.assertRaisesRegex(CircuitError, "Export Verilog"):
            export_spice(NAND)
