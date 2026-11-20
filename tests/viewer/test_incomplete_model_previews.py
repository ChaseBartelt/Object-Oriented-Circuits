"""Incomplete numerical models still permit honest, editable catalog previews."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.devices import DeviceLayouts
from circuitlab.layout.persistence import pack_layout
from circuitlab.model.graph import CircuitError
from circuitlab.viewer.device_layouts import DeviceLayoutDialog
from circuitlab.viewer.schematic import SchematicView, point


class IncompleteModelPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="ooc-catalog-preview-")
        self.config = str(Path(self.temp.name) / "defaults.ini")
        self.store = DeviceLayouts(QSettings(self.config, QSettings.Format.IniFormat))
        self.parent = QWidget()
        self.parent.schematic = SchematicView(self.store)
        self.dialog = DeviceLayoutDialog(self.parent, self.store, "LM358")
        self.dialog.show()
        self.app.processEvents()
        # Widgets release their settings and drafts before temporary files go.
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.close_widgets)

    def close_widgets(self):
        self.dialog.drafts.clear()
        self.dialog.close()
        self.dialog.deleteLater()
        self.parent.schematic.close()
        self.parent.schematic.deleteLater()
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    def select_device(self, key):
        self.dialog.select_device(key)
        self.app.processEvents()
        self.assertNotIn("Could not preview", self.dialog.message.text())
        self.assertIsNotNone(self.dialog.preview.layout)
        return self.store.preview(key)

    def test_reported_chips_render_warn_when_needed_and_allow_layout_application(self):
        for key in ("lm311", "lm339", "lm358", "lm393"):
            with self.subTest(device=key):
                circuit, root, view, boundary = self.select_device(key)
                self.assertIsNotNone(root)
                self.assertFalse(circuit.model_issues)
                if circuit.model_issues:
                    self.assertIn("Topology preview is available", self.dialog.message.text())
                    self.assertIn("Numerical model is incomplete", self.dialog.message.text())
                else:
                    self.assertNotIn("Numerical model is incomplete", self.dialog.message.text())
                self.assertTrue(self.dialog.preview.isEnabled())
                self.assertTrue(self.dialog.grid_controls.isEnabled())
                self.assertTrue(self.dialog.apply_button.isEnabled())
                self.assertTrue(self.dialog.preview.layout.symbols)
                self.assertTrue(preserves_nets(view, self.dialog.preview.layout))
                # The warning is meaningful: this topology-only graph cannot
                # accidentally be passed to a solver as a completed model.
                if circuit.model_issues:
                    with self.assertRaises(CircuitError) as caught:
                        circuit.require_models()
                    self.assertEqual(caught.exception.code, "UnsupportedModelProperty")
                else:
                    circuit.require_models()

                self.dialog.record_draft()
                self.dialog.apply_button.click()
                self.app.processEvents()
                self.assertTrue(self.store.has_saved(key))
                saved = self.store.saved(key)
                self.assertIsNotNone(saved)
                self.assertTrue(preserves_nets(view, saved))
                bound = self.store.for_instance(circuit.instances[0], view, boundary)
                self.assertIsNotNone(bound)
                self.assertTrue(preserves_nets(view, bound))

    def test_every_current_declaration_previews_persists_and_rebinds(self):
        # No fixed catalog count or named pinout-only list: actual declarations
        # and their model_available flag define the expected behavior.
        self.assertTrue(self.store.devices)
        for key, device in self.store.devices.items():
            with self.subTest(device=device.name):
                circuit, root, view, boundary = self.select_device(key)
                if root is None:
                    self.assertFalse(circuit.instances[0].model_available)
                    self.assertFalse(self.dialog.apply_button.isEnabled())
                    self.assertIn("Pinout only", self.dialog.message.text())
                    continue
                drawing = self.dialog.preview.layout
                self.assertTrue(self.dialog.apply_button.isEnabled())
                self.assertTrue(preserves_nets(view, drawing))
                self.assertTrue(self.store.set(key, drawing))
                saved = self.store.saved(key)
                self.assertIsNotNone(saved)
                self.assertTrue(preserves_nets(view, saved))
                rebound = self.store.for_instance(circuit.instances[0], view, boundary)
                self.assertIsNotNone(rebound)
                self.assertTrue(preserves_nets(view, rebound))

    def test_math_only_ad633_boundary_contacts_survive_save_reload_and_apply(self):
        circuit, root, view, boundary = self.select_device("ad633")
        self.assertIsNotNone(root)
        self.assertFalse(view.components)
        self.assertTrue(circuit.behavior)
        drawing = self.dialog.preview.layout
        self.assertFalse(drawing.symbols)
        self.assertEqual(set(drawing.boundary), set(boundary))
        self.assertEqual(len(boundary), len(circuit.instances[0].pins))
        self.assertTrue(set(boundary) <= self.dialog.preview.anchors.keys())
        self.assertTrue(preserves_nets(view, drawing))
        # A math-only sheet exposes real boundary contacts without inventing
        # components or requiring dangling one-contact wire stubs.
        probed = []
        self.dialog.preview.probed.connect(lambda kind, net: probed.append((kind, net)))
        for net in boundary:
            QTest.mouseClick(self.dialog.preview.viewport(), Qt.MouseButton.LeftButton,
                             pos=self.dialog.preview.mapFromScene(point(self.dialog.preview.anchors[net])))
            self.assertEqual(probed[-1], ("node", net))

        self.dialog.record_draft()
        self.dialog.apply_button.click()
        self.app.processEvents()
        restored_store = DeviceLayouts(QSettings(self.config, QSettings.Format.IniFormat))
        restored = restored_store.get("ad633")
        self.assertIsNotNone(restored)
        self.assertEqual(pack_layout(restored), pack_layout(drawing))
        self.assertTrue(preserves_nets(view, restored))
        rebound = restored_store.for_instance(circuit.instances[0], view, boundary)
        self.assertIsNotNone(rebound)
        self.assertEqual(set(rebound.boundary), set(boundary))
        self.assertTrue(preserves_nets(view, rebound))
