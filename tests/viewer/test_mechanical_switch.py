"""Manual switch clicks update the shared source, solution and undo history."""
# A click commits a source state change and recomputes results; Ctrl-click only selects.
# Check the acknowledged IDE path as well as standalone editing, including preserved layout and
# Undo.

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.layout.geometry import along, rotate_point
from circuitlab.layout.persistence import pack_layout
from circuitlab.viewer.schematic import point
from circuitlab.viewer.editor import CircuitEditor
from circuitlab.viewer.window import ViewerWindow


SOURCE = "V1(5) between(input,0);\nS1(open) between(input,out);\nR1(1k) between(out,0);\n"


class MechanicalSwitchTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "switch.ckt"
        self.path.write_text(SOURCE, encoding="utf-8")
        self.window = ViewerWindow()
        self.window.show()
        self.window.open_path(self.path)
        self.app.processEvents()

    def tearDown(self):
        self.window.source_tools.checkpoint()
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def switch(self):
        return next(part for part in self.window.schematic.circuit.components if part.kind == "K")

    def voltage(self):
        network = self.window.solution.network
        return network.voltage[network.nodes.index("out")]

    def switch_center(self):
        view = self.window.schematic
        _, a, b, _ = next(record for record in view.layout.symbols if record[0].kind == "K")
        return view.mapFromScene(point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)))

    def click_switch(self):
        QTest.mouseClick(self.window.schematic.viewport(), Qt.MouseButton.LeftButton, pos=self.switch_center())
        self.app.processEvents()

    def test_click_changes_source_and_output_and_undo_preserves_layout(self):
        tools = self.window.source_tools
        layout = pack_layout(self.window.schematic.layout)
        self.assertFalse(self.switch().closed)
        self.assertAlmostEqual(self.voltage(), 0, places=6)
        self.click_switch()
        self.assertTrue(self.switch().closed)
        self.assertIn("S1(closed)", self.path.read_text())
        self.assertAlmostEqual(self.voltage(), 5, places=4)
        self.assertEqual(len(tools.history), 1)
        self.assertEqual(pack_layout(self.window.schematic.layout), layout)
        tools.undo()
        self.assertFalse(self.switch().closed)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertAlmostEqual(self.voltage(), 0, places=6)
        self.assertEqual(pack_layout(self.window.schematic.layout), layout)
        self.click_switch()
        self.click_switch()
        self.assertFalse(self.switch().closed)
        self.assertEqual(self.path.read_text(), SOURCE)

    def test_control_click_selects_without_toggling(self):
        view = self.window.schematic
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, pos=self.switch_center())
        self.assertFalse(self.switch().closed)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertFalse(self.window.source_tools.history)
        self.assertIn("S1", view.selected_symbols)

    def test_clicks_on_the_switch_lead_and_caption_leave_it_open(self):
        view = self.window.schematic
        part, a, b, _ = next(record for record in view.layout.symbols if record[0].kind == "K")
        turns = view.layout.rotations.get(part.name, 0)
        lead = point(rotate_point(along(a, b, 0.1), a, b, turns))
        caption = next(item for item in view.captions if item.data(1) == part.name)
        for position in (lead, caption.sceneBoundingRect().center()):
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(position))
            self.assertFalse(self.switch().closed)
            self.assertEqual(self.path.read_text(), SOURCE)
        self.click_switch()
        self.assertTrue(self.switch().closed)

    def test_ide_toggle_waits_for_ack_and_rejection_leaves_source_unchanged(self):
        tools = self.window.source_tools
        requests = []
        tools.sender = requests.append
        self.click_switch()
        self.assertTrue(tools.pending)
        self.assertFalse(self.switch().closed)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertEqual(len(requests), 1)
        self.assertIn("S1(closed)", requests[0]["after"])
        with patch("circuitlab.viewer.source_editing.QMessageBox.warning"):
            tools.result({"id": requests[0]["id"], "ok": False, "error": "Source changed in the IDE"})
        self.assertFalse(tools.pending)
        self.assertTrue(self.window.schematic.isEnabled())
        self.assertFalse(self.switch().closed)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertFalse(tools.history)
        self.click_switch()
        request = requests[-1]
        self.path.write_text(request["after"], encoding="utf-8")
        tools.result({"id": request["id"], "ok": True})
        self.assertTrue(self.switch().closed)
        self.assertAlmostEqual(self.voltage(), 5, places=4)
        self.assertEqual(len(tools.history), 1)

    def test_legacy_editor_toggle_updates_buffer_and_can_be_undone(self):
        from circuitlab.viewer.legacy import MainWindow

        window = MainWindow()
        try:
            window.open_path(self.path)
            window.compile_source()
            window.source_tools.toggle_switch("S1")
            self.assertIn("S1(closed)", window.editor.toPlainText())
            self.assertEqual(self.path.read_text(), SOURCE)
            self.assertTrue(next(part for part in window.solution.circuit.components if part.kind == "K").closed)
            window.source_tools.undo()
            self.assertEqual(window.editor.toPlainText(), SOURCE)
        finally:
            window.placement_dirty = False
            for editor, state in window.documents.items():
                state.placement_dirty = False
                editor.document().setModified(False)
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_native_completion_inserts_a_switch_placement_with_selected_state(self):
        editor = CircuitEditor()
        try:
            editor.setPlainText("main {\n    S")
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            editor.setTextCursor(cursor)
            editor.insert_completion("S1")
            self.assertEqual(editor.toPlainText(), "main {\n    S1(open) between(a, b);")
            self.assertEqual(editor.textCursor().selectedText(), "open")
        finally:
            editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
