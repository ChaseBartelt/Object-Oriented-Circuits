"""Exercise the independent viewer and its presentation-only persistence."""
# Exercise the IDE-first shell with its real source transaction and persistence machinery.
# Dirty values/layouts, file changes, and included definitions must retain ownership through
# acknowledgement, Discard, failed opens, and asynchronous refreshes.

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPlainTextEdit, QMessageBox
from PySide6.QtCore import QTimer, Qt

from circuitlab.exporters.ltspice import export_hierarchy
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.persistence import (pack_layout, placement_path, layout_payload,
                                         strip_placement, read_placement, write_placement)
from circuitlab.tooling.source_edits import edit_value
from circuitlab.viewer.window import ViewerWindow

ROOT = Path(__file__).resolve().parents[2]
DIVIDER = "V1(5) between(supply, 0);\nR1(1k) between(supply, out);\nR2(1k) between(out, 0);\n"


class ViewerTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "viewer circuit.ckt"
        self.path.write_text(DIVIDER, encoding="utf-8")
        self.window = ViewerWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.source_tools.checkpoint()
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def change_value(self, name, value):
        plan = edit_value(self.window.schematic.circuit, name, value,
                          self.window.path, self.window.session.source,
                          lambda path: path.read_text(encoding='utf-8'))
        self.window.source_tools.apply(plan)
        return plan

    def test_scope_toggle_opens_beside_schematic_and_keeps_running(self):
        window = self.window
        window.open_path(self.path)
        self.app.processEvents()
        self.assertEqual(window.views.currentIndex(), 0)
        self.assertTrue(window.schematic.isVisible())
        self.assertFalse(window.scope.isVisible())
        schematic_page = window.views.currentWidget()
        self.assertEqual(window.schematic.width(), schematic_page.width())
        self.assertEqual(window.scope_toggle.text(), "Oscilloscope")
        self.assertIs(window.views.cornerWidget(Qt.Corner.TopRightCorner), window.schematic_header)
        tabs = window.views.tabBar().geometry()
        for control in (window.wire_style, window.scope_toggle):
            y = control.mapTo(window.views, control.rect().center()).y()
            self.assertTrue(tabs.top() <= y <= tabs.bottom())
        window.views.setCurrentWidget(window.results)
        self.assertFalse(window.wire_style.isVisible())
        window.scope_toggle.click()
        self.app.processEvents()
        self.assertIs(window.views.currentWidget(), schematic_page)
        self.assertTrue(window.scope.isVisible())
        self.assertTrue(window.schematic.isVisible())
        self.assertLess(window.schematic.width(), schematic_page.width())
        self.assertGreater(window.scope_sidebar.panel.x(), window.schematic.geometry().right())
        placement = pack_layout(window.schematic.layout)
        window.scope.run.click()
        simulation = window.scope.simulation
        self.assertIsNotNone(simulation)
        window.scope.pending[1].result(timeout=5)
        window.scope.advance()
        self.assertTrue(window.scope.waveform.samples)
        window.scope_toggle.click()
        self.app.processEvents()
        self.assertIs(window.views.currentWidget(), schematic_page)
        self.assertFalse(window.scope.isVisible())
        self.assertEqual(window.schematic.width(), schematic_page.width())
        self.assertIs(window.scope.simulation, simulation)
        self.assertTrue(window.scope.timer.isActive())
        window.scope_action.trigger()
        self.app.processEvents()
        self.assertTrue(window.scope_toggle.isChecked())
        self.assertTrue(window.scope.isVisible())
        self.assertIs(window.scope.simulation, simulation)
        self.assertEqual(window.views.currentIndex(), 0)
        self.assertEqual(pack_layout(window.schematic.layout), placement)
        self.assertEqual(self.path.read_text(), DIVIDER)

    def test_close_discard_restores_all_value_changes_since_open(self):
        self.window.open_path(self.path)
        self.change_value('R1', '2k')
        self.change_value('R1', '3k')
        self.change_value('R2', '4k')
        self.assertIn('R1(3k)', self.path.read_text())
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.window.close())
        self.assertEqual(self.path.read_text(), DIVIDER)
        self.assertFalse(self.window.source_tools.value_changes)

    def test_save_establishes_value_checkpoint_and_discard_keeps_its_layout(self):
        self.window.open_path(self.path)
        self.change_value('R1', '2k')
        self.window.write_pin('out', 'Saved output')
        self.assertTrue(self.window.save())
        saved_layout = layout_payload(self.path.read_text())
        self.assertFalse(self.window.source_tools.value_changes)
        self.change_value('R1', '3k')
        self.change_value('R2', '4k')
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.window.confirm_edits())
        self.assertEqual(strip_placement(self.path.read_text()), DIVIDER.replace('R1(1k)', 'R1(2k)'))
        self.assertEqual(layout_payload(self.path.read_text()), saved_layout)
        self.assertFalse(self.window.source_tools.value_changes)

    def test_discard_after_undo_returns_to_explicitly_saved_value(self):
        self.window.open_path(self.path)
        self.change_value('R1', '2k')
        self.assertTrue(self.window.save())
        self.window.schematic.undo()
        self.assertIn('R1(1k)', strip_placement(self.path.read_text()))
        self.assertTrue(self.window.source_tools.discard_values())
        self.assertEqual(strip_placement(self.path.read_text()), DIVIDER.replace('R1(1k)', 'R1(2k)'))

    def test_discard_restores_values_in_included_definition(self):
        definition = self.path.with_name('load.ckt')
        original = 'chip load(a,b) { R1(1k) between(a,b); }\n'
        definition.write_text(original)
        source = 'include("load.ckt");\nV1(5) between(a,0);\nmain { load(a,0); }\n'
        self.path.write_text(source)
        self.window.open_path(self.path)
        resistor = next(part.name for part in self.window.schematic.circuit.components if part.kind == 'R')
        self.change_value(resistor, '2k')
        self.change_value(resistor, '3k')
        self.assertIn('R1(3k)', definition.read_text())
        self.assertTrue(self.window.source_tools.discard_values())
        self.assertEqual(definition.read_text(), original)
        self.assertEqual(self.path.read_text(), source)

    def test_ide_discard_waits_for_ack_and_rejection_keeps_viewer_open(self):
        self.window.open_path(self.path)
        self.change_value('R1', '2k')
        changed = self.path.read_text()
        tools = self.window.source_tools
        requests = []
        def reject(request):
            requests.append(request)
            QTimer.singleShot(0, lambda: tools.result({'id': request['id'], 'ok': False, 'error': 'Unsaved IDE changes'}))
        tools.sender = reject
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard), \
             patch('circuitlab.viewer.source_editing.QMessageBox.warning'):
            self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.path.read_text(), changed)
        self.assertTrue(tools.value_changes)
        self.assertIsNone(tools.pending)

        def accept(request):
            requests.append(request)
            def acknowledge():
                self.assertTrue(self.window.isVisible())
                self.assertEqual(request['before'].replace('\r\n', '\n'), changed)
                self.assertEqual(request['after'].replace('\r\n', '\n'), DIVIDER)
                self.path.write_text(request['after'], encoding='utf-8', newline='')
                tools.result({'id': request['id'], 'ok': True})
            QTimer.singleShot(0, acknowledge)
        tools.sender = accept
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.window.close())
        self.assertEqual(self.path.read_text(), DIVIDER)
        self.assertFalse(tools.value_changes)
        self.assertEqual(len(requests), 2)

    def test_discard_does_not_overwrite_external_edits(self):
        self.window.open_path(self.path)
        self.change_value('R1', '2k')
        external = self.path.read_text().replace('R2(1k)', 'R2(7k)')
        self.path.write_text(external)
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard), \
             patch('circuitlab.viewer.source_editing.QMessageBox.warning') as warning, \
             patch.object(self.window.source_tools, 'snapshot') as snapshot:
            self.assertFalse(self.window.close())
        warning.assert_called_once()
        snapshot.assert_not_called()
        self.assertEqual(self.path.read_text(), external)
        self.assertTrue(self.window.source_tools.value_changes)

    def test_partial_discard_refreshes_restored_values_when_second_file_is_rejected(self):
        definition = self.path.with_name('load.ckt')
        original_definition = 'chip load(a,b) { R1(1k) between(a,b); }\n'
        definition.write_text(original_definition)
        source = 'include("load.ckt");\nV1(5) between(a,0);\nR2(1k) between(a,b);\nmain { load(b,0); }\n'
        self.path.write_text(source)
        self.window.open_path(self.path)
        resistor = next(part.name for part in self.window.schematic.circuit.components
                        if part.kind == 'R' and part.name != 'R2')
        self.change_value('R2', '2k')
        self.change_value(resistor, '3k')
        self.window.write_pin('b', 'Output')
        tools = self.window.source_tools
        requests = []

        def partial_accept(request):
            requests.append(request)
            def acknowledge():
                if Path(request['path']) == self.path:
                    self.path.write_text(request['after'], encoding='utf-8', newline='')
                    tools.result({'id': request['id'], 'ok': True})
                else:
                    tools.result({'id': request['id'], 'ok': False, 'error': 'Unsaved definition'})
            QTimer.singleShot(0, acknowledge)

        tools.sender = partial_accept
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard), \
             patch('circuitlab.viewer.source_editing.QMessageBox.warning'):
            self.assertFalse(self.window.close())
        self.assertEqual(len(requests), 2)
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.path.read_text(), source)
        self.assertIn('R1(3k)', definition.read_text())
        self.assertEqual(self.window.session.source.replace('\r\n', '\n'), source)
        values = {part.name: part.value for part in self.window.schematic.circuit.components}
        self.assertEqual(values['R2'], 1000)
        self.assertEqual(values[resistor], 3000)
        self.assertEqual(self.window.pins['b'], 'Output')
        self.assertEqual(set(tools.value_changes), {definition})

    def test_discard_then_failed_file_open_keeps_displayed_values_current(self):
        self.window.open_path(self.path)
        self.change_value('R1', '2k')
        missing = self.path.with_name('missing.ckt')
        with patch('circuitlab.viewer.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Discard), \
             patch('circuitlab.viewer.window.QMessageBox.warning'):
            self.assertFalse(self.window.open_from_editor(missing))
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.path, self.path)
        self.assertEqual(self.path.read_text(), DIVIDER)
        self.assertEqual(self.window.session.source.replace('\r\n', '\n'), DIVIDER)
        self.assertEqual(next(part.value for part in self.window.schematic.circuit.components
                              if part.name == 'R1'), 1000)
        self.assertFalse(self.window.source_tools.value_changes)

    def test_discard_refreshes_external_undo_even_when_linked_file_conflicts(self):
        definition = self.path.with_name('load.ckt')
        definition.write_text('chip load(a,b) { R1(1k) between(a,b); }\n')
        source = 'include("load.ckt");\nV1(5) between(a,0);\nR2(1k) between(a,b);\nmain { load(b,0); }\n'
        self.path.write_text(source)
        self.window.open_path(self.path)
        resistor = next(part.name for part in self.window.schematic.circuit.components
                        if part.kind == 'R' and part.name != 'R2')
        self.change_value('R2', '2k')
        self.change_value(resistor, '3k')
        self.path.write_text(source)  # The IDE has already undone the first edit.
        external = definition.read_text().replace('R1(3k)', 'R1(4k)')
        definition.write_text(external)
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning') as warning:
            self.assertFalse(self.window.source_tools.discard_values())
        warning.assert_called_once()
        self.assertEqual(self.window.session.source.replace('\r\n', '\n'), source)
        values = {part.name: part.value for part in self.window.schematic.circuit.components}
        self.assertEqual(values['R2'], 1000)
        self.assertEqual(values[resistor], 4000)
        self.assertEqual(definition.read_text(), external)

    def test_close_waits_for_pending_value_edit(self):
        self.window.open_path(self.path)
        tools = self.window.source_tools
        requests = []
        tools.sender = requests.append
        self.change_value('R1', '2k')
        with patch('circuitlab.viewer.window.QMessageBox.question') as question:
            self.assertFalse(self.window.close())
        question.assert_not_called()
        self.assertTrue(self.window.isVisible())
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning'):
            tools.result({'id': requests[-1]['id'], 'ok': False, 'error': 'cancelled'})

    def test_no_editor_widgets_or_legacy_module_needed(self):
        self.assertFalse(self.window.findChildren(QPlainTextEdit))
        self.assertFalse(hasattr(self.window, "editor"))
        self.assertFalse(hasattr(self.window, "editor_tabs"))
        script = """
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from circuitlab.viewer.window import ViewerWindow
app = QApplication([])
window = ViewerWindow()
assert 'circuitlab.viewer.legacy' not in sys.modules
assert 'circuitlab.viewer.editor' not in sys.modules
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
        saved_source = self.path.read_text(encoding="utf-8")
        self.assertEqual(strip_placement(saved_source), latest)
        self.assertIsNotNone(layout_payload(saved_source))
        self.assertFalse(placement_path(self.path).exists())
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
        self.assertEqual(strip_placement(self.path.read_text(encoding="utf-8")), source)
        self.window.open_path(self.path)
        self.assertEqual(self.window.pins, {"supply": "Power"})
        saved = layout_payload(self.path.read_text(encoding="utf-8"))
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
        self.assertIsNotNone(layout_payload(self.path.read_text(encoding="utf-8")))
        self.assertIsNone(layout_payload(other.read_text(encoding="utf-8")))
        self.assertFalse(placement_path(self.path).exists())
        self.assertFalse(placement_path(other).exists())
        self.assertEqual(self.window.pins, {})

    def test_legacy_layout_save_embeds_all_levels_styles_labels_and_probes(self):
        source = 'chip block(a,b) { R1(1k) between(a,b); }\nV1(5) between(a,0);\nmain { block(a,0); }\n'
        self.path.write_text(source, encoding='utf-8')
        self.window.open_path(self.path)
        view = self.window.schematic
        top = replace(view.layout, label_offsets={('component', 'V1'): (1.5, 2)})
        view.toggle_package('@method:0')
        child = view.level_layouts['@method:0', 'auto']
        child = replace(child, label_offsets={('component', child.symbols[0][0].name): (2.5, 1.25)})
        records = {(None, 'auto'): pack_layout(top), (None, 'routed'): pack_layout(top),
                   ('@method:0', 'auto'): pack_layout(child)}
        legacy = write_placement('auto', records, pin_overrides={'a': 'Power', '0': None})
        expected = json.loads(legacy)
        expected_records = {(level, mode): record for level, mode, record in expected['layouts']}
        placement_path(self.path).write_bytes(legacy)
        self.window.open_path(self.path)
        self.assertEqual(read_placement(self.path)[1], expected_records)
        self.assertTrue(self.window.save())
        saved = self.path.read_text(encoding='utf-8')
        self.assertEqual(strip_placement(saved), source)
        self.assertEqual(read_placement(self.path), ('auto', expected_records))
        self.assertEqual(layout_payload(saved)['pin_overrides'], expected['pin_overrides'])
        self.assertEqual(placement_path(self.path).read_bytes(), legacy)
        self.window.open_path(self.path)
        self.assertEqual(self.window.pins, {'a': 'Power'})
        self.assertEqual(view.layout.label_offsets[('component', 'V1')], (1.5, 2))
        view.toggle_package('@method:0')
        restored = view.manual_layouts['@method:0', 'auto']
        self.assertEqual(restored.label_offsets, child.label_offsets)

    def test_ide_layout_save_waits_for_ack_and_failure_keeps_dirty_state(self):
        self.window.open_path(self.path)
        self.window.write_pin('out', 'Output')
        tools = self.window.source_tools
        requests = []
        def reject(request):
            requests.append(request)
            QTimer.singleShot(0, lambda: tools.result({'id': request['id'], 'ok': False, 'error': 'Unsaved IDE changes'}))
        tools.sender = reject
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning'), \
             patch('circuitlab.viewer.window.QMessageBox.warning'):
            self.assertFalse(self.window.save())
        self.assertTrue(self.window.placement_dirty)
        self.assertEqual(self.path.read_text(), DIVIDER)
        self.assertIsNone(tools.pending)

        def accept(request):
            requests.append(request)
            def acknowledge():
                self.assertTrue(self.window.placement_dirty)
                self.assertEqual(request['before'].replace('\r\n', '\n'), DIVIDER)
                self.path.write_text(request['after'], encoding='utf-8', newline='')
                tools.result({'id': request['id'], 'ok': True})
            QTimer.singleShot(0, acknowledge)
        tools.sender = accept
        self.assertTrue(self.window.save())
        self.assertFalse(self.window.placement_dirty)
        self.assertIsNone(tools.pending)
        self.assertEqual(layout_payload(self.path.read_text())['pin_overrides'], {'out': 'Output'})
        self.assertEqual(len(requests), 2)
        self.assertFalse(placement_path(self.path).exists())

    def test_value_undo_after_layout_save_keeps_embedded_metadata(self):
        self.window.open_path(self.path)
        tools = self.window.source_tools
        plan = edit_value(self.window.schematic.circuit, 'R1', '2k', self.path, DIVIDER,
                          lambda path: path.read_text(encoding='utf-8'))
        tools.apply(plan)
        self.window.write_pin('out', 'Output')
        self.assertTrue(self.window.save())
        saved = layout_payload(self.path.read_text())
        self.assertEqual(len(tools.history), 1)
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning') as warning:
            self.window.schematic.undo()
        warning.assert_not_called()
        after = self.path.read_text()
        self.assertEqual(strip_placement(after), DIVIDER)
        self.assertEqual(layout_payload(after), saved)
        self.assertFalse(tools.history)

    def test_renderable_model_error_shows_diagnostics_and_scope(self):
        issues = self.window.issues
        self.assertEqual(issues.item(0).text(), "No compiler diagnostics.")
        clean_height = issues.height()
        margins = issues.contentsMargins()
        self.assertEqual(clean_height, issues.sizeHintForRow(0) + margins.top() + margins.bottom())
        self.path.write_text('include("@chips/converters/ADS1115.ckt");\nmain { ADS1115(a,b,0,c,d,e,f,v,sda,scl); }')
        self.window.open_path(self.path)
        self.assertIsNotNone(self.window.schematic.circuit)
        self.assertIsNone(self.window.solution)
        self.assertIn("simulation model", self.window.status.text())
        self.assertIn("simulation model", issues.item(0).text())
        self.assertGreater(issues.height(), clean_height)

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

    def open_logic_playground(self):
        source = (ROOT / 'tests/fixtures/digital/logic_playground.ckt').read_text(encoding='utf-8')
        self.path.write_text(source, encoding='utf-8')
        self.assertTrue(self.window.open_path(self.path))
        self.assertFalse(self.window.session.issues)
        self.assertIsNotNone(self.window.digital.simulation)
        self.assertTrue(self.window.views.isTabVisible(self.window.digital_tab))
        return source

    def test_digital_inputs_color_wires_and_probe_without_an_editor(self):
        from circuitlab.viewer.schematic import ACCENT, LOGIC_HIGH, LOGIC_LOW

        self.open_logic_playground()
        digital, schematic = self.window.digital, self.window.schematic
        self.assertFalse(self.window.findChildren(QPlainTextEdit))
        self.assertNotIn('clk', schematic.logic_values)
        self.window.probe('node', 'clk')
        self.assertIn('simulated clock (1000 Hz)', self.window.status.text())
        self.assertEqual(schematic.logic_values['and_led'], 0)
        self.assertEqual(schematic.paths['and_led'].pen().color(), LOGIC_LOW)
        digital.input_buttons['a'].click()
        self.assertEqual(digital.simulation.values['xor_led'], 1)
        digital.input_buttons['b'].click()
        self.assertEqual(digital.simulation.values['and_led'], 1)
        self.assertEqual(schematic.paths['and_led'].pen().color(), LOGIC_HIGH)
        schematic.probe_wire('and_led')
        self.assertEqual(digital.probe.currentText(), 'and_led')
        self.assertEqual(schematic.paths['and_led'].pen().color(), ACCENT)
        digital.set_input('a', 0)
        self.assertEqual(schematic.logic_values['and_led'], 0)
        self.assertEqual(schematic.paths['and_led'].pen().color(), ACCENT)
        schematic.redraw(self.window.pins)
        self.assertEqual(schematic.highlight, 'and_led')
        self.assertEqual(schematic.paths['and_led'].pen().color(), ACCENT)
        self.assertEqual(schematic.paths['xor_led'].pen().color(), LOGIC_HIGH)
        schematic.probe_wire('and_led')
        self.assertEqual(schematic.paths['and_led'].pen().color(), LOGIC_LOW)

    def test_digital_run_step_and_probe_preserve_source_and_placement(self):
        self.open_logic_playground()
        source = self.window.session.source
        digital, schematic = self.window.digital, self.window.schematic
        original_layout = pack_layout(schematic.layout)
        original_records = schematic.placements()
        original_disk = self.path.read_bytes()
        digital.set_input('a', 1)
        digital.step()
        self.assertEqual(digital.simulation.values['counter'], 1)
        self.assertEqual(digital.simulation.cycles, 1)
        digital.probe.setCurrentText('counter')
        self.assertIn('counter', digital.waveform.traces)
        self.assertEqual(digital.waveform.history[-1][1]['counter'], 1)
        with patch('circuitlab.viewer.digital.perf_counter', return_value=perf_counter() + 60) as clock:
            digital.toggle()
            self.assertTrue(digital.timer.isActive())
            clock.return_value += .01
            digital.advance()
            digital.toggle()
        self.assertFalse(digital.timer.isActive())
        self.assertEqual(digital.simulation.cycles, 11)  # One step plus 10 ms at 1 kHz.
        self.assertEqual(self.path.read_bytes(), original_disk)
        self.assertEqual(self.window.session.source, source)
        self.assertEqual(pack_layout(schematic.layout), original_layout)
        self.assertEqual(schematic.placements(), original_records)
        self.assertFalse(self.window.placement_dirty)
        self.assertFalse(self.window.source_tools.value_changes)

    def test_digital_source_refresh_resets_state_and_keeps_selected_tab(self):
        source = self.open_logic_playground()
        digital = self.window.digital
        self.window.views.setCurrentWidget(digital)
        digital.set_input('a', 1)
        digital.step()
        original = digital.simulation
        self.assertEqual(original.values['counter'], 1)
        digital.toggle()
        self.path.write_text(source.replace('counter[4];', 'counter[4] = 3;'), encoding='utf-8')
        self.assertTrue(self.window.refresh_from_editor(self.path))
        self.assertIsNot(digital.simulation, original)
        self.assertFalse(digital.timer.isActive())
        self.assertEqual(digital.simulation.cycles, 0)
        self.assertEqual(digital.simulation.values['counter'], 3)
        self.assertEqual(digital.simulation.values['a'], 0)
        self.assertIs(self.window.views.currentWidget(), digital)
        self.assertTrue(all(cycle == 0 for cycle, _ in digital.waveform.history))

    def test_assembled_bus_schematic_keeps_numeric_nets_out_of_scalar_color_overlay(self):
        source = (ROOT / 'tests/fixtures/digital/byte_selector.ckt').read_text()
        self.path.write_text(source, encoding='utf-8')
        self.assertTrue(self.window.open_path(self.path))
        digital = self.window.digital
        digital.set_input('a', 50)
        digital.set_input('choose', 1)
        self.assertEqual(digital.simulation.values['data'], 50)
        self.assertEqual(self.window.schematic.logic_values['choose'], 1)
        self.assertNotIn('data', self.window.schematic.logic_values)
        self.assertNotIn('a', self.window.schematic.logic_values)
        self.assertEqual(self.window.session.drawing.instances[0].name, 'SelectByte')
        self.assertEqual(self.path.read_text(), source)

    def test_digital_to_analog_or_invalid_clears_running_simulation_and_colors(self):
        invalid = 'chip broken(a,y) { a=input; y=output; y=missing; }'
        for source in (DIVIDER, invalid):
            with self.subTest(source=source):
                self.open_logic_playground()
                digital = self.window.digital
                digital.set_input('a', 1)
                digital.toggle()
                self.assertTrue(digital.timer.isActive())
                self.path.write_text(source, encoding='utf-8')
                self.assertTrue(self.window.open_path(self.path))
                self.assertFalse(digital.timer.isActive())
                self.assertIsNone(digital.simulation)
                self.assertEqual(digital.input_buttons, {})
                self.assertEqual(digital.output_labels, {})
                self.assertEqual(list(digital.waveform.history), [])
                self.assertEqual(self.window.schematic.logic_values, {})
                if source == DIVIDER:
                    self.assertFalse(self.window.views.isTabVisible(self.window.digital_tab))
                    self.assertIsNotNone(self.window.solution)
                else:
                    self.assertTrue(self.window.session.issues)

    def test_closing_viewer_stops_digital_clock(self):
        self.open_logic_playground()
        digital = self.window.digital
        digital.toggle()
        self.assertTrue(digital.timer.isActive())
        self.assertTrue(self.window.close())
        self.assertFalse(digital.timer.isActive())

    def test_logic_tab_hides_diagnostics_and_other_tabs_restore_them(self):
        self.open_logic_playground()
        self.assertTrue(self.window.issues.isVisible())
        self.window.views.setCurrentWidget(self.window.digital)
        self.assertTrue(self.window.issues.isHidden())
        self.window.views.setCurrentWidget(self.window.results)
        self.assertTrue(self.window.issues.isVisible())
        self.window.views.setCurrentWidget(self.window.digital)
        self.path.write_text('chip broken(a,y) { a=input; y=output; y=missing; }', encoding='utf-8')
        self.assertTrue(self.window.open_path(self.path))
        self.assertIs(self.window.views.currentWidget(), self.window.digital)
        self.assertTrue(self.window.issues.isHidden())
        self.assertIn('missing', self.window.digital.status.text())
        self.window.views.setCurrentIndex(0)
        self.assertTrue(self.window.issues.isVisible())
        self.assertIn('missing', self.window.issues.item(0).text())
        self.window.views.setCurrentWidget(self.window.digital)
        self.path.write_text(DIVIDER, encoding='utf-8')
        self.assertTrue(self.window.open_path(self.path))
        self.assertFalse(self.window.views.isTabVisible(self.window.digital_tab))
        self.assertTrue(self.window.issues.isVisible())

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

    def test_export_refresh_replaces_running_scope_when_linked_source_changes(self):
        definition = self.path.with_name("block.ckt")
        source = "chip block(a,y,gnd) { R1(1k) between(a,y); R2(1k) between(y,gnd); }\n"
        definition.write_text(source)
        self.path.write_text('include("block.ckt");\nV1(5) between(a,0);\nmain { block(a,out,0); }\n')
        self.window.open_path(self.path)
        self.window.scope.toggle()
        self.assertIsNotNone(self.window.scope.simulation)
        self.assertTrue(self.window.scope.timer.isActive())
        definition.write_text(source.replace("R1(1k)", "R1(2k)"))
        with patch("circuitlab.viewer.exports.QFileDialog.getSaveFileName", return_value=("", "")):
            self.assertFalse(self.window.export_ltspice())
        scope = self.window.scope
        self.assertIs(scope.circuit, self.window.schematic.circuit)
        self.assertEqual(next(part.value for part in scope.circuit.components if part.name == "R1_block_1"), 2000)
        self.assertIsNone(scope.simulation)
        self.assertFalse(scope.timer.isActive())
