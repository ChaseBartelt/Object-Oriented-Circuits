"""Schematic value edits preserve source connections and support checked undo."""
# Check exact source changes as well as numeric results: comments, names, and terminal order
# must survive edits. Undo/Discard and IDE acknowledgements must not overwrite external changes.
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
IMPEDANCE_SOURCE = ('f=1k\nV1(1 Vrms) between(v,0);\n'
                    'Z1(2-j2) between(v,out); # keep impedance comment\n'
                    'R2(1k) between(out,0);\n')
UNSPECIFIED_IMPEDANCE_SOURCE = IMPEDANCE_SOURCE.replace('Z1(2-j2)', 'Z1()')
BARE_IMPEDANCE_SOURCE = IMPEDANCE_SOURCE.replace('Z1(2-j2)', 'Z1')
POTENTIOMETER_SOURCE = ('V1(5) between(v,0);\n'
                        'P1(10k,25%) between(v,out,0); # keep potentiometer comment\n'
                        'Rload(1k) between(out,0);\n')


class SourceEditsTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
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

    def assert_missing_component(self, window, name='Z1'):
        self.assertIsNotNone(window.schematic.circuit)
        self.assertIsNotNone(window.schematic.layout)
        self.assertIsNone(window.solution)
        self.assertIsNone(next(p.value for p in window.schematic.circuit.components if p.name == name))
        self.assertTrue(window.session.issues)
        self.assertIn(name, window.session.issues[0].message)


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

    def test_complex_impedance_edits_preserve_comments_and_connections(self):
        grouped = ('f=1k\nV1(1 Vrms) between(v,0);\n'
                   'series load(input, output) {\n'
                   '    Z1 = 2-j2; # keep impedance comment\n'
                   '}\nmain {\n    load(v,out);\n    R2(1k) between(out,0);\n}\n')
        sources = (
            IMPEDANCE_SOURCE,
            IMPEDANCE_SOURCE.replace('Z1(2-j2) between(v,out);', 'Z1 v out 2-j2'),
            grouped,
        )
        for source in sources:
            with self.subTest(source=source):
                circuit = compile_circuit(source, path=self.path, render_only=True)
                impedance = next(part for part in circuit.components if part.kind == 'Z')
                plan = edit_value(circuit, impedance.name, '4+j3', self.path, source, self.reader)
                changed = compile_circuit(plan.after, path=self.path, render_only=True)
                self.assertEqual(next(p.value for p in changed.components if p.name == impedance.name), 4 + 3j)
                self.assertEqual([(p.name, p.kind, p.terminal_nets) for p in changed.components],
                                 [(p.name, p.kind, p.terminal_nets) for p in circuit.components])
                self.assertIn('# keep impedance comment', plan.after)
                self.assertIn('R2(1k) between(out,0);', plan.after)
                self.assertEqual(plan.before, source)
                self.assertEqual(plan.path, self.path.resolve())
        self.assertEqual(self.reader(self.path), SOURCE)

    def test_invalid_zero_and_nonfinite_impedance_edits_leave_source_untouched(self):
        self.path.write_text(IMPEDANCE_SOURCE, encoding='utf-8')
        circuit = compile_circuit(IMPEDANCE_SOURCE, path=self.path, render_only=True)
        original = self.path.read_bytes()
        for value in ('oops', '1 bananas', '0', '0+j0', 'nan', 'inf', '1e999',
                      '2-j2; R99(1) between(a,b);', '2-j2\nR99(1) between(a,b);'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                edit_value(circuit, 'Z1', value, self.path, IMPEDANCE_SOURCE, self.reader)
        self.assertEqual(self.path.read_bytes(), original)

    def test_potentiometer_edits_preserve_three_terminals_and_validate_both_values(self):
        self.path.write_text(POTENTIOMETER_SOURCE, encoding='utf-8')
        circuit = compile_circuit(POTENTIOMETER_SOURCE, path=self.path, render_only=True)
        for value, expected in (('200,75%', (200, .75)), ('4.7k', (4700, .5)),
                                ('1k,0', (1000, 0)), (',25%', (None, .25))):
            with self.subTest(value=value):
                plan = edit_value(circuit, 'P1', value, self.path, POTENTIOMETER_SOURCE, self.reader)
                self.assertEqual(plan.after, POTENTIOMETER_SOURCE.replace('10k,25%', value))
                changed = compile_circuit(plan.after, path=self.path, render_only=True)
                part = next(part for part in changed.components if part.name == 'P1')
                self.assertEqual((part.value, part.position), expected)
                self.assertEqual(part.terminal_nets, ('v', 'out', '0'))
                if part.value is None:
                    from circuitlab.math.analysis import analyze
                    with self.assertRaises(ValueError) as caught:
                        analyze(changed)
                    self.assertEqual(caught.exception.code, 'MissingComponentValue')
        original = self.path.read_bytes()
        for value in ('0,25%', '-1,25%', '1k,101%', '1k,-1%', '1k,', '1k,1,2',
                      '1k,nan', '1e999,25%', '1k,25%; R99(1) between(a,b);', '1k,25%\n# changed'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                edit_value(circuit, 'P1', value, self.path, POTENTIOMETER_SOURCE, self.reader)
        self.assertEqual(self.path.read_bytes(), original)

    def test_included_potentiometer_edit_updates_shared_definition(self):
        library = self.path.with_name('pot.ckt')
        definition = 'chip knob(high,wiper,low) {\n  P1(10k,25%) between(high,wiper,low);\n}\n'
        library.write_text(definition, encoding='utf-8')
        source = 'include("pot.ckt");\nmain { knob(v,a,0); knob(v,b,0); }\n'
        circuit = compile_circuit(source, path=self.path, render_only=True)
        part = next(part for part in circuit.components if part.kind == 'P')
        plan = edit_value(circuit, part.name, '20k,75%', self.path, source, self.reader)
        self.assertEqual(plan.path, library.resolve())
        self.assertEqual(plan.after, definition.replace('10k,25%', '20k,75%'))
        changed = compile_circuit(source, path=self.path, render_only=True,
                                  read_source=lambda path: plan.after if path == library else self.reader(path))
        self.assertEqual([(part.value, part.position) for part in changed.components], [(20000, .75)] * 2)
        self.assertEqual(self.reader(library), definition)

    def test_potentiometer_right_click_edit_undo_clear_and_discard_are_atomic(self):
        self.path.write_text(POTENTIOMETER_SOURCE, encoding='utf-8')
        window = self.window()
        window.resize(1400, 800)
        window.show()
        self.app.processEvents()
        window.schematic.fit()
        layout = pack_layout(window.schematic.layout)
        _, a, b, _ = next(row for row in window.schematic.layout.symbols if row[0].name == 'P1')
        pos = window.schematic.mapFromScene(point(tuple((x + y) / 2 for x, y in zip(a, b))))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                  window.schematic.viewport().mapToGlobal(pos))
        with patch('circuitlab.viewer.schematic.QMenu') as menu, \
             patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('20k,75%', True)) as dialog:
            window.schematic.contextMenuEvent(event)
            actions = dict(call.args for call in menu.return_value.addAction.call_args_list)
            self.assertIn('Edit value…', actions)
            actions['Edit value…']()
        self.assertEqual(dialog.call_args.kwargs['text'], '10k,25%')
        self.assertIn('resistance, position', dialog.call_args.args[2])
        changed_source = POTENTIOMETER_SOURCE.replace('10k,25%', '20k,75%')
        self.assertEqual(self.reader(self.path), changed_source)
        self.assertEqual(len(window.source_tools.history), 1)
        part = next(part for part in window.schematic.circuit.components if part.name == 'P1')
        self.assertEqual((part.value, part.position), (20000, .75))
        window.schematic.undo()
        self.assertEqual(self.reader(self.path), POTENTIOMETER_SOURCE)
        with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=(',25%', True)):
            window.source_tools.change_value('P1')
        self.assertEqual(self.reader(self.path), POTENTIOMETER_SOURCE.replace('10k,25%', ',25%'))
        self.assert_missing_component(window, 'P1')
        with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('', True)):
            window.source_tools.change_value('P1')
        self.assertEqual(self.reader(self.path), POTENTIOMETER_SOURCE.replace('P1(10k,25%)', 'P1'))
        self.assert_missing_component(window, 'P1')
        with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('200', True)):
            window.source_tools.change_value('P1')
        part = next(part for part in window.schematic.circuit.components if part.name == 'P1')
        self.assertEqual((part.value, part.position), (200, .5))
        self.assertTrue(window.source_tools.discard_values())
        self.assertEqual(self.reader(self.path), POTENTIOMETER_SOURCE)
        self.assertEqual(pack_layout(window.schematic.layout), layout)
        self.assertFalse(window.source_tools.history)
        self.assertFalse(window.source_tools.value_changes)

    def test_impedance_edit_keeps_explicitly_selected_ac_mode(self):
        source = IMPEDANCE_SOURCE.replace('f=1k\n', '').replace('1 Vrms', '1')
        circuit = compile_circuit(source, 'ac', path=self.path, render_only=True)
        plan = edit_value(circuit, 'Z1', '3-j4', self.path, source, self.reader)
        changed = compile_circuit(plan.after, 'ac', path=self.path, render_only=True)
        self.assertEqual(changed.mode, 'ac')
        self.assertEqual(next(p.value for p in changed.components if p.name == 'Z1'), 3 - 4j)

    def test_included_impedance_edit_targets_definition_and_all_instances(self):
        library = self.path.with_name('impedance.ckt')
        definition = 'chip load(a,b) {\n  Z1(2-j2) between(a,b); # shared impedance\n}\n'
        library.write_text(definition, encoding='utf-8')
        source = ('include("impedance.ckt");\nf=1k\nV1(1 Vrms) between(v,0);\n'
                  'main { load(v,out); load(out,0); }\n')
        self.path.write_text(source, encoding='utf-8')
        circuit = compile_circuit(source, path=self.path, render_only=True)
        impedance = next(part for part in circuit.components if part.kind == 'Z')
        plan = edit_value(circuit, impedance.name, '4-j1', self.path, source, self.reader)
        self.assertEqual(plan.path, library.resolve())
        self.assertEqual(plan.after, definition.replace('2-j2', '4-j1'))
        changed = compile_circuit(source, path=self.path, render_only=True,
                                  read_source=lambda path: plan.after if path == library else self.reader(path))
        self.assertEqual([p.value for p in changed.components if p.kind == 'Z'], [4 - 1j, 4 - 1j])
        self.assertEqual(self.reader(library), definition)
        self.assertEqual(self.reader(self.path), source)


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

    def test_right_click_complex_impedance_edit_and_ctrl_z_restore_code_and_value(self):
        self.path.write_text(IMPEDANCE_SOURCE, encoding='utf-8')
        window = self.window()
        window.resize(1400, 800)
        window.show()
        window.activateWindow()
        self.app.processEvents()
        original_layout = pack_layout(window.schematic.layout)
        _, a, b, _ = next(row for row in window.schematic.layout.symbols if row[0].name == 'Z1')
        location = tuple((x + y) / 2 for x, y in zip(a, b))
        pos = window.schematic.mapFromScene(point(location))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                  window.schematic.viewport().mapToGlobal(pos))
        with patch('circuitlab.viewer.schematic.QMenu') as menu, \
             patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('4+j3', True)):
            window.schematic.contextMenuEvent(event)
            actions = dict(call.args for call in menu.return_value.addAction.call_args_list)
            self.assertIn('Edit value…', actions)
            actions['Edit value…']()
        self.assertEqual(self.reader(self.path), IMPEDANCE_SOURCE.replace('2-j2', '4+j3'))
        self.assertEqual(next(p.value for p in window.schematic.circuit.components if p.name == 'Z1'), 4 + 3j)
        self.assertEqual(pack_layout(window.schematic.layout), original_layout)
        window.schematic.setFocus()
        self.app.processEvents()
        QTest.keyClick(window.schematic, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.reader(self.path), IMPEDANCE_SOURCE)
        self.assertEqual(next(p.value for p in window.schematic.circuit.components if p.name == 'Z1'), 2 - 2j)
        self.assertEqual(pack_layout(window.schematic.layout), original_layout)

    def test_unspecified_impedance_renders_with_a_named_missing_value_diagnostic(self):
        self.path.write_text(UNSPECIFIED_IMPEDANCE_SOURCE, encoding='utf-8')
        window = self.window()
        self.assert_missing_component(window)
        labels = [item.text() for item in window.schematic.scene().items()
                  if item.data(4) == ('component', 'Z1') and hasattr(item, 'text')]
        self.assertEqual(labels, ['Z1'])
        self.assertEqual(self.reader(self.path), UNSPECIFIED_IMPEDANCE_SOURCE)

    def test_filling_unspecified_impedance_solves_and_ctrl_z_restores_placeholder(self):
        self.path.write_text(UNSPECIFIED_IMPEDANCE_SOURCE, encoding='utf-8')
        window = self.window()
        self.assert_missing_component(window)
        before = pack_layout(window.schematic.layout)
        window.show()
        window.activateWindow()
        self.app.processEvents()
        with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('4+j3', True)) as dialog:
            window.source_tools.change_value('Z1')
        self.assertEqual(dialog.call_args.kwargs['text'], '')
        self.assertEqual(self.reader(self.path), UNSPECIFIED_IMPEDANCE_SOURCE.replace('Z1()', 'Z1(4+j3)'))
        self.assertIsNotNone(window.solution)
        self.assertFalse(window.session.issues)
        self.assertEqual(next(p.value for p in window.schematic.circuit.components if p.name == 'Z1'), 4 + 3j)
        self.assertEqual(pack_layout(window.schematic.layout), before)
        window.schematic.setFocus()
        self.app.processEvents()
        QTest.keyClick(window.schematic, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.reader(self.path), UNSPECIFIED_IMPEDANCE_SOURCE)
        self.assert_missing_component(window)
        self.assertEqual(pack_layout(window.schematic.layout), before)

    def test_clearing_known_impedance_keeps_drawing_and_removes_value_parentheses(self):
        for blank in ('', '   '):
            with self.subTest(blank=blank):
                self.path.write_text(IMPEDANCE_SOURCE, encoding='utf-8')
                window = self.window()
                self.assertIsNotNone(window.solution)
                before = pack_layout(window.schematic.layout)
                with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=(blank, True)):
                    window.source_tools.change_value('Z1')
                self.assertEqual(self.reader(self.path), BARE_IMPEDANCE_SOURCE)
                self.assert_missing_component(window)
                self.assertEqual(pack_layout(window.schematic.layout), before)
                window.source_tools.checkpoint()

    def test_bare_passive_value_edits_fill_clear_and_undo_without_changing_connections(self):
        cases = (('R1', '2k', 2000), ('C1', '220n', 220e-9),
                 ('L1', '10m', .01), ('Z1', '4+j3', 4 + 3j))
        for name, value, expected in cases:
            with self.subTest(part=name):
                source = ('f=1k\nV1(1 Vrms) between(v,0);\n'
                          f'{name} between(v,out); # keep placeholder comment\n'
                          'Rload(1k) between(out,0);\n')
                self.path.write_text(source, encoding='utf-8')
                window = self.window()
                self.assert_missing_component(window, name)
                original_layout = pack_layout(window.schematic.layout)
                original_nets = [(p.name, p.terminal_nets) for p in window.schematic.circuit.components]
                with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=(value, True)) as dialog:
                    window.source_tools.change_value(name)
                self.assertEqual(dialog.call_args.kwargs['text'], '')
                filled = source.replace(f'{name} between', f'{name}({value}) between')
                self.assertEqual(self.reader(self.path), filled)
                self.assertIsNotNone(window.solution)
                self.assertFalse(window.session.issues)
                self.assertAlmostEqual(next(p.value for p in window.schematic.circuit.components if p.name == name), expected)
                with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=('   ', True)):
                    window.source_tools.change_value(name)
                self.assertEqual(self.reader(self.path), source)
                self.assert_missing_component(window, name)
                window.schematic.undo()
                self.assertEqual(self.reader(self.path), filled)
                self.assertAlmostEqual(next(p.value for p in window.schematic.circuit.components if p.name == name), expected)
                window.schematic.undo()
                self.assertEqual(self.reader(self.path), source)
                self.assert_missing_component(window, name)
                self.assertEqual([(p.name, p.terminal_nets) for p in window.schematic.circuit.components], original_nets)
                self.assertEqual(pack_layout(window.schematic.layout), original_layout)
                self.assertFalse(window.source_tools.history)
                window.source_tools.checkpoint()

    def test_cancelled_impedance_dialog_does_not_fill_or_clear_a_value(self):
        for source, replacement in ((UNSPECIFIED_IMPEDANCE_SOURCE, '4+j3'), (IMPEDANCE_SOURCE, '')):
            with self.subTest(source=source):
                self.path.write_text(source, encoding='utf-8')
                window = self.window()
                before = self.path.read_bytes()
                revision = window.session.revision
                with patch('circuitlab.viewer.source_editing.QInputDialog.getText', return_value=(replacement, False)), \
                     patch('circuitlab.viewer.source_editing.edit_value') as edit:
                    window.source_tools.change_value('Z1')
                edit.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(window.session.revision, revision)
                self.assertFalse(window.source_tools.history)
                self.assertFalse(window.source_tools.value_changes)

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
