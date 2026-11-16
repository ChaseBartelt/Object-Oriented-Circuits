"""Schematic value edits preserve source connections and support checked undo."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtCore import Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.persistence import pack_layout
from circuitlab.tooling.source_edits import edit_value
from circuitlab.viewer.window import ViewerWindow
from circuitlab.viewer.schematic import point


SOURCE = 'V1(5) between(v,0);\nR1(1k) between(v,out); # keep comment\nR2(1k) between(out,0);\n'


class SourceEditsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'circuit.ckt'
        self.path.write_text(SOURCE, encoding='utf-8')
        self.reader = lambda p: p.read_text(encoding='utf-8-sig')
        self.circuit = compile_circuit(SOURCE, path=self.path, render_only=True)

    def window(self):
        view = ViewerWindow()
        view.open_path(self.path)
        def close():
            view.source_tools.checkpoint()
            view.placement_dirty = False
            view.close()
            view.deleteLater()
        self.addCleanup(close)
        return view


    def test_component_toolbar_has_no_value_or_topology_tools(self):
        window = self.window()
        labels = {button.text() for button in window.findChildren(QPushButton)}
        self.assertTrue({'Edit value', 'Add resistor', 'Add capacitor', 'Add inductor'}.isdisjoint(labels))
        self.assertTrue({'Optimize wiring', 'Undo'} <= labels)
        self.assertFalse(hasattr(window.schematic, 'part_tool'))
        self.assertFalse(hasattr(window.schematic, 'insert_requested'))
        self.assertFalse(hasattr(window.schematic, 'delete_requested'))

    def test_value_change_preserves_comments_and_supports_legacy_rows(self):
        for source in (SOURCE, SOURCE.replace('R1(1k) between(v,out);', 'R1 v out 1k')):
            circuit = compile_circuit(source, path=self.path, render_only=True)
            plan = edit_value(circuit, 'R1', '4.7k', self.path, source, self.reader)
            changed = compile_circuit(plan.after, path=self.path, render_only=True)
            self.assertEqual(next(p.value for p in changed.components if p.name == 'R1'), 4700)
            self.assertIn('# keep comment', plan.after)
            self.assertIn('R2(1k) between(out,0);', plan.after)


    def test_rejects_invalid_values_and_generated_builtin_components(self):
        for value in ('oops', '1 bananas', '1k; R99(1) between(a,b);'):
            with self.assertRaises(ValueError):
                edit_value(self.circuit, 'R1', value, self.path, SOURCE, self.reader)
        source = 'main { not(v,0,a,b); }'
        circuit = compile_circuit(source, path=self.path, render_only=True)
        part = next(p for p in circuit.components if p.kind == 'R')
        with self.assertRaises(ValueError):
            edit_value(circuit, part.name, '1k', self.path, source, self.reader)

    def test_included_definition_value_has_correct_source_scope(self):
        library = Path(self.temp.name) / 'part.ckt'
        library.write_text('chip load(a,b) {\n R1(1k) between(a,b);\n}\n')
        source = 'include("part.ckt");\nmain { load(v,0); }'
        circuit = compile_circuit(source, path=self.path, render_only=True)
        plan = edit_value(circuit, circuit.components[0].name, '2k', self.path, source, self.reader)
        self.assertEqual(plan.path, library)
        self.assertIn('R1(2k)', plan.after)

    def test_viewer_changes_source_and_values_without_moving_existing_layout(self):
        window = self.window()
        before = pack_layout(window.schematic.layout)
        plan = edit_value(window.schematic.circuit, 'R1', '2k', self.path, SOURCE, self.reader)
        window.source_tools.apply(plan)
        self.assertIn('R1(2k)', self.reader(self.path))
        self.assertEqual(pack_layout(window.schematic.layout), before)
        self.assertEqual(next(p.value for p in window.schematic.circuit.components if p.name == 'R1'), 2000)
        self.assertIsNotNone(window.solution)

    def test_unchanged_value_dialog_keeps_source_clean_without_recompiling(self):
        window = self.window()
        tools = window.source_tools
        revision = window.session.revision
        before = self.path.read_bytes()
        with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=(' 1k ', True)), \
             patch('circuitlab.viewer.source_editing.edit_value') as edit, \
             patch.object(tools, 'snapshot') as snapshot, \
             patch.object(window, 'compile_source') as compile_source:
            tools.change_value('R1')
        edit.assert_not_called()
        snapshot.assert_not_called()
        compile_source.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(window.session.revision, revision)
        self.assertFalse(window.placement_dirty)
        self.assertFalse(tools.history)
        self.assertFalse(tools.value_changes)


    def test_right_click_value_edit_and_legacy_editor_undo(self):
        window = self.window()
        window.resize(1400, 800)
        window.show()
        self.app.processEvents()
        window.schematic.fit()
        _, a, b, _ = next(row for row in window.schematic.layout.symbols if row[0].name == 'R1')
        location = tuple((x+y)/2 for x,y in zip(a,b))
        pos = window.schematic.mapFromScene(point(location))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                  window.schematic.viewport().mapToGlobal(pos))
        with patch('circuitlab.viewer.schematic.QMenu') as menu, \
             patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('2k', True)):
            window.schematic.contextMenuEvent(event)
            actions = dict(call.args for call in menu.return_value.addAction.call_args_list)
            self.assertIn('Edit value…', actions)
            self.assertNotIn('Delete component', actions)
            actions['Edit value…']()
        self.assertIn('R1(2k)', self.reader(self.path))
        window.schematic.undo()
        self.assertEqual(self.reader(self.path), SOURCE)

        from circuitlab.viewer.legacy import MainWindow
        legacy = MainWindow()
        try:
            legacy.open_path(self.path)
            original = legacy.editor.toPlainText()
            plan = edit_value(legacy.schematic.circuit, 'R1', '3k', self.path, original, self.reader)
            legacy.source_tools.apply(plan)
            self.assertIn('R1(3k)', legacy.editor.toPlainText())
            self.assertEqual(self.reader(self.path), SOURCE)
            legacy.schematic.undo()
            self.assertEqual(legacy.editor.toPlainText(), original)
            legacy.source_tools.apply(plan)
            legacy.editor.undo()
            self.assertEqual(legacy.editor.toPlainText(), original)
        finally:
            legacy.editor.document().setModified(False)
            legacy.placement_dirty = False
            legacy.close()
            legacy.deleteLater()

    def test_stale_disk_is_not_overwritten_and_ide_request_waits_for_ack(self):
        window = self.window()
        plan = edit_value(window.schematic.circuit, 'R1', '2k', self.path, SOURCE, self.reader)
        self.path.write_text(SOURCE + '# changed externally\n')
        with self.assertRaisesRegex(ValueError, 'changed on disk'):
            window.source_tools.apply(plan)
        messages = []
        window.source_tools.sender = messages.append
        window.source_tools.apply(plan)
        self.assertEqual(messages[0]['type'], 'ooc-source-edit')
        self.assertNotIn('R1(2k)', self.reader(self.path))
        self.assertFalse(window.refresh_from_editor(str(self.path)))
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning') as warning:
            window.source_tools.result({'id':window.source_tools.request_id, 'ok':False, 'error':'stale'})
            warning.assert_called_once()
        self.assertIsNone(window.source_tools.pending)

    def test_source_undo_restores_consecutive_value_edits_in_order(self):
        window = self.window()
        tools = window.source_tools
        original_layout = pack_layout(window.schematic.layout)
        previous = []
        for name, value in [('R1', '2k'), ('R2', '3k'), ('R1', '4k')]:
            before = self.reader(self.path)
            previous.append(before)
            tools.apply(edit_value(window.schematic.circuit, name, value, self.path, before, self.reader))
        for expected in reversed(previous):
            window.schematic.undo()
            self.assertEqual(self.reader(self.path), expected)
            self.assertEqual(window.session.source.replace('\r\n', '\n'), expected)
        self.assertEqual(pack_layout(window.schematic.layout), original_layout)
        self.assertFalse(tools.history)

    def test_ctrl_z_restores_grouped_series_value(self):
        source = ('V1(5) between(vin,0);\n'
                  'series lowpass(input, output) {\n    R1 = 10;\n}\n'
                  'parallel shunt(input, ground) {\n    C1 = 100n;\n}\n'
                  'main {\n    lowpass(vin, out);\n    shunt(out, 0);\n}\n')
        self.path.write_text(source)
        window = self.window()
        plan = edit_value(window.schematic.circuit, 'R1_lowpass_1', '20', self.path, source, self.reader)
        window.source_tools.apply(plan)
        self.assertIn('R1 = 20', self.reader(self.path))
        self.assertIn('lowpass(vin, out)', self.reader(self.path))
        window.show()
        window.activateWindow()
        window.schematic.setFocus()
        self.app.processEvents()
        QTest.keyClick(window.schematic, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.reader(self.path), source)
        self.assertEqual(len(window.schematic.circuit.components), 3)

    def test_undo_source_and_layout_moves_share_chronological_order(self):
        from circuitlab.layout.editing import move_symbol
        window = self.window()
        view = window.schematic
        key = next(iter(view.levels))
        original = pack_layout(view.layout)
        def move():
            x, y = view.layout.positions['R1']
            moved = move_symbol(view.levels[key], view.layout, 'R1', (x, y + 2))
            self.assertIsNotNone(moved)
            view.remember_placement(key)
            view.manual_layouts[key] = moved
            view._redraw()
        move()
        first_move = pack_layout(view.layout)
        window.source_tools.apply(edit_value(view.circuit, 'R1', '2k', self.path, SOURCE, self.reader))
        move()
        view.undo()
        self.assertEqual(pack_layout(view.layout), first_move)
        self.assertIn('R1(2k)', self.reader(self.path))
        view.undo()
        self.assertEqual(self.reader(self.path), SOURCE)
        self.assertEqual(pack_layout(view.layout), first_move)
        view.undo()
        self.assertEqual(pack_layout(view.layout), original)

    def test_ide_undo_waits_for_ack_and_rejected_undo_can_be_retried(self):
        window = self.window()
        tools = window.source_tools
        messages = []
        tools.sender = messages.append
        plan = edit_value(window.schematic.circuit, 'R1', '2k', self.path, SOURCE, self.reader)
        tools.apply(plan)
        self.assertFalse(tools.history)
        self.path.write_text(plan.after)
        tools.result({'id':tools.request_id, 'ok': True})
        window.schematic.undo()
        self.assertEqual(messages[-1]['before'].replace('\r\n', '\n'), plan.after)
        self.assertEqual(messages[-1]['after'].replace('\r\n', '\n'), SOURCE)
        self.assertEqual(len(tools.history), 1)
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning'):
            tools.result({'id':tools.request_id, 'ok': False, 'error': 'Unsaved changes'})
        self.assertEqual(len(tools.history), 1)
        self.assertTrue(window.schematic.isEnabled())
        window.schematic.undo()
        self.path.write_text(SOURCE)
        tools.result({'id':tools.request_id, 'ok': True})
        self.assertFalse(tools.history)
        self.assertEqual(window.session.source.replace('\r\n', '\n'), SOURCE)

    def test_external_changes_and_file_switch_do_not_undo_into_wrong_source(self):
        window = self.window()
        tools = window.source_tools
        tools.apply(edit_value(window.schematic.circuit, 'R1', '2k', self.path, SOURCE, self.reader))
        newer = self.reader(self.path) + '# external change\n'
        self.path.write_text(newer)
        with patch('circuitlab.viewer.source_editing.QMessageBox.warning') as warning:
            window.schematic.undo()
            warning.assert_called_once()
        self.assertEqual(self.reader(self.path), newer)
        other = self.path.with_name('other.ckt')
        other.write_text(SOURCE)
        window.open_path(other)
        self.assertFalse(tools.history)
        window.schematic.undo()
        self.assertEqual(self.reader(self.path), newer)
        self.assertEqual(self.reader(other), SOURCE)

    def test_ctrl_z_works_when_toolbar_keeps_focus(self):
        window = self.window()
        window.show()
        window.activateWindow()
        button = next(b for b in window.findChildren(QPushButton) if b.text() == 'Undo')
        button.setFocus()
        self.app.processEvents()
        with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('2k', True)):
            window.source_tools.change_value('R1')
        self.assertIn('R1(2k)', self.reader(self.path))
        QTest.keyClick(button, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.reader(self.path), SOURCE)

    def test_async_edit_can_undo_with_actual_focus_and_ignores_late_ack(self):
        window = self.window()
        window.show()
        window.activateWindow()
        window.schematic.setFocus()
        self.app.processEvents()
        tools = window.source_tools
        sent = []
        tools.sender = sent.append
        plan = edit_value(window.schematic.circuit, 'R1', '2k', self.path, SOURCE, self.reader)
        tools.apply(plan)
        old_id = sent[-1]['id']
        self.path.write_text(plan.after)
        tools.result({'id': old_id, 'ok': True})
        self.app.processEvents()
        # Disabling the canvas moved focus. Do not artificially focus it again.
        focused = self.app.focusWidget()
        self.assertIsNotNone(focused)
        QTest.keyClick(focused, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1]['after'].replace('\r\n', '\n'), SOURCE)
        self.assertNotEqual(sent[-1]['id'], old_id)
        tools.result({'id': old_id, 'ok': True})
        self.assertIsNotNone(tools.pending)
        self.assertEqual(len(tools.history), 1)
        # Toolbar/menu actions must not change the stack under an in-flight undo.
        window.schematic.reset_placement()
        window.schematic.align_to_grid()
        window.schematic.optimize_wiring()
        self.assertEqual(len(tools.history), 1)
        self.path.write_text(SOURCE)
        tools.result({'id': sent[-1]['id'], 'ok': True})
        self.assertIsNone(tools.pending)
        self.assertFalse(tools.history)

    def test_rerender_does_not_erase_move_undo_or_copy_history_into_snapshots(self):
        from dataclasses import replace
        window = self.window()
        view = window.schematic
        original = pack_layout(view.layout)
        key = next(iter(view.levels))
        view.remember_placement(key)
        view.manual_layouts[key] = replace(view.layout, label_offsets={('component', 'R1'): (1, 2)})
        view._redraw()
        window.source_tools.apply(edit_value(view.circuit, 'R1', '2k', self.path, SOURCE, self.reader))
        self.assertIs(window.source_tools.history, view.undo_history)
        self.assertNotIn('history', window.source_tools.history[-1].snapshot)
        window.compile_source()
        view.undo()
        self.assertEqual(self.reader(self.path), SOURCE)
        view.undo()
        self.assertEqual(pack_layout(view.layout), original)

    def test_reset_placement_keeps_source_undo_and_is_itself_undoable(self):
        from dataclasses import replace
        window = self.window()
        view = window.schematic
        key = next(iter(view.levels))
        view.manual_layouts[key] = replace(view.layout, label_offsets={('component', 'R1'): (1, 2)})
        view._redraw()
        before = pack_layout(view.layout)
        window.source_tools.apply(edit_value(view.circuit, 'R1', '2k', self.path, SOURCE, self.reader))
        view.reset_placement()
        self.assertEqual(len(view.undo_history), 2)
        view.undo()
        self.assertEqual(pack_layout(view.layout), before)
        view.undo()
        self.assertEqual(self.reader(self.path), SOURCE)


if __name__ == '__main__':
    unittest.main()
