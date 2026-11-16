import base64
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from circuitlab.language.linked import read_includes
from circuitlab.tooling.hover import HoverRenderer


class HoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "hover.ckt"
        self.renderer = HoverRenderer()
        for widget in (self.renderer.device, self.renderer.view):
            self.addCleanup(widget.deleteLater)

    def test_hover_worker_does_not_load_the_source_editor(self):
        script = """
import sys
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from circuitlab.tooling.hover import HoverRenderer
app = QApplication([])
renderer = HoverRenderer()
assert 'circuitlab.viewer.editor' not in sys.modules
assert not any(isinstance(widget, QPlainTextEdit) for widget in app.allWidgets())
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def preview(self, source, token, offset=0):
        index = source.index(token) + offset
        before = source[:index]
        return self.renderer.preview({"file": str(self.path), "source": source,
                                      "line": before.count("\n"), "column": len(before.rsplit("\n", 1)[-1])})

    def assert_png(self, result):
        self.assertTrue(base64.b64decode(result["image"]).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_builtin_gate_and_custom_chip_pinout_images(self):
        gate = self.preview("nor(vcc, gnd, a, b, y);", "nor")
        self.assert_png(gate)
        self.assertIn("nor(", gate["text"])
        chip = self.preview("chip example(vcc, a, y, gnd) {\nR1(1k) between(a,y);\n}\nexample(vcc,a,y,0);", "example(vcc,a")
        self.assert_png(chip)
        self.assertIn("example(vcc, a, y, gnd)", chip["text"])

    def test_wire_preview_highlights_the_selected_net_and_excludes_parallel_branch(self):
        source = """V1(5) between(start, 0);
R1(1k) between(start, middle);
R2(1k) between(middle, 0);
R3(1k) between(middle, split);
R4(1k) between(split, 0);
"""
        result = self.preview(source, "R3(1k) between(middle", len("R3(1k) between("))
        self.assert_png(result)
        self.assertEqual(result["nets"], ["middle"])
        self.assertTrue({"R1", "R3", "R4"}.issubset(result["components"]))
        self.assertNotIn("R2", result["components"])

    def test_imported_chip_and_unsaved_text_use_current_definitions(self):
        child = self.path.with_name("chip.ckt")
        child.write_text("chip demo(vcc,a,y,gnd) { R1(1k) between(a,y); }", encoding="utf-8")
        source = 'include("chip.ckt");\ndemo(supply,input,out,0);'
        with patch("circuitlab.tooling.hover.read_includes", wraps=read_includes) as read:
            result = self.preview(source, "demo(")
            self.assert_png(result)
            self.assertIn("demo(vcc, a, y, gnd)", result["text"])
            self.assertEqual(read.call_count, 1)
            child.write_text("chip demo(power,in1,out1,ground) { R1(1k) between(in1,out1); }", encoding="utf-8")
            self.assertIn("demo(power, in1, out1, ground)", self.preview(source, "demo(")["text"])
            self.assertEqual(read.call_count, 2)

    def test_short_chip_import_provides_its_pinout_preview(self):
        source = (Path(__file__).resolve().parents[1] / "examples/chip_library_nand.ckt").read_text()
        result = self.preview(source, "SN74HC00(")
        self.assert_png(result)
        self.assertIn("SN74HC00(", result["text"])

    def test_grouped_gate_input_highlights_the_correct_wire(self):
        source = (
            "VCC(5) between(vcc, 0);\n"
            "VA(5) between(a, 0);\nVB(0) between(b, 0);\n"
            "nor(vcc, (a, b), out);"
        )
        result = self.preview(source, "b), out")
        self.assert_png(result)
        self.assertEqual(result["nets"], ["b"])
        self.assertIn("Wire: b", result["text"])

    def test_comments_and_invalid_circuit_do_not_crash(self):
        self.assertIsNone(self.preview("# nor(vcc,gnd,a,b,y);", "nor"))
        result = self.preview("R1(bad_value) between(a, b);", "a, b")
        self.assertIn("Wire: a", result["text"])
        self.assertIn("Preview unavailable", result["text"])
        self.assertNotIn("image", result)
