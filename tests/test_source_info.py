"""Editor recognition remains useful without importing a graphical editor."""

from pathlib import Path
import subprocess
import sys
import unittest

from circuitlab.tooling.source_info import SourceInfo, accepts_calls, gate_inputs, methods


class SourceInfoTest(unittest.TestCase):
    def test_import_and_lookup_do_not_load_qt(self):
        script = """
import sys
from circuitlab.tooling.source_info import SourceInfo
assert SourceInfo('not(vcc, a, y);').device_at(0, 1) == ('builtin', 'NOT', 1)
assert not any(name.startswith('PySide6') for name in sys.modules)
assert not any(name.startswith('circuitlab.viewer') for name in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_incomplete_definitions_and_legacy_default_terminals(self):
        source = "series pair { R1 = 1k; }\nchip demo(vcc, a, y, gnd) {\n"
        self.assertEqual(methods(source), {
            "pair": ("series", "pair", "a, b"),
            "demo": ("chip", "demo", "vcc, a, y, gnd"),
        })
        self.assertTrue(accepts_calls(source))
        self.assertTrue(accepts_calls("main {\nfor (int i=0; i<3; i++) {\n"))
        self.assertFalse(accepts_calls("(battery supply)\nV = 5\n"))
        self.assertFalse(accepts_calls("main {\n}\n"))

    def test_imports_refresh_and_local_declarations_take_precedence(self):
        included = ["chip demo(vcc,a,y,gnd) { R1(1k) between(a,y); }"]
        info = SourceInfo('include("parts.ckt");\ndemo(vcc,a,y,0);', lambda _: included)
        self.assertEqual(info.device_at(1, 1), ("chip", "demo", ("vcc", "a", "y", "gnd")))
        included[0] = "chip demo(power,input,output,ground) { R1(1k) between(input,output); }"
        self.assertIn("demo(power, input, output, ground)", info.hover_help(1, 1))
        local = SourceInfo(info.source + "\nchip demo(p,a,b,g) {", lambda _: included)
        self.assertEqual(local.method_signatures()["demo"][2], "p, a, b, g")

    def test_device_help_and_declared_pin_orders(self):
        source = "chip timer(gnd, trig, out, vcc) {\nnot(vcc,trig,out);\n}\ntimer(0,a,y,vcc);"
        info = SourceInfo(source)
        self.assertEqual(info.device_at(0, 6), ("chip", "timer", ("gnd", "trig", "out", "vcc")))
        self.assertEqual(info.device_at(3, 2), ("chip", "timer", ("gnd", "trig", "out", "vcc")))
        self.assertEqual(info.device_at(1, 1), ("builtin", "NOT", 1))
        self.assertIn("transistor circuitry", info.hover_help(1, 1))
        self.assertIn("Non-inverting", SourceInfo("op1(a,b,y);").hover_help(0, 1))
        self.assertIn("base, collector, emitter", SourceInfo("transQ1(b,c,e,npn);").hover_help(0, 2))
        self.assertEqual(gate_inputs("nor", "vcc, (a, b, c), y"), 3)
        self.assertEqual(gate_inputs("and", "vcc, gnd, a, b, y"), 2)

    def test_modern_legacy_and_grouped_wire_locations(self):
        rows = [
            ("R1(1k) between(start, middle); R2 middle 0 1k;", "start", (1, "R1", 0, "start")),
            ("R1(1k) between(start, middle); R2 middle 0 1k;", "middle 0", (1, "R2", 0, "middle")),
            ("nor(vcc, (a, b), out);", "b)", (1, "nor", 2, "b")),
            ("transQ1(base, collector, emitter, npn);", "collector", (1, "transQ1", 1, "collector")),
        ]
        for source, token, expected in rows:
            with self.subTest(source=source, token=token):
                self.assertEqual(SourceInfo(source).wire_at(0, source.index(token)), expected)
        self.assertIsNone(SourceInfo("# R1(1k) between(a,b);").wire_at(0, 18))
        self.assertIsNone(SourceInfo("R1(1k) between(a,").wire_at(0, 14))

    def test_utf16_columns_and_crlf_lines_match_editor_coordinates(self):
        first = 'include("😀.ckt"); nor(vcc, (a, b), out);'
        info = SourceInfo(first + "\r\nR1(1k) between(out, 0);\r\n")
        device_column = len(first[:first.index("nor")].encode("utf-16-le")) // 2
        wire_column = len(first[:first.index("out")].encode("utf-16-le")) // 2
        self.assertEqual(info.device_at(0, device_column), ("builtin", "NOR", 2))
        self.assertEqual(info.wire_at(0, wire_column), (1, "nor", 3, "out"))
        self.assertEqual(info.wire_at(1, 15), (2, "R1", 0, "out"))
        emoji = first.index("😀")
        self.assertIsNone(info.position(0, emoji + 1))
        self.assertEqual(info.position(2, 0), ("", 0))

    def test_outside_document_positions_are_ignored(self):
        info = SourceInfo("nor(vcc,a,y);")
        for line, column in ((-1, 0), (1, 0), (0, -1), (0, 100), (0, 0.5)):
            with self.subTest(line=line, column=column):
                self.assertIsNone(info.device_at(line, column))
                self.assertIsNone(info.wire_at(line, column))
                self.assertEqual(info.hover_help(line, column), "")


if __name__ == "__main__":
    unittest.main()
