"""The optional legacy editor preserves viewer-only probe captions."""
# Probe captions are viewer metadata: save/reload must preserve overrides without changing
# the circuit targets described by the optional source editor.

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from circuitlab.viewer.legacy import MainWindow
from circuitlab.layout.persistence import placement_path, read_pin_overrides, write_placement, strip_placement, layout_payload


class PinPersistenceTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_legacy_edit_and_save_retains_viewer_overrides(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "pins.ckt"
            source = 'V1(5) between(v,0);\nR1(1k) between(v,0);\n# @pin {"node":"v","name":"Source"}\n'
            path.write_text(source, encoding="utf-8")
            placement_path(path).write_bytes(write_placement("auto", {}, pin_overrides={"v": "Viewer"}))
            window = MainWindow()
            try:
                window.open_path(path)
                self.assertEqual(window.pins, {"v": "Viewer"})
                self.assertTrue(window.save())
                self.assertEqual(read_pin_overrides(path), {"v": "Viewer"})
                saved = path.read_text(encoding="utf-8")
                self.assertEqual(strip_placement(saved), source)
                self.assertEqual(layout_payload(saved)['pin_overrides'], {"v": "Viewer"})
                window.write_pin("v", "Edited")
                self.assertEqual(window.pins, {"v": "Edited"})
                self.assertTrue(window.save())
                self.assertEqual(read_pin_overrides(path), {})
                self.assertIn('"name": "Edited"', path.read_text(encoding="utf-8"))
            finally:
                window.placement_dirty = False
                for editor in window.documents:
                    editor.document().setModified(False)
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_invalid_optional_metadata_does_not_break_old_sidecars(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "pins.ckt"
            for contents in ('[]', '{"version":1,"pin_overrides":[]}', '{"version":9,"pin_overrides":{"v":"name"}}'):
                placement_path(path).write_text(contents, encoding="utf-8")
                self.assertEqual(read_pin_overrides(path), {})
