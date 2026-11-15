"""Exercise the independent viewer and its presentation-only persistence."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPlainTextEdit

from circuitlab.exporters.ltspice import export_hierarchy
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.persistence import pack_layout, placement_path
from circuitlab.viewer.window import ViewerWindow

ROOT = Path(__file__).resolve().parents[1]
DIVIDER = "V1(5) between(supply, 0);\nR1(1k) between(supply, out);\nR2(1k) between(out, 0);\n"


class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "viewer circuit.ckt"
        self.path.write_text(DIVIDER, encoding="utf-8")
        self.window = ViewerWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_no_editor_widgets_or_legacy_module_needed(self):
        self.assertFalse(self.window.findChildren(QPlainTextEdit))
        self.assertFalse(hasattr(self.window, "editor"))
        self.assertFalse(hasattr(self.window, "editor_tabs"))
        script = """
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from circuitlab import gui
app = QApplication([])
window = gui.ViewerWindow()
assert 'circuitlab.viewer.legacy' not in sys.modules
assert 'circuitlab.viewer.editor' not in sys.modules
assert 'circuitlab.editor' not in sys.modules
window.close()
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_save_restores_geometry_and_does_not_overwrite_ide_source(self):
        self.assertTrue(self.window.open_path(self.path))
        drawing = self.window.schematic.layout
        key = None, "auto"
        self.window.schematic.remember_placement(key)
        changed = replace(drawing, label_offsets={("component", "R1"): (1.5, 2)})
        self.window.schematic.manual_layouts[key] = changed
        self.window.schematic.redraw(self.window.pins)
        self.window.placement_changed()
        latest = DIVIDER.replace("V1(5)", "V1(8)")
        self.path.write_text(latest, encoding="utf-8")
        self.assertTrue(self.window.save())
        self.assertEqual(self.path.read_text(encoding="utf-8"), latest)
        self.assertTrue(self.window.open_path(self.path))
        restored = self.window.schematic.layout
        self.assertEqual(restored.label_offsets["component", "R1"], (1.5, 2))
        self.assertTrue(preserves_nets(self.window.schematic.circuit, restored))

    def test_named_probe_overrides_persist_without_source_edits(self):
        source = DIVIDER + '# @pin {"node": "out", "name": "Old output"}\n'
        self.path.write_text(source, encoding="utf-8")
        self.window.open_path(self.path)
        self.window.write_pin("out", None)
        self.window.write_pin("supply", "Power")
        self.assertEqual(self.window.pins, {"supply": "Power"})
        self.assertTrue(self.window.save())
        self.assertEqual(self.path.read_text(encoding="utf-8"), source)
        self.window.open_path(self.path)
        self.assertEqual(self.window.pins, {"supply": "Power"})
        saved = json.loads(placement_path(self.path).read_bytes())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["pin_overrides"], {"out": None, "supply": "Power"})

    def test_background_missing_file_keeps_current_document_without_dialog(self):
        self.window.open_path(self.path)
        old_session = self.window.path, self.window.session.source, self.window.solution
        with patch("circuitlab.viewer.window.QMessageBox.warning") as warning:
            self.assertFalse(self.window.refresh_from_editor(self.path.with_name("missing.ckt")))
        warning.assert_not_called()
        self.assertEqual((self.window.path, self.window.session.source, self.window.solution), old_session)

    def test_file_switch_can_save_dirty_layout_to_its_original_file(self):
        self.window.open_path(self.path)
        self.window.write_pin("out", "Output")
        other = self.path.with_name("second.ckt")
        other.write_text(DIVIDER.replace("V1(5)", "V1(6)"))
        from PySide6.QtWidgets import QMessageBox
        with patch("circuitlab.viewer.window.QMessageBox.question", return_value=QMessageBox.StandardButton.Save):
            self.assertTrue(self.window.open_from_editor(other))
        self.assertEqual(self.window.path, other)
        self.assertTrue(placement_path(self.path).exists())
        self.assertFalse(placement_path(other).exists())
        self.assertEqual(self.window.pins, {})

    def test_renderable_model_error_shows_diagnostics_and_scope(self):
        self.path.write_text('include("@chips/converters/ADS1115.ckt");\nmain { ADS1115(a,b,0,c,d,e,f,v,sda,scl); }')
        self.window.open_path(self.path)
        self.assertIsNotNone(self.window.schematic.circuit)
        self.assertIsNone(self.window.solution)
        self.assertIn("simulation model", self.window.status.text())

    def test_spice_and_hierarchical_export_keep_source_and_companions(self):
        self.path.write_text("chip block(a,b) { R1(1k) between(a,b); }\nV1(5) between(a,0);\nmain { block(a,0); }\n")
        self.window.open_path(self.path)
        original = self.path.read_bytes()
        target = self.path.with_suffix(".cir")
        with patch("circuitlab.viewer.exports.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            self.assertTrue(self.window.export_netlist())
        self.assertIn("R1_block_1", target.read_text())
        target = self.path.with_suffix(".asc")
        with patch("circuitlab.viewer.exports.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            self.assertTrue(self.window.export_ltspice())
        self.assertTrue(target.exists())
        self.assertTrue(list(target.parent.glob("ec_*.asy")))
        self.assertTrue(list(target.parent.glob("ec_*.asc")))
        self.assertEqual(self.path.read_bytes(), original)

    def test_digital_view_and_verilog_export(self):
        self.path.write_text("chip invert(a,y) { a = input; y = output; y = !a; }")
        self.window.open_path(self.path)
        self.assertIsNotNone(self.window.schematic.circuit)
        self.assertIsNone(self.window.solution)
        target = self.path.with_suffix(".v")
        with patch("circuitlab.viewer.exports.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            self.assertTrue(self.window.export_verilog())
        self.assertIn("assign y = !a;", target.read_text())

    def test_included_chip_export_restores_saved_collapsed_child_layout(self):
        definition = self.path.with_name("block.ckt")
        definition.write_text("chip block(a,b) { R1(1k) between(a,b); }")
        self.path.write_text('include("block.ckt");\nV1(5) between(a,0);\nmain { block(a,0); }\n')
        self.window.open_path(self.path)
        schematic = self.window.schematic
        name = "@method:0"
        schematic.toggle_package(name)
        key = name, "auto"
        original = schematic.level_layouts[key]
        part = original.symbols[0][0]
        edited = replace(original, label_offsets={("component", part.name): (2.5, 1.25)})
        schematic.manual_layouts[key] = edited
        self.window.placement_changed()
        self.assertTrue(self.window.save())
        self.window.open_path(self.path)
        self.assertNotIn(name, schematic.expanded)
        self.assertIn(key, schematic.placement_records)
        target = self.path.with_suffix(".asc")
        with patch("circuitlab.viewer.exports.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                patch("circuitlab.viewer.window.export_hierarchy", wraps=export_hierarchy) as exported:
            self.assertTrue(self.window.export_ltspice())
        details = exported.call_args.args[2]
        self.assertEqual(details[name].label_offsets["component", part.name], (2.5, 1.25))
        self.assertEqual(pack_layout(details[name]), pack_layout(edited))
        self.assertNotIn(name, schematic.expanded)
