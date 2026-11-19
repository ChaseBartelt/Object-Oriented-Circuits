"""Library-wide layout editing, independent defaults and geometry reuse."""
# Preview real library declarations and rebind their saved geometry to other instances.
# Shared defaults, nested drafts, and user drag/undo behavior must not change device wiring.

import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.devices import DeviceLayouts, library_devices
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.gates import GateLayouts, default_gate_layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import pack_layout
from circuitlab.viewer.device_layouts import DeviceLayoutDialog
from circuitlab.viewer.exports import hierarchy_layouts
from circuitlab.viewer.schematic import SchematicView, point


class DeviceLayoutsTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = str(Path(self.temp.name) / "defaults.ini")
        self.store = DeviceLayouts(QSettings(self.config, QSettings.Format.IniFormat))
        self.parent = QWidget()
        self.parent.schematic = SchematicView(self.store)
        self.dialog = DeviceLayoutDialog(self.parent, self.store, "LM358")
        self.addCleanup(self.cleanup_widgets)

    def cleanup_widgets(self):
        self.dialog.drafts.clear()
        self.dialog.close()
        self.dialog.deleteLater()
        self.parent.schematic.close()
        self.parent.schematic.deleteLater()
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    def edit_current(self):
        preview = self.dialog.preview
        key = None, "auto"
        original = preview.layout
        part = original.symbols[0][0]
        moved = move_symbol(preview.levels[key], original, part.name, (0, 0.5))
        self.assertIsNotNone(moved)
        self.assertNotEqual(moved.positions, original.positions)
        preview.remember_placement(key)
        preview.manual_layouts[key] = moved
        preview._redraw()
        preview.placement_changed.emit()
        return moved

    def test_every_library_declaration_previews_and_restores_without_wiring_changes(self):
        devices = library_devices()
        self.assertEqual(len(devices), 48)  # 40 packages, uA741 wrapper, seven shared circuits
        self.assertEqual(sum(d.kind == "chip" for d in devices), 41)
        self.assertEqual(len({d.key for d in devices}), len(devices))
        pinouts = set()
        for device in devices:
            with self.subTest(device=device.name):
                self.dialog.select_device(device.key)
                self.assertNotIn("Could not preview", self.dialog.message.text())
                circuit, root, view, boundary = self.store.preview(device.key)
                if root is None:
                    pinouts.add(device.name)
                    self.assertFalse(self.dialog.apply_button.isEnabled())
                    self.assertFalse(self.dialog.grid_controls.isEnabled())
                    self.assertIn("Pinout only", self.dialog.message.text())
                    continue
                layout = self.dialog.preview.layout
                self.assertTrue(preserves_nets(view, layout))
                # Exercise the complete persistence/rebinding path for every
                # declaration, including switches and large sequential chips.
                self.assertTrue(self.store.set(device.key, layout))
                saved = self.store.saved(device.key)
                self.assertIsNotNone(saved)
                self.assertTrue(preserves_nets(view, saved))
                bound = self.store.for_instance(circuit.instances[0], view, boundary)
                self.assertIsNotNone(bound)
                self.assertTrue(preserves_nets(view, bound))
        self.assertEqual(pinouts, {"AD633", "LM13700", "PT2399", "ADS1115", "MCP4921", "CD4046B", "XR2206"})

    def test_defaults_are_individual_and_keep_existing_gate_settings_and_backups(self):
        gate = default_gate_layout("nand", 2)
        self.assertTrue(GateLayouts(self.store.settings).set(("nand", 2), gate))
        moved = self.edit_current()
        self.dialog.apply_layout()
        reloaded = DeviceLayouts(QSettings(self.config, QSettings.Format.IniFormat))
        self.assertEqual(reloaded.get("lm358").positions, moved.positions)
        self.assertIsNone(reloaded.get("lm393"))
        self.assertEqual(reloaded.get(("nand", 2)).positions, gate.positions)
        self.dialog.restore_automatic()
        self.dialog.apply_layout()
        self.assertIsNone(self.store.get("lm358"))
        self.dialog.restore_saved()
        self.dialog.apply_layout()
        self.assertEqual(self.store.get("lm358").positions, moved.positions)

    def test_reuse_tied_ports_local_layout_priority_and_collapsed_export(self):
        source = '''include("LM358");
main {
    LM358(out1, minus1, plus1, 0, plus2, minus2, out2, supply);
    LM358(out3, out3, input3, 0, input4, out4, out4, supply);
}'''
        circuit = compile_circuit(source, render_only=True)
        moved = self.edit_current()
        self.dialog.apply_layout()
        renderer = self.parent.schematic
        renderer.draw(circuit)
        details = hierarchy_layouts(renderer)
        self.assertEqual(set(details), {"@method:0", "@method:1"})
        for name, view, _, boundary in hood_levels(circuit, set(details)):
            if name is None:
                continue
            self.assertTrue(preserves_nets(view, details[name]))
            self.assertEqual([s[1:3] for s in details[name].symbols], [s[1:3] for s in moved.symbols])
        local = replace(details["@method:0"], label_offsets={('net', 'plus1'): (1, 2)})
        renderer.placement_records["@method:0", "auto"] = pack_layout(local)
        renderer.toggle_package("@method:0")
        renderer.refresh_gate_layouts()
        self.assertEqual(renderer.manual_layouts["@method:0", "auto"].label_offsets, local.label_offsets)
        renderer.toggle_package("@method:0")
        self.assertEqual(hierarchy_layouts(renderer)["@method:0"].label_offsets, local.label_offsets)
        self.assertIs(renderer.circuit, circuit)
        self.assertEqual(compile_circuit(source, render_only=True).components, circuit.components)

    def test_nested_blocks_select_their_own_defaults_and_keep_pending_drafts(self):
        self.dialog.select_device("NE555")
        moved = self.edit_current()
        circuit = self.dialog.preview.circuit
        target = next(i for i, instance in enumerate(circuit.instances) if instance.name == "oocDifferential")
        self.dialog.preview.toggle_package(f"@method:{target}")
        self.assertEqual(self.dialog.key, "oocdifferential")
        self.assertEqual(len(self.dialog.preview.levels), 1)
        gate = next(i for i, instance in enumerate(self.dialog.preview.circuit.instances) if instance.builtin_gate)
        with patch("circuitlab.viewer.device_layouts.GateLayoutDialog") as dialog_type:
            self.dialog.preview.toggle_package(f"@method:{gate}")
            dialog_type.return_value.exec.assert_called_once()
        self.dialog.select_device("NE555")
        self.assertEqual(self.dialog.preview.layout.positions, moved.positions)
        self.dialog.apply_layout()
        self.assertIsNone(self.store.get("oocdifferential"))

    def test_nested_packages_rebind_to_other_instance_numbers_and_grounded_ports(self):
        for key in ("ne555", "ne556", "sn74hc00", "oocdifferential"):
            with self.subTest(device=key):
                self.dialog.select_device(key)
                layout = self.dialog.preview.layout
                self.store.set(key, layout)
                device = self.store.devices[key]
                args = ["0" if pin in {"gnd", "vminus", "vss"} else "external_" + pin for pin in device.pins]
                source = f'''include("@chips/{device.file}");
chip wrapper({", ".join(device.pins)}) {{ {device.name}({", ".join(device.pins)}); }}
main {{ wrapper({", ".join(args)}); }}'''
                circuit = compile_circuit(source, render_only=True)
                name, view, _, boundary = next(row for row in hood_levels(circuit, {"@method:0", "@method:1"}) if row[0] == "@method:1")
                rebound = self.store.for_instance(circuit.instances[1], view, boundary)
                self.assertIsNotNone(rebound)
                self.assertTrue(preserves_nets(view, rebound))
                self.assertEqual([s[1:3] for s in rebound.symbols], [s[1:3] for s in layout.symbols])

    def test_real_drag_undo_filter_and_copyable_call(self):
        self.dialog.show()
        self.app.processEvents()
        preview = self.dialog.preview
        original = preview.layout
        part = original.symbols[0][0]
        preview.start_drag("component", part.name, (None, "auto"), preview.mapFromScene(point(original.symbols[0][1])))
        start = preview.drag.start
        end = start + preview.mapFromScene(point((0, 0.5))) - preview.mapFromScene(point((0, 0)))
        preview.preview_drag(end)
        QTest.mouseRelease(preview.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertNotEqual(preview.layout.positions, original.positions)
        self.assertIn("lm358", self.dialog.drafts)
        self.dialog.apply_layout()
        preview.setFocus()
        QTest.keyClick(preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(preview.layout.positions, original.positions)
        self.dialog.apply_layout()
        self.assertEqual(self.store.saved("lm358").positions, original.positions)
        self.dialog.search.setText("555")
        self.assertFalse(self.dialog.items["ne555"].isHidden())
        self.assertTrue(self.dialog.items["lm358"].isHidden())
        compiled = compile_circuit(self.dialog.call.toPlainText(), render_only=True)
        self.assertEqual(compiled.instances[0].name, "LM358")

    def test_invalid_saved_geometry_is_not_applied_to_a_changed_device(self):
        moved = self.edit_current()
        self.dialog.apply_layout()
        circuit, _, view, boundary = self.store.preview("lm358")
        changed = replace(view, components=view.components[:1])
        self.assertIsNone(self.store.for_instance(circuit.instances[0], changed, boundary))
        record = pack_layout(moved)
        record["symbols"][0][2] = ["broken"]
        import json
        self.store.settings.setValue(self.store.path("lm358"), json.dumps(record))
        self.assertIsNone(DeviceLayouts(self.store.settings).get("lm358"))


if __name__ == "__main__":
    unittest.main()
