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
    def test_internal_state_defaults_to_zero_without_changing_scalar_ports(self):
        source = """chip StateBits(a, y, z) {
            a = input;
            y, z = output;
            phase[32];
            blink_counter[25];
            led_state;
            y = a ^ phase[31] ^ blink_counter[24];
            z = !led_state;
        }
        """
        verilog = export_verilog(source)
        self.assertIn("reg [31:0] phase = 32'd0;", verilog)
        self.assertIn("reg [24:0] blink_counter = 25'd0;", verilog)
        self.assertIn("reg led_state = 1'b0;", verilog)
        self.assertIn("input wire a", verilog)
        self.assertIn("output wire y", verilog)
        self.assertIn("assign y = a ^ phase[31] ^ blink_counter[24];", verilog)
        self.assertIn("assign z = !led_state;", verilog)
        self.assertNotRegex(verilog, r"\b(always|posedge|negedge)\b")

    def test_explicit_unsigned_state_initializers_fit_the_declared_width(self):
        source = """chip InitialState(y, z) {
            y, z = output;
            phase[32] = 100;
            max_value[32] = 4294967295;
            blink_counter[25] = 24999999;
            led_state = 1;
            y = phase[6] & max_value[31] & blink_counter[24];
            z = led_state;
        }
        """
        verilog = export_verilog(source)
        self.assertIn("reg [31:0] phase = 32'd100;", verilog)
        self.assertIn("reg [31:0] max_value = 32'd4294967295;", verilog)
        self.assertIn("reg [24:0] blink_counter = 25'd24999999;", verilog)
        self.assertIn("reg led_state = 1'b1;", verilog)

    def test_state_declarations_accept_smallest_and_largest_supported_widths(self):
        source = """chip WidthLimits(y) {
            y = output;
            smallest[1] = 1;
            largest[4096];
            y = smallest[0] | largest[4095];
        }
        """
        verilog = export_verilog(source)
        self.assertIn("reg [0:0] smallest = 1'd1;", verilog)
        self.assertIn("reg [4095:0] largest = 4096'd0;", verilog)

    def test_state_decimal_width_value_and_index_allow_digit_separators(self):
        source = """chip DigitGroups(y) {
            y = output;
            phase[1_024] = 1_024;
            y = phase[1_023];
        }
        """
        verilog = export_verilog(source)
        self.assertIn("reg [1023:0] phase = 1024'd1024;", verilog)
        self.assertIn("assign y = phase[1023];", verilog)

    def test_maximum_width_initializer_is_not_truncated_and_overflow_is_rejected(self):
        maximum = (1 << 4096) - 1
        source = (f"chip LargeState(y) {{ y = output; phase[4096] = {maximum}; "
                  "y = phase[4095]; }")
        verilog = export_verilog(source)
        literal = re.search(r"reg \[4095:0\] phase = 4096'd([0-9]+);", verilog)
        self.assertIsNotNone(literal)
        self.assertEqual(int(literal[1]), maximum)
        for invalid in (str(maximum + 1), '9' * 5000):
            with self.subTest(digits=len(invalid)):
                with self.assertRaises(CircuitError):
                    export_verilog(source.replace(str(maximum), invalid))

    def test_state_comments_and_windows_line_endings_preserve_diagnostics(self):
        source = ("chip Commented(y) {\r\n"
                  " y = output;\r\n"
                  " phase[32] = 100; # initialized data\r\n"
                  " # a misleading declaration in a comment: phase[0];\r\n"
                  " y = phase[31]; # read one bit\r\n"
                  "}\r\n")
        self.assertEqual(export_verilog(source), export_verilog(source.replace('\r\n', '\n')))
        with self.assertRaises(CircuitError) as failure:
            export_verilog(source.replace('y = phase[31]', 'y = phase[32]'))
        self.assertEqual(failure.exception.line, 5)

    def test_output_expressions_can_reference_later_state_declarations(self):
        source = """chip ForwardState(y) {
            y = output;
            y = phase[31] | led_state;
            phase[32] = 100;
            led_state;
        }
        """
        verilog = export_verilog(source)
        self.assertIn("assign y = phase[31] | led_state;", verilog)
        self.assertLess(verilog.index("reg [31:0] phase"), verilog.index("assign y"))

    def test_rejects_duplicate_state_and_port_name_collisions(self):
        declarations = (
            "a;", "y[2];", "a = 1;", "phase[32]; phase[32];",
            "flag; flag;", "flag; flag = 1;", "flag = 1; flag;",
            "flag; flag[2];", "phase[32]; phase;", "always;",
            "phase[32] = input;",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                with self.assertRaises(CircuitError):
                    export_verilog(f"chip Bad(a,y) {{ a = input; y = output; {declaration} y = a; }}")

    def test_rejects_invalid_widths_and_unsigned_initial_values_on_declaration_line(self):
        declarations = (
            "phase[0];", "phase[-1];", "phase[1.5];", "phase[2+2];",
            "phase[width];", "phase[4097];", "phase[0x20];", "phase[];",
            "phase[1__024];", "phase[_32];", "phase[32_];",
            "phase[32] = -1;", "phase[32] = 4294967296;",
            "phase[32] = 1.5;", "phase[32] = 1k;", "phase[32] = 0x10;",
            "phase[32] = other;",
            "phase[32] = 1__024;", "phase[32] = _100;", "phase[32] = 100_;",
            "flag = -1;", "flag = 2;",
        )
        for declaration in declarations:
            source = f"chip Bad(y) {{\n y = output;\n {declaration}\n y = 0;\n}}\n"
            with self.subTest(declaration=declaration):
                with self.assertRaises(CircuitError) as failure:
                    export_verilog(source)
                self.assertEqual(failure.exception.line, 3)

    def test_vector_reads_require_one_in_range_constant_bit_index(self):
        for expression in (
            "phase", "phase[32]", "phase[-1]", "phase[a]",
            "phase[1+1]", "phase[31:0]", "phase[]", "phase[1.5]",
            "phase[0][0]", "phase[1__0]", "phase[_1]", "phase[1_]", "a[0]", "flag[0]",
        ):
            source = ("chip Bad(a,y) {\n a = input; y = output;\n phase[32]; flag;\n"
                      f" y = {expression};\n}}\n")
            with self.subTest(expression=expression):
                with self.assertRaises(CircuitError) as failure:
                    export_verilog(source)
                self.assertEqual(failure.exception.line, 4)

    def test_state_declarations_do_not_silently_enable_clocked_updates(self):
        updates = (
            "phase = phase + 1;", "phase = 100;", "phase[0] = 1;",
            "flag = !flag;", "phase++;", "phase += 1;",
            "if (clk) { flag = 1; }", "always(clk) { flag = 1; }",
            "on rising(clk) { flag = 1; }",
        )
        for update in updates:
            source = ("chip Bad(clk,y) { clk = input; y = output; phase[32]; flag; "
                      f"{update} y = phase[31] | flag; }}")
            with self.subTest(update=update):
                with self.assertRaises(CircuitError):
                    export_verilog(source)

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
