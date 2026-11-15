"""IDE reload requests update a live Qt window without revealing the code panel."""

import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.editor_bridge import EditorBridge
from circuitlab.viewer.window import ViewerWindow


class EditorBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "circuit with spaces.ckt"
        self.window = ViewerWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def write_divider(self, voltage):
        self.path.write_text(
            f"V1({voltage}) between(supply, 0);\n"
            "R1(1k) between(supply, out);\nR2(1k) between(out, 0);\n",
            encoding="utf-8",
        )

    def output_voltage(self):
        self.window.results.search.setText("node out")
        return self.window.results.table.item(0, 2).text()

    def test_queued_reload_updates_results_and_reuses_viewer(self):
        self.write_divider(4)
        self.window.open_from_editor(str(self.path))
        self.assertEqual(self.output_voltage(), "2 V")
        self.write_divider(8)
        bridge = EditorBridge(self.window)
        bridge.requested.connect(self.window.open_from_editor, Qt.ConnectionType.QueuedConnection)
        stream = io.StringIO("bad json\n[]\n" + json.dumps({"file": str(self.path)}) + "\n")
        bridge.start(stream)
        for _ in range(100):
            QTest.qWait(10)
            if self.output_voltage() == "4 V":
                break
        self.assertEqual(self.output_voltage(), "4 V")
        self.assertEqual(self.window.path, self.path)
        self.assertIsNotNone(self.window.schematic.circuit)
        self.assertFalse(hasattr(self.window, "editor"))
        self.assertFalse(hasattr(self.window, "editor_tabs"))
        self.assertTrue(self.window.views.isVisible())
        self.assertTrue(self.window.issues.isVisible())

    def test_cancel_protects_unsaved_layout_before_reload(self):
        self.write_divider(4)
        self.window.open_from_editor(str(self.path))
        self.window.placement_dirty = True
        self.write_divider(8)
        with patch.object(self.window, "confirm_edits", return_value=False) as confirm:
            self.assertFalse(self.window.open_from_editor(str(self.path)))
        confirm.assert_called_once()
        self.assertEqual(self.output_voltage(), "2 V")
        self.assertTrue(self.window.placement_dirty)

    def test_auto_refresh_does_not_raise_window_or_prompt_over_unsaved_changes(self):
        self.write_divider(4)
        self.window.open_from_editor(str(self.path))
        self.write_divider(8)
        with patch.object(self.window, "activateWindow") as activate:
            self.assertTrue(self.window.refresh_from_editor(str(self.path)))
        activate.assert_not_called()
        self.assertEqual(self.output_voltage(), "4 V")
        self.window.placement_dirty = True
        self.write_divider(10)
        with patch.object(self.window, "confirm_edits") as confirm:
            self.assertFalse(self.window.refresh_from_editor(str(self.path)))
        confirm.assert_not_called()
        self.assertEqual(self.output_voltage(), "4 V")

    def test_pipe_distinguishes_save_from_manual_run(self):
        bridge = EditorBridge()
        manual, automatic = [], []
        bridge.requested.connect(manual.append)
        bridge.refreshed.connect(automatic.append)
        bridge.read_requests(io.StringIO(
            json.dumps({"file": str(self.path), "activate": False}) + "\n"
            + json.dumps({"file": str(self.path)}) + "\n"
        ))
        self.assertEqual(manual, [str(self.path)])
        self.assertEqual(automatic, manual)

    def test_switching_files_reuses_viewer_and_reads_linked_files_from_disk(self):
        self.write_divider(4)
        self.window.open_from_editor(str(self.path))
        other = self.path.with_name("other.ckt")
        other.write_text(self.path.read_text(), encoding="utf-8")
        self.window.open_from_editor(str(other))
        self.write_divider(8)
        self.assertIn("V1(8)", self.window.read_linked_source(self.path))
        self.window.open_from_editor(str(self.path))
        self.assertEqual(self.output_voltage(), "4 V")
        self.assertEqual(self.window.path, self.path)
