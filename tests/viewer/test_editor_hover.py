"""Delayed syntax formatting must not dismiss source hover previews."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.viewer.editor import CircuitEditor


class EditorHoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.editor = CircuitEditor()
        self.editor.setPlainText("R1(1k) between(input, middle);\nR2(1k) between(middle, 0);")

    def tearDown(self):
        self.editor.close()
        self.editor.deleteLater()
        self.app.processEvents()

    def test_delayed_highlighting_keeps_wire_and_device_hover(self):
        wire, device = "middle", ("builtin", "NOT", 1)
        self.editor.set_hovered_wire(wire)
        self.editor.set_hovered_device(device)
        changes = []
        source = self.editor.toPlainText()
        self.editor.textChanged.connect(lambda: changes.append(self.editor.toPlainText()))
        self.editor.highlighter.rehighlight()
        QTest.qWait(120)
        self.assertTrue(changes)
        self.assertTrue(all(text == source for text in changes))
        self.assertEqual(self.editor.hovered_wire, wire)
        self.assertEqual(self.editor.hovered_device, device)

    def test_actual_text_edit_clears_hover_and_completion(self):
        self.editor.set_hovered_wire("middle")
        self.editor.set_hovered_device(("builtin", "NOT", 1))
        with patch.object(self.editor.completer.popup(), "hide") as hide:
            self.editor.insertPlainText("// edited\n")
        self.assertIsNone(self.editor.hovered_wire)
        self.assertIsNone(self.editor.hovered_device)
        hide.assert_called()


if __name__ == "__main__":
    unittest.main()
