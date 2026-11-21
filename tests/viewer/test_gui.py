"""Exercise the normal editor, solve, render, and file workflows through Qt."""
# Exercise normal editor and schematic workflows through real Qt events.
# Check source text and electrical contacts alongside visual state so a successful gesture cannot
# hide a changed connection, discarded edit, or incorrect export.

import json
import math
import os
import shutil
import unittest
from dataclasses import replace
from math import dist
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QHelpEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip

from circuitlab.layout.gates import GateLayouts, default_gate_layout, gate_preview
from circuitlab.viewer.legacy import GateLayoutDialog, MainWindow
from circuitlab.layout.geometry import rotate_point, symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.persistence import (
    layout_payload, pack_layout, placement_path, read_placement, restore_layout, strip_placement,
)
from circuitlab.math.report import component_labels
from circuitlab.layout.checks import mark_crossings as _mark_crossings
from circuitlab.layout.routing import fast_layout
from circuitlab.layout.editing import move_ground, move_symbol, move_symbols, move_wire
from circuitlab.viewer.schematic import SCALE, point
from circuitlab.viewer.snapping import grid_step, snap_radius
from circuitlab.exporters.spice import export_spice

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class ApplicationTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.fixtures_dir = TemporaryDirectory()
        self.addCleanup(self.fixtures_dir.cleanup)
        fixtures = Path(self.fixtures_dir.name)
        shutil.copytree(FIXTURES, fixtures, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.layout.json"))
        self.window = MainWindow()
        open_path = self.window.open_path

        def open_fixture(path, **kwargs):
            path = Path(path)
            if path.is_relative_to(FIXTURES):
                path = fixtures / path.relative_to(FIXTURES)
            return open_path(path, **kwargs)

        self.window.open_path = open_fixture
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.placement_dirty = False
        for editor, state in self.window.documents.items():
            state.placement_dirty = False
            editor.document().setModified(False)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_for_drag_preview(self, schematic, end):
        deadline = monotonic() + 1
        while schematic.drag.last_position != end and monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(schematic.drag.last_position, end)

    def test_gate_layout_dialog_resizes_and_allows_shortening_a_pin_lead(self):
        config = str(Path(self.fixtures_dir.name) / "pin-settings.ini")
        layouts = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        dialog = GateLayoutDialog(self.window, layouts, ("not", 1))
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        preview = dialog.preview
        viewport = preview.viewport()
        self.assertTrue(dialog.isSizeGripEnabled())
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        dialog.resize(540, 360)
        self.app.processEvents()
        small = viewport.size()
        dialog.resize(700, 500)
        self.app.processEvents()
        self.assertGreater(viewport.width(), small.width())
        self.assertGreater(viewport.height(), small.height())

        original = preview.layout
        port = original.boundary["a"]
        pin = next(pos for net, pos in original.pins() if net == "a")
        halfway = tuple((a + b) / 2 for a, b in zip(port, pin))
        marker = next(
            item for item in preview.scene().items()
            if item.data(0) == "boundary" and item.data(1) == "a" and not hasattr(item, "text")
        )
        start = preview.mapFromScene(marker.sceneBoundingRect().center())
        end = start + preview.mapFromScene(point(halfway)) - preview.mapFromScene(point(port))
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(preview.drag.kind, "boundary")
        QTest.mouseMove(viewport, end)
        self.wait_for_drag_preview(preview, end)
        moved = preview.layout
        self.assertLess(dist(pin, moved.boundary["a"]), dist(pin, port))
        self.assertLess(sum(dist(*edge) for edge in moved.wires["a"]),
                        sum(dist(*edge) for edge in original.wires["a"]))
        self.assertTrue(preserves_nets(preview.circuit, moved))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
        dialog.apply_layout()
        reloaded = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        self.assertEqual(reloaded.get(("not", 1)).boundary["a"], moved.boundary["a"])
        QTest.keyClick(preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(preview.layout.boundary["a"], port)
        dialog.restore_automatic()
        dialog.apply_layout()
        self.assertIsNone(layouts.get(("not", 1)))
        dialog.saved_button.click()
        self.assertEqual(preview.layout.boundary["a"], moved.boundary["a"])
        dialog.apply_layout()
        self.assertEqual(layouts.get(("not", 1)).boundary["a"], moved.boundary["a"])
        dialog.close()

    def test_gate_labels_drag_without_moving_their_anchors_and_reopen(self):
        config = str(Path(self.fixtures_dir.name) / "label-settings.ini")
        layouts = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        dialog = GateLayoutDialog(self.window, layouts, ("and", 2))
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        preview = dialog.preview
        viewport = preview.viewport()
        original = preview.layout
        resistor = next(part for part, *_ in original.symbols if part.kind == "R")
        package = next(part for part, *_ in original.symbols if part.kind == "H")
        for key in (
            ("component", resistor.name), ("terminal", package.name, 1),
            ("boundary", "a"), ("ground", *original.grounds[0]),
        ):
            with self.subTest(key=key):
                previous = preview.layout
                item = next(item for item in preview.scene().items() if item.data(4) == key)
                position = item.pos()
                start = preview.mapFromScene(item.sceneBoundingRect().center())
                end = start + QPoint(18, -29)
                QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
                self.assertIs(preview.drag.caption, item)
                QTest.mouseMove(viewport, end)
                self.wait_for_drag_preview(preview, end)
                self.assertNotEqual(item.pos(), position)
                QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
                self.assertIn(key, preview.layout.label_offsets)
                self.assertEqual(replace(preview.layout, label_offsets=original.label_offsets), original)
        QTest.keyClick(preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertIs(preview.layout, previous)
        dialog.apply_layout()
        expected = preview.layout.label_offsets
        reloaded = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        self.assertEqual(reloaded.get(("and", 2)).label_offsets, expected)
        dialog.close()

    def test_gate_layout_applies_across_instances_and_reloads_from_settings(self):
        win = self.window
        config = str(Path(self.fixtures_dir.name) / "gate-settings.ini")
        layouts = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        win.schematic.gate_layouts = layouts
        source = """chip Quad(supply, a, b, y, z) {
    nand(supply, a, b, y);
    nand(supply, a, a, z);
}
main { Quad(vcc, in_a, in_b, out_a, out_b); }
"""
        win.editor.setPlainText(source)
        win.render_source()
        schematic = win.schematic
        schematic.toggle_package("@method:0")
        for name in ("@method:1", "@method:2"):
            schematic.toggle_package(name)
        dial = GateLayoutDialog(win, layouts)
        self.addCleanup(dial.deleteLater)
        dial.gate.setCurrentIndex(dial.gate.findData("nand"))
        dial.show()
        self.app.processEvents()
        preview = dial.preview
        original = preview.layout
        _, a, b, _ = original.symbols[0]
        start = preview.mapFromScene(point(tuple((x + y) / 2 for x, y in zip(a, b))))
        end = start + QPoint(round(preview.transform().m11() * SCALE * -4), 0)
        viewport = preview.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(viewport, end)
        self.wait_for_drag_preview(preview, end)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
        self.assertNotEqual(preview.layout.positions, original.positions)
        ground = preview.layout.grounds[0]
        preview.rotate_ground((None, "auto"), ground)
        self.assertEqual(preview.layout.ground_rotations[ground], 1)
        template_part = preview.layout.symbols[0][0].name
        caption = next(
            item for item in preview.scene().items() if item.data(4) == ("component", template_part)
        )
        start = preview.mapFromScene(caption.sceneBoundingRect().center())
        end = start + QPoint(0, -22)
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
        label_offset = preview.layout.label_offsets["component", template_part]
        positions = [a for _, a, _, _ in preview.layout.symbols]
        dial.apply_layout()
        self.assertEqual(win.editor.toPlainText(), source)
        for name in ("@method:1", "@method:2"):
            key = name, "auto"
            drawing = schematic.level_layouts[key]
            self.assertEqual([a for _, a, _, _ in drawing.symbols], positions)
            self.assertEqual(drawing.ground_rotations[ground], 1)
            self.assertEqual(drawing.label_offsets["component", drawing.symbols[0][0].name], label_offset)
            self.assertTrue(preserves_nets(schematic.levels[key], drawing))
        new_layouts = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        self.assertEqual(new_layouts.get(("nand", 2)).positions, preview.layout.positions)
        self.assertIsNone(new_layouts.get(("nand", 3)))
        win.editor.setPlainText("""@Override
gate nand(vcc, gnd, a, b, y) {
    R1(1k) between(a, y);
}
main { nand(supply, 0, input_a, input_b, out); }
""")
        win.render_source()
        schematic.toggle_package("@method:0")
        custom = schematic.level_layouts["@method:0", "auto"]
        self.assertEqual(len(custom.symbols), 1)
        self.assertFalse(schematic.circuit.instances[0].builtin_gate)
        dial.restore_automatic()
        dial.apply_layout()
        self.assertIsNone(GateLayouts(QSettings(config, QSettings.Format.IniFormat)).get(("nand", 2)))
        dial.close()

    def test_wide_and_default_is_shared_by_gate_dialog_and_expanded_instances(self):
        win = self.window
        config = str(Path(self.fixtures_dir.name) / "and-layout-settings.ini")
        settings = QSettings(config, QSettings.Format.IniFormat)
        layouts = GateLayouts(settings)
        earlier, boundary = gate_preview("and", 3)
        previous = fast_layout(earlier, component_labels(earlier), boundary=boundary)
        record = pack_layout(previous)
        record.pop("label_offsets")
        settings.setValue(layouts.path(("and", 3)), json.dumps(record))
        win.schematic.gate_layouts = layouts
        win.editor.setPlainText(
            "and(vdd, 0, a, b, c, d, y);\nand(vdd, 0, a, a, c, d, e, z);\nand(vdd, 0, a, b, c, out);"
        )
        win.render_source()
        for index, count in enumerate((4, 5)):
            name = f"@method:{index}"
            win.schematic.toggle_package(name)
            key = name, "auto"
            drawing = win.schematic.level_layouts[key]
            default = default_gate_layout("and", count)
            self.assertEqual([(a, b) for _, a, b, _ in drawing.symbols],
                             [(a, b) for _, a, b, _ in default.symbols])
            self.assertTrue(preserves_nets(win.schematic.levels[key], drawing))
        default = default_gate_layout("and", 4)
        custom = replace(default, label_offsets={("boundary", "y"): (1, 1)})
        self.assertTrue(layouts.set(("and", 4), custom))
        dialog = GateLayoutDialog(win, layouts, ("and", 4))
        self.addCleanup(dialog.deleteLater)
        self.assertIs(dialog.preview.layout, custom)
        dialog.restore_automatic()
        self.assertIs(dialog.preview.layout, default)
        dialog.apply_layout()
        self.assertIsNone(layouts.get(("and", 4)))
        reloaded = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        self.assertIsNone(reloaded.get(("and", 4)))
        self.assertEqual(reloaded.saved(("and", 4)), custom)
        self.assertEqual(reloaded.get(("and", 3)), previous)
        self.assertTrue(dialog.saved_button.isEnabled())
        dialog.saved_button.click()
        self.assertEqual(dialog.preview.layout, custom)
        dialog.apply_layout()
        self.assertEqual(GateLayouts(QSettings(config, QSettings.Format.IniFormat)).get(("and", 4)), custom)
        dialog.inputs.setValue(3)
        self.assertEqual(dialog.preview.layout, previous)
        dialog.restore_automatic()
        three_input = default_gate_layout("and", 3)
        self.assertIs(dialog.preview.layout, three_input)
        dialog.apply_layout()
        win.schematic.toggle_package("@method:2")
        drawing = win.schematic.level_layouts["@method:2", "auto"]
        self.assertEqual([(a, b) for _, a, b, _ in drawing.symbols],
                         [(a, b) for _, a, b, _ in three_input.symbols])
        dialog.close()

    def test_nand_or_and_nor_defaults_are_used_in_gate_dialog_and_expanded_circuit(self):
        win = self.window
        config = str(Path(self.fixtures_dir.name) / "gate-default-settings.ini")
        layouts = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        win.schematic.gate_layouts = layouts
        win.editor.setPlainText(
            "nand(vdd, 0, a, a, b, nand_y);\n"
            "nand(vdd, ret, a, b, c, d, nand_out);\n"
            "or(vdd, 0, a, a, b, or_y);\n"
            "or(vdd, ret, a, b, c, d, or_out);\n"
            "nor(vdd, 0, a, nor_y);\n"
            "nor(vdd, ret, a, b, c, d, nor_out);"
        )
        win.render_source()
        cases = (("nand", 3), ("nand", 4), ("or", 3), ("or", 4), ("nor", 1), ("nor", 4))
        for index, (gate, count) in enumerate(cases):
            with self.subTest(gate=gate, count=count):
                dialog = GateLayoutDialog(win, layouts, (gate, count))
                self.addCleanup(dialog.deleteLater)
                default = default_gate_layout(gate, count)
                self.assertIs(dialog.preview.layout, default)
                name = f"@method:{index}"
                win.schematic.toggle_package(name)
                key = name, "auto"
                drawing = win.schematic.level_layouts[key]
                self.assertEqual([(a, b) for _, a, b, _ in drawing.symbols],
                                 [(a, b) for _, a, b, _ in default.symbols])
                self.assertTrue(preserves_nets(win.schematic.levels[key], drawing))
                dialog.close()

    def test_apply_without_a_draft_keeps_an_unreadable_gate_layout(self):
        config = str(Path(self.fixtures_dir.name) / "unreadable-gate-settings.ini")
        settings = QSettings(config, QSettings.Format.IniFormat)
        layouts = GateLayouts(settings)
        key = "and", 3
        stored = '{"symbols": []}'
        settings.setValue(layouts.path(key), stored)
        dialog = GateLayoutDialog(self.window, layouts, key)
        self.addCleanup(dialog.deleteLater)
        self.assertIsNone(layouts.get(key))
        dialog.apply_layout()
        self.assertEqual(settings.value(layouts.path(key)), stored)
        self.assertFalse(settings.contains(layouts.path(key, "automatic/v1")))
        dialog.close()

    def test_gate_resistor_snaps_straight_into_the_transistor_base(self):
        config = str(Path(self.fixtures_dir.name) / "snap-settings.ini")
        layouts = GateLayouts(QSettings(config, QSettings.Format.IniFormat))
        dialog = GateLayoutDialog(self.window, layouts, ("buf", 1))
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        preview = dialog.preview
        original = preview.layout
        transistor = next(
            symbol for symbol in original.symbols if symbol[0].base and symbol[0].name.startswith("Q1")
        )
        net = transistor[0].base
        resistor = next(
            symbol for symbol in original.symbols if symbol[0].kind == "R" and net in symbol[0].terminal_nets
        )
        part, a, b, _ = resistor
        name = part.name
        base = next(pos for wire, pos in symbol_pins(*transistor) if wire == net)
        lead = next(pos for wire, pos in symbol_pins(*resistor) if wire == net)
        start = preview.mapFromScene(point(tuple((x + y) / 2 for x, y in zip(a, b))))
        end = start + preview.mapFromScene(point(base)) - preview.mapFromScene(point(lead)) + QPoint(1, 1)
        self.assertTrue(original.wires[net])
        viewport = preview.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(viewport, end)
        self.wait_for_drag_preview(preview, end)
        self.assertIsNotNone(preview.drag.snapped_terminal)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
        moved = next(symbol for symbol in preview.layout.symbols if symbol[0].name == name)
        pin = next(pos for wire, pos in symbol_pins(*moved) if wire == net)
        self.assertAlmostEqual(dist(pin, base), 0)
        self.assertNotIn(net, preview.layout.wires)
        self.assertTrue(preserves_nets(preview.circuit, preview.layout))
        preview.undo_placement()
        self.assertIs(preview.layout, original)
        dialog.close()

    def test_solve_search_and_probe(self):
        win = self.window
        self.assertTrue(win.open_path(FIXTURES / "voltage_divider.ckt"))
        win.results.search.setText("node out")
        self.assertEqual(win.results.table.rowCount(), 1)
        self.assertEqual(win.results.table.item(0, 2).text(), "4 V")
        win.show_analysis()
        self.assertTrue(win.analysis_window.isVisible())
        win.analysis_window.close()
        label = next(
            item
            for item in win.schematic.scene().items()
            if item.data(0) == "node" and item.data(1) == "out"
        )
        QTest.mouseClick(
            win.schematic.viewport(),
            Qt.MouseButton.LeftButton,
            pos=win.schematic.mapFromScene(label.sceneBoundingRect().center()),
        )
        self.assertIn("V(out) relative to 0 = 4 V", win.status.text())

    def test_preview_toggle_gives_the_editor_full_width(self):
        win = self.window
        win.open_path(FIXTURES / "voltage_divider.ckt")
        win.split.setSizes([530, 750])
        self.app.processEvents()
        width = win.editor.width()
        drawing = win.schematic.layout
        win.views.setCurrentWidget(win.results)
        win.preview_button.click()
        self.app.processEvents()
        self.assertTrue(win.views.isHidden())
        self.assertGreater(win.editor.width(), width)
        self.assertEqual(win.preview_button.text(), "Show preview")
        self.assertIs(win.schematic.layout, drawing)
        win.preview_button.click()
        self.app.processEvents()
        self.assertTrue(win.views.isVisible())
        self.assertEqual(win.editor.width(), width)
        self.assertIs(win.views.currentWidget(), win.results)
        self.assertIs(win.schematic.layout, drawing)

    def test_drag_moves_package_and_resistor_without_editing_the_circuit(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        source = win.editor.toPlainText()
        netlist = export_spice(source)
        original = schematic.layout

        def drag(name, start, dx, dy):
            before = schematic.layout.positions[name]
            circuit = schematic.levels[None, "auto"]
            shifts = ((dx, -dy), (0, 3), (0, -3), (-3, 0), (3, 0), (-1, 0), (0, -1))
            shift = next((move for move in shifts if move_symbol(
                circuit, schematic.layout, name, move,
            ) is not None), None)
            self.assertIsNotNone(shift)
            scale = schematic.transform().m11() * SCALE
            end = start + QPoint(round(shift[0] * scale), round(-shift[1] * scale))
            QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(schematic.viewport(), end)
            self.wait_for_drag_preview(schematic, end)
            preview = schematic.layout
            self.assertNotEqual(preview.positions[name], before)
            QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertIs(schematic.layout, preview)
            self.assertTrue(preserves_nets(schematic.levels[None, "auto"], schematic.layout))

        chip = next(item for item in schematic.scene().items()
                    if item.data(0) == "hood" and item.toolTip() == "Open timer555"
                    and not hasattr(item, "text"))
        drag("@method:0", schematic.mapFromScene(chip.sceneBoundingRect().center()), 3, 0)
        self.assertFalse(schematic.expanded)
        _, a, b, _ = next(symbol for symbol in schematic.layout.symbols if symbol[0].name == "RA")
        center = point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        drag("RA", schematic.mapFromScene(center), 0, -3)
        self.assertEqual(win.editor.toPlainText(), source)
        self.assertEqual(export_spice(win.editor.toPlainText()), netlist)
        self.assertFalse(win.editor.document().isModified())
        schematic.reset_placement()
        self.assertIs(schematic.layout, original)

    def test_ctrl_click_selects_parts_for_one_live_and_undoable_drag(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        source = win.editor.toPlainText()
        original = schematic.layout
        chip = "@method:0"

        def center(name):
            _, a, b, _ = next(row for row in schematic.layout.symbols if row[0].name == name)
            return schematic.mapFromScene(point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)))

        ctrl = Qt.KeyboardModifier.ControlModifier
        viewport = schematic.viewport()
        for name in ("RA", chip, "RB", "RB"):
            QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, ctrl, center(name))
        self.assertEqual(schematic.selected_symbols, {"RA", chip})
        self.assertFalse(schematic.expanded)
        outline, = schematic.selection_items
        for name in ("RA", chip):
            self.assertTrue(outline.rect().contains(schematic.mapToScene(center(name))))

        start = center(chip)
        circuit = schematic.levels[None, "auto"]
        shifts = ((0, -3), (0, 3), (3, 0), (-3, 0), (0, -1), (0, 1))
        shift = next((move for move in shifts if move_symbols(
            circuit, original, schematic.selected_symbols, move,
        ) is not None), None)
        self.assertIsNotNone(shift)
        scale = schematic.transform().m11() * SCALE
        end = start + QPoint(round(shift[0] * scale), round(-shift[1] * scale))
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, ctrl, start)
        QTest.mouseMove(viewport, end)
        self.wait_for_drag_preview(schematic, end)
        preview = schematic.layout
        shift = tuple(a - b for a, b in zip(preview.positions[chip], original.positions[chip]))
        self.assertNotEqual(shift, (0, 0))
        for a, b, offset in zip(preview.positions["RA"], original.positions["RA"], shift):
            self.assertAlmostEqual(a - b, offset)
        self.assertEqual(preview.positions["RB"], original.positions["RB"])
        self.assertTrue(preserves_nets(schematic.levels[None, "auto"], preview))
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, ctrl, end)
        self.assertIs(schematic.layout, preview)
        self.assertEqual(win.editor.toPlainText(), source)
        self.assertFalse(win.editor.document().isModified())
        QTest.keyClick(schematic, Qt.Key.Key_Z, ctrl)
        self.assertIs(schematic.layout, original)

        schematic.clear_selection()
        locations = [center(name) for name in ("RA", chip)]
        start = QPoint(min(pos.x() for pos in locations) - 20, 3)
        end = QPoint(max(pos.x() for pos in locations) + 8,
                     max(pos.y() for pos in locations) + 12)
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, ctrl, start)
        QTest.mouseMove(viewport, end)
        self.assertTrue(schematic.selection_box.isVisible())
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, ctrl, end)
        self.assertFalse(schematic.selection_box.isVisible())
        self.assertTrue({"RA", chip}.issubset(schematic.selected_symbols))

    def test_chip_library_browses_previews_and_completes_imported_chips(self):
        from circuitlab.language.linked import CHIP_LIBRARY

        win = self.window
        win.show_chip_library()
        self.assertEqual(Path(win.file_model.rootPath()), CHIP_LIBRARY)
        self.assertTrue(win.open_path(CHIP_LIBRARY / "logic" / "74HC00.ckt"))
        self.assertTrue(win.schematic.packages)
        self.assertIsNone(win.solution)
        self.assertFalse(win.editor.document().isModified())
        win.open_path(FIXTURES / "refactor" / "chip_library_nand.ckt")
        self.assertEqual(win.editor.method_signatures()["sn74hc00"][0], "chip")
        self.assertTrue(win.schematic.packages)
        self.assertIsNotNone(win.solution.network)

    def test_clicking_the_same_wire_again_clears_its_highlight(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        path = schematic.paths["supply"]
        normal_pen = path.pen()
        a, b = max(schematic.layout.visible_segments("supply"), key=lambda edge: dist(*edge))
        midpoint = point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        pos = schematic.mapFromScene(midpoint)

        QTest.mouseClick(schematic.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(schematic.highlight, "supply")
        self.assertNotEqual(path.pen(), normal_pen)
        QTest.mouseClick(schematic.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.assertIsNone(schematic.highlight)
        self.assertEqual(path.pen(), normal_pen)

    def test_ground_drag_previews_a_connected_move(self):
        win = self.window
        for example in ("timer555.ckt", "voltage_divider.ckt"):
            with self.subTest(example=example):
                win.open_path((FIXTURES / "refactor" if example == "timer555.ckt" else FIXTURES) / example)
                schematic = win.schematic
                source = win.editor.toPlainText()
                original = schematic.layout
                ground = original.grounds[0]
                start = schematic.mapFromScene(point((ground[0], ground[1] - 0.42)))
                scale = round(schematic.transform().m11() * SCALE)
                end = start + QPoint(-scale, scale)

                QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
                self.assertEqual(schematic.drag.kind, "ground")
                QTest.mouseMove(schematic.viewport(), end)
                self.wait_for_drag_preview(schematic, end)
                preview = schematic.layout
                moved = preview.grounds[0]
                self.assertNotEqual(moved, ground)
                self.assertEqual(preview.grounds[1:], original.grounds[1:])
                self.assertTrue(any(moved in edge for edge in preview.wires["0"]))
                self.assertTrue(preserves_nets(schematic.levels[None, "auto"], preview))

                QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
                self.assertIs(schematic.layout, preview)
                self.assertEqual(preview.positions, original.positions)
                self.assertEqual(win.editor.toPlainText(), source)
                self.assertFalse(win.editor.document().isModified())

    def test_ground_drag_snaps_directly_below_its_source_pin(self):
        win = self.window
        win.open_path(FIXTURES / "voltage_divider.ckt")
        schematic = win.schematic
        key = None, "auto"
        original, circuit = schematic.layout, schematic.levels[key]
        pin = next(b for part, _, b, _ in original.symbols if part.kind == "V")
        index, ground = min(enumerate(original.grounds), key=lambda entry: dist(entry[1], pin))
        offset = move_ground(circuit, original, ground, (-0.5, -0.28))
        self.assertIsNotNone(offset)
        schematic.manual_layouts[key] = offset
        schematic.redraw(schematic.saved_pins)
        x, y = schematic.layout.grounds[index]
        start = schematic.mapFromScene(point((x, y - 0.42)))
        end = start + QPoint(round((pin[0] - x) * schematic.transform().m11() * SCALE) + 1, 12)

        QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(schematic.viewport(), end)
        self.wait_for_drag_preview(schematic, end)
        preview = schematic.layout
        moved = preview.grounds[index]
        self.assertEqual(moved[0], pin[0])
        self.assertIn(tuple(sorted((moved, pin))), preview.wires["0"])
        QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertIs(schematic.layout, preview)
        self.assertTrue(preserves_nets(circuit, preview))
        self.assertFalse(win.editor.document().isModified())

    def test_split_move_and_join_connectors_then_undo(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        original = schematic.layout
        a, b = max(original.wires["supply"], key=lambda edge: abs(edge[1][0] - edge[0][0]))
        middle = schematic.mapFromScene(point(((a[0] + b[0]) / 2, a[1])))
        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, middle, schematic.viewport().mapToGlobal(middle)
        )
        with patch("circuitlab.viewer.schematic.QMenu") as menu:
            schematic.contextMenuEvent(event)
            dict(call.args for call in menu.return_value.addAction.call_args_list)["Split"]()
        split = schematic.layout
        i, j = len(split.connectors) - 2, len(split.connectors) - 1
        step = grid_step(schematic.transform().m11() * SCALE)
        for index in (i, j):
            for value in split.connectors[index][1]:
                self.assertAlmostEqual(value / step, round(value / step))
        net, left, name, *_ = split.connectors[i]
        self.assertEqual((net, name), ("supply", split.connectors[j][2]))
        circuit = schematic.levels[None, "auto"]
        self.assertTrue(preserves_nets(circuit, split))

        def drag(start, end):
            start, end = (schematic.mapFromScene(point(p)) for p in (start, end))
            QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
            self.assertEqual(schematic.drag.kind, "connector")
            QTest.mouseMove(schematic.viewport(), end)
            self.wait_for_drag_preview(schematic, end)
            preview = schematic.layout
            QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertIs(schematic.layout, preview)
            self.assertTrue(preserves_nets(circuit, preview))
            return preview

        moved = drag(left, (left[0] - 2, left[1] + 1.5))
        self.assertNotEqual(moved.connectors[i][1], left)
        self.assertEqual(moved.connectors[j], split.connectors[j])
        joined = drag(moved.connectors[i][1], moved.connectors[j][1])
        self.assertEqual(joined.connectors, original.connectors)
        schematic.undo_placement()
        self.assertIs(schematic.layout, moved)

    def test_rotated_ground_snaps_to_the_package_pin(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        original = schematic.layout
        x, y = original.grounds[-1]
        end = (x, y)
        initial = original.ground_rotations.get(end, 0)
        cursor = schematic.mapFromScene(point(rotate_point((x, y - 0.42), end, end, initial)))
        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, cursor, schematic.viewport().mapToGlobal(cursor)
        )
        with patch("circuitlab.viewer.schematic.QMenu") as menu:
            schematic.contextMenuEvent(event)
            actions = dict(call.args for call in menu.return_value.addAction.call_args_list)
            self.assertEqual(list(actions)[:2], ["Rotate clockwise", "Rotate counterclockwise"])
            actions["Rotate counterclockwise"]()
            self.assertEqual(schematic.layout.ground_rotations.get(end, 0), (initial - 1) % 4)
            schematic.undo_placement()
            actions["Rotate clockwise"]()
        self.assertEqual(schematic.layout.ground_rotations.get(end, 0), (initial + 1) % 4)
        a, b = next(edge for edge in original.wires["0"] if (x, y) in edge)
        pin = a if b == (x, y) else b
        facing = 1 if x < pin[0] else 3
        if (initial + 1) % 4 != facing:
            schematic.rotate_ground((None, "auto"), end, direction=facing - (initial + 1) % 4)
        start = schematic.mapFromScene(point(rotate_point((x, y - 0.42), end, end, facing)))
        end = start + schematic.mapFromScene(point(pin)) - schematic.mapFromScene(point((x, y)))
        QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(schematic.drag.kind, "ground")
        QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
        layout = schematic.layout
        self.assertAlmostEqual(layout.grounds[-1][0], pin[0])
        self.assertAlmostEqual(layout.grounds[-1][1], pin[1])
        self.assertEqual(layout.ground_rotations[layout.grounds[-1]], facing)
        self.assertEqual(len(layout.wires["0"]), len(original.wires["0"]) - 1)
        self.assertTrue(preserves_nets(schematic.levels[None, "auto"], layout))

    def test_saved_layout_reopens_in_a_new_window_without_changing_source(self):
        win = self.window
        with TemporaryDirectory() as directory:
            path = Path(directory) / "timer555.ckt"
            source = (FIXTURES / "refactor" / "timer555.ckt").read_bytes()
            path.write_bytes(source)
            win.open_path(path)
            win.wire_style.setCurrentIndex(win.wire_style.findData("routed"))
            schematic = win.schematic
            original = schematic.layout
            a, b = max(original.wires["supply"], key=lambda edge: abs(edge[1][0] - edge[0][0]))
            start = schematic.mapFromScene(point(((a[0] + b[0]) / 2, a[1])))
            end = start - QPoint(0, round(0.5 * schematic.transform().m11() * SCALE))
            QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertNotEqual(schematic.layout.wires, original.wires)
            schematic.rotate_ground((None, "routed"), schematic.layout.grounds[-1])
            key = ("component", "RA")
            caption = next(item for item in schematic.scene().items() if item.data(4) == key)
            start = schematic.mapFromScene(caption.sceneBoundingRect().center())
            QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(12, -18))
            saved = schematic.layout
            self.assertIn(key, saved.label_offsets)
            self.assertTrue(win.isWindowModified())
            self.assertTrue(win.save())
            self.assertFalse(win.isWindowModified())
            self.assertIs(schematic.layout, saved)
            self.assertTrue(schematic.undo_history)
            self.assertEqual(strip_placement(path.read_bytes().decode("utf-8-sig")), source.decode("utf-8-sig"))
            self.assertIsNotNone(layout_payload(path.read_text(encoding="utf-8-sig")))
            self.assertFalse(placement_path(path).exists())
            self.assertEqual(win.editor.toPlainText(), path.read_text(encoding="utf-8-sig"))

            reopened = MainWindow()
            try:
                reopened.open_path(path, reload=True)
                self.assertEqual(reopened.wire_style.currentData(), "routed")
                self.assertEqual(reopened.schematic.layout, saved)
                reopened.render_source()
                self.assertEqual(reopened.schematic.layout, saved)
                reopened.schematic.reset_placement()
                self.assertTrue(reopened.save())
                reopened.open_path(path, reload=True)
                self.assertEqual(reopened.schematic.layout, original)
            finally:
                reopened.placement_dirty = False
                reopened.close()
                reopened.deleteLater()

    def test_hovering_a_wire_in_the_editor_previews_only_its_path(self):
        win = self.window
        source = (
            "VCC(5) between(start, 0);\n"
            "R1(1k) between(start, middle);\n"
            "R2(1k) between(middle, 0);\n"
            "R3(1k) between(middle, split);\n"
            "R4(1k) between(split, 0);"
        )
        win.editor.setPlainText(source)
        win.render_source()
        schematic = win.schematic
        scene, layout = schematic.scene(), schematic.layout
        cursor = win.editor.textCursor()
        cursor.setPosition(source.index("middle", source.index("R3")) + 2)
        pos = win.editor.cursorRect(cursor).center()
        QApplication.sendEvent(win.editor.viewport(), QHelpEvent(
            QEvent.Type.ToolTip, pos, win.editor.viewport().mapToGlobal(pos),
        ))
        self.app.processEvents()
        preview = win.trace_view
        self.assertTrue(win.trace_popup.isVisible())
        self.assertEqual({part.name for part, *_ in preview.layout.symbols}, {"R1", "R3", "R4"})
        self.assertEqual(preview.trace_nets, {"middle"})
        self.assertEqual(preview.paths["middle"].pen().color().name(), "#006dab")
        self.assertIs(schematic.scene(), scene)
        QApplication.sendEvent(win.editor.viewport(), QEvent(QEvent.Type.Leave))
        self.assertFalse(win.trace_popup.isVisible())
        self.assertIs(schematic.scene(), scene)
        self.assertIs(schematic.layout, layout)

    def test_hover_on_builtin_gate_and_chip_shows_reference_pinout(self):
        win = self.window
        source = "chip timer555(gnd, trig, out, reset, ctrl, thres, disch, vcc) {\n    not(vcc, trig, out);\n}"
        win.editor.setPlainText(source)
        for name, expected in (
            ("timer555", ("chip", "timer555", ("gnd", "trig", "out", "reset", "ctrl", "thres", "disch", "vcc"))),
            ("not", ("builtin", "NOT", 1)),
        ):
            cursor = win.editor.textCursor()
            cursor.setPosition(source.index(name) + 1)
            pos = win.editor.cursorRect(cursor).center()
            QApplication.sendEvent(win.editor.viewport(), QHelpEvent(
                QEvent.Type.ToolTip, pos, win.editor.viewport().mapToGlobal(pos),
            ))
            self.assertTrue(win.device_popup.isVisible())
            self.assertEqual(win.device_reference.reference, expected)
            QApplication.sendEvent(win.editor.viewport(), QEvent(QEvent.Type.Leave))
            self.assertFalse(win.device_popup.isVisible())

    def test_folder_and_tabs_compile_unsaved_connector_file(self):
        win = self.window
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            root = folder / "main.ckt"
            load = folder / "load.ckt"
            root.write_text('include("load.ckt");\nconnector(signal, 0);\n'
                            'V1(8) between(vcc, 0);\nR1(1k) between(vcc, signal);\n')
            load.write_text("connector(signal, 0);\nR1(1k) between(signal, 0);\n")
            with patch("circuitlab.viewer.legacy.QFileDialog.getExistingDirectory", return_value=str(folder)):
                win.pick_folder()
            self.assertEqual(Path(win.file_model.rootPath()), folder)
            self.assertTrue(win.explorer.isVisible())
            win.open_path(root)
            win.open_path(load)
            edit = win.editor
            self.assertEqual(win.editor_tabs.count(), 2)
            win.editor.setPlainText(edit.toPlainText().replace("1k", "3k"))
            win.editor.document().setModified(True)
            win.open_path(root)
            signal = win.solution.network.nodes.index("signal")
            self.assertAlmostEqual(win.solution.network.voltage[signal], 6)
            win.open_path(load)
            self.assertIs(win.editor, edit)
            self.assertIn("3k", edit.toPlainText())
            self.assertIn("1k", load.read_text())
            self.assertEqual(win.editor_tabs.count(), 2)

    def test_files_panel_navigates_folder_history(self):
        win = self.window
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            with patch("circuitlab.viewer.legacy.QFileDialog.getExistingDirectory", return_value=str(root)):
                win.pick_folder()
            win.open_tree_file(win.file_model.index(str(first)))
            self.assertEqual(Path(win.file_model.rootPath()), first)
            self.assertFalse(win.folder_forward.isEnabled())
            win.folder_back.click()
            self.assertEqual(Path(win.file_model.rootPath()), root)
            self.assertTrue(win.folder_forward.isEnabled())
            win.folder_forward.click()
            self.assertEqual(Path(win.file_model.rootPath()), first)
            win.folder_up.click()
            self.assertEqual(Path(win.file_model.rootPath()), root)
            win.open_tree_file(win.file_model.index(str(second)))
            self.assertEqual(Path(win.file_model.rootPath()), second)
            self.assertEqual(win.editor_tabs.count(), 1)

    def test_bjt_oscillator_runs_and_pauses_in_scope_sidebar(self):
        win = self.window
        win.open_path(FIXTURES / "bjt_oscillator.ckt")
        scope = win.scope
        self.app.processEvents()
        self.assertTrue(win.schematic.isVisible())
        self.assertFalse(scope.isVisible())
        full_width = win.schematic.width()
        win.scope_toggle.click()
        self.app.processEvents()
        self.assertTrue(win.schematic.isVisible())
        self.assertTrue(scope.isVisible())
        self.assertLess(win.schematic.width(), full_width)
        scope.node.setCurrentText("c1")
        scope.initial.setCurrentText("C1")
        scope.voltage.setValue(1)
        scope.run.click()
        self.assertEqual(scope.run.text(), "Pause")
        simulation = scope.simulation
        win.scope_toggle.click()
        self.app.processEvents()
        self.assertFalse(scope.isVisible())
        self.assertEqual(win.schematic.width(), full_width)
        self.assertTrue(scope.timer.isActive())
        win.scope_toggle.click()
        self.assertIs(scope.simulation, simulation)
        for _ in range(35):
            scope.pending[1].result(timeout=5)
            scope.advance()
        scope.run.click()
        points = tuple(scope.waveform.samples)
        self.assertEqual(scope.run.text(), "Resume")
        self.assertFalse(scope.timer.isActive())
        self.assertGreater(sum((x > 2) != (y > 2) for (_, x), (_, y) in zip(points, points[1:])), 5)
        scope.run.click()
        scope.pending[1].result(timeout=5)
        scope.advance()
        scope.pending[1].result(timeout=5)
        scope.advance()
        self.assertGreater(scope.waveform.samples[-1][0], points[-1][0])
        scope.run.click()

    def test_ctrl_z_undoes_wire_moves_only_when_schematic_has_focus(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        win.activateWindow()
        self.app.processEvents()
        original = schematic.layout
        source = win.editor.toPlainText()
        layouts = [original]
        for _ in range(2):
            a, b = max(
                ((a, b) for a, b in schematic.layout.wires["supply"] if a[1] == b[1]),
                key=lambda edge: dist(*edge),
            )
            start = schematic.mapFromScene(point(((a[0] + b[0]) / 2, a[1])))
            # Two different, clear grid rows; +1 would land on discharge's rail.
            distance = 0.25 if _ == 0 else 0.5
            end = start + QPoint(0, -round(distance * schematic.transform().m11() * SCALE))
            QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertIsNot(schematic.layout, layouts[-1])
            layouts.append(schematic.layout)

        undo = Qt.KeyboardModifier.ControlModifier
        QTest.keyClick(schematic.viewport(), Qt.Key.Key_Z, undo)
        self.assertIs(schematic.layout, layouts[1])

        win.results.search.setFocus()
        QTest.keyClick(win.results.search, Qt.Key.Key_Z, undo)
        self.assertIs(schematic.layout, layouts[1])

        schematic.setFocus()
        QTest.keyClick(schematic.viewport(), Qt.Key.Key_Z, undo)
        self.assertIs(schematic.layout, original)
        self.assertFalse(win.editor.document().isModified())

        win.editor.setFocus()
        win.editor.insertPlainText("# edit\n")
        QTest.keyClick(win.editor, Qt.Key.Key_Z, undo)
        self.assertEqual(win.editor.toPlainText(), source)

    def test_wire_drag_snaps_and_holds_a_jog_flat_in_the_preview(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        original = schematic.layout
        source_pin = original.positions["VCC"]
        a, b = next(
            (a, b) for a, b in original.wires["supply"] if a == source_pin and a[1] == b[1]
        )
        x, y = (a[0] + b[0]) / 2, a[1]
        start = schematic.mapFromScene(point((x, y)))
        bumped = schematic.mapFromScene(point((x, y - 1)))
        QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=bumped)
        jog = next(
            (p, q) for p, q in schematic.layout.wires["supply"]
            if p[1] == q[1] and p[0] < x < q[0]
        )
        self.assertNotEqual(jog[0][1], y)
        start = schematic.mapFromScene(point((x, jog[0][1])))
        level = schematic.mapFromScene(point((x, y)))
        pixels = schematic.transform().m11() * SCALE
        acquire, release = snap_radius(pixels, grid_step(pixels))
        near_level = level + QPoint(0, max(1, int(acquire * pixels / 2)))

        QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(schematic.viewport(), near_level)
        self.wait_for_drag_preview(schematic, near_level)
        preview = schematic.layout
        self.assertTrue(any(
            p[1] == q[1] == y and p[0] < x < q[0] for p, q in preview.wires["supply"]
        ))

        still_snapped = level - QPoint(0, max(1, int(release * pixels - 1)))
        QTest.mouseMove(schematic.viewport(), still_snapped)
        self.wait_for_drag_preview(schematic, still_snapped)
        self.assertIs(schematic.layout, preview)

        beyond_snap = level - QPoint(0, math.ceil(release * pixels) + 4)
        QTest.mouseMove(schematic.viewport(), beyond_snap)
        self.wait_for_drag_preview(schematic, beyond_snap)
        self.assertIsNone(schematic.drag.snapped_offset)
        self.assertIsNot(schematic.layout, preview)

        QTest.mouseMove(schematic.viewport(), near_level)
        self.wait_for_drag_preview(schematic, near_level)
        preview = schematic.layout
        self.assertTrue(any(
            p[1] == q[1] == y and p[0] < x < q[0] for p, q in preview.wires["supply"]
        ))
        QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=still_snapped)
        self.assertIs(schematic.layout, preview)
        self.assertTrue(preserves_nets(schematic.levels[None, "auto"], preview))
        self.assertFalse(win.editor.document().isModified())

    def test_resistor_drag_snaps_its_pin_level_with_a_branched_wire(self):
        win = self.window
        fixture = FIXTURES / "refactor" / "timer555.ckt"
        win.open_path(fixture)
        schematic = win.schematic
        key = None, "auto"
        circuit = schematic.levels[key]
        mode, records = read_placement(fixture)
        original = restore_layout(records[None, mode], circuit, ())
        self.assertIsNotNone(original)
        original = align_layout(circuit, original)
        self.assertIsNotNone(original)
        left, right, discharge = (pin for net, pin in original.pins() if net == "discharge")
        tee = ((left[0] + right[0]) / 2, left[1])
        bend = tee[0], discharge[1]
        straight = replace(
            original,
            wires={**original.wires, "discharge": [
                (left, tee), (tee, right), (tee, bend), (bend, discharge),
            ]},
            junctions={**original.junctions, "discharge": [tee]},
            labels=dict(original.labels),
            crossings={},
        )
        _mark_crossings(straight)
        self.assertTrue(preserves_nets(circuit, straight))

        for shift in (0.38, -0.38):
            with self.subTest(shift=shift):
                moved = move_symbol(circuit, straight, "RB", (0, shift))
                self.assertIsNotNone(moved)
                schematic.manual_layouts[key] = moved
                schematic.redraw(schematic.saved_pins)
                _, a, b, _ = next(
                    symbol for symbol in schematic.layout.symbols if symbol[0].name == "RB"
                )
                start = schematic.mapFromScene(point(((a[0] + b[0]) / 2, a[1])))
                direction = 1 if shift > 0 else -1
                end = start + QPoint(0, round(shift * schematic.transform().m11() * SCALE) - direction)

                QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
                QTest.mouseMove(schematic.viewport(), end)
                self.wait_for_drag_preview(schematic, end)
                preview = schematic.layout
                self.assertEqual(preview.positions["RA"], original.positions["RA"])
                self.assertEqual(preview.positions["RB"][1], left[1])
                self.assertIn((left, tee), preview.wires["discharge"])
                self.assertIn((tee, (round(right[0], 8), right[1])), preview.wires["discharge"])
                self.assertIn(tee, preview.junctions["discharge"])
                QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
                self.assertIs(schematic.layout, preview)
                self.assertTrue(preserves_nets(circuit, preview))
                self.assertFalse(win.editor.document().isModified())

    def test_wire_drag_previews_a_perpendicular_slide_without_editing_source(self):
        win = self.window
        fixture = FIXTURES / "refactor" / "timer555.ckt"
        win.open_path(fixture)
        schematic = win.schematic
        source = win.editor.toPlainText()
        circuit = schematic.levels[None, "auto"]
        mode, records = read_placement(fixture)
        original = restore_layout(records[None, mode], circuit, ())
        self.assertIsNotNone(original)
        original = align_layout(circuit, original)
        self.assertIsNotNone(original)
        schematic.draw(schematic.circuit, layout=original)

        for vertical in (False, True):
            with self.subTest(vertical=vertical):
                schematic.reset_placement()
                circuit = schematic.levels[None, "auto"]
                edges = sorted(
                    ((a, b) for a, b in original.wires["supply"]
                     if (a[0] == b[0]) == vertical),
                    key=lambda edge: abs(edge[1][int(vertical)] - edge[0][int(vertical)]),
                    reverse=True,
                )
                move = next(((edge, delta) for edge in edges for delta in (0.5, -0.5)
                             if move_wire(circuit, original, "supply", edge, delta)[0] is not None), None)
                self.assertIsNotNone(move)
                (a, b), delta = move
                middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                start = schematic.mapFromScene(point(middle))
                scale = round(delta * schematic.transform().m11() * SCALE)
                end = start + QPoint(scale if vertical else 0, 0 if vertical else -scale)
                QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
                self.assertEqual(schematic.drag.kind, "wire")
                QTest.mouseMove(schematic.viewport(), end)
                self.wait_for_drag_preview(schematic, end)
                preview = schematic.layout
                self.assertIsNot(preview, original)
                self.assertEqual(preview.positions, original.positions)
                self.assertNotEqual(preview.wires["supply"], original.wires["supply"])
                axis = 0 if vertical else 1
                self.assertTrue(any(
                    p[axis] == q[axis] and abs(p[axis] - a[axis] - delta) < 0.15
                    for p, q in preview.wires["supply"]
                ))
                QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
                self.assertIs(schematic.layout, preview)
                self.assertTrue(preserves_nets(schematic.levels[None, "auto"], preview))
        self.assertEqual(win.editor.toPlainText(), source)
        self.assertFalse(win.editor.document().isModified())

    def test_shift_drag_can_use_shorter_diagonal_leads(self):
        win = self.window
        fixture = FIXTURES / "refactor" / "timer555.ckt"
        win.open_path(fixture)
        schematic = win.schematic
        mode, records = read_placement(fixture)
        original = restore_layout(records[None, mode], schematic.levels[None, "auto"], ())
        self.assertIsNotNone(original)
        schematic.draw(schematic.circuit, layout=original)
        _, a, b, _ = next(symbol for symbol in original.symbols if symbol[0].name == "RA")
        middle = point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        start = schematic.mapFromScene(middle)
        scale = round(schematic.transform().m11() * SCALE)
        end = start + QPoint(scale, -scale)
        shift = Qt.KeyboardModifier.ShiftModifier
        QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, shift, start)
        QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, shift, end)
        moved = schematic.layout
        self.assertTrue(any(
            p[0] != q[0] and p[1] != q[1]
            for net in ("supply", "discharge") for p, q in moved.wires[net]
        ))
        self.assertTrue(preserves_nets(schematic.levels[None, "auto"], moved))
        self.assertFalse(win.editor.document().isModified())

    def test_move_toward_a_wire_bend_shortens_the_route(self):
        win = self.window
        fixture = FIXTURES / "refactor" / "timer555.ckt"
        win.open_path(fixture)
        schematic = win.schematic
        mode, records = read_placement(fixture)
        circuit = schematic.levels[None, "auto"]
        original = restore_layout(records[None, mode], circuit, ())
        self.assertIsNotNone(original)
        schematic.draw(schematic.circuit, layout=original)
        _, a, b, _ = next(symbol for symbol in original.symbols if symbol[0].name == "RA")
        middle = point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        start = schematic.mapFromScene(middle)
        scale = round(schematic.transform().m11() * SCALE)
        end = start + QPoint(scale, 0)
        QTest.mousePress(schematic.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseRelease(schematic.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertLess(
            sum(dist(*segment) for wires in schematic.layout.wires.values() for segment in wires),
            sum(dist(*segment) for wires in original.wires.values() for segment in wires),
        )

    def test_right_click_rotates_part_and_keeps_its_pin_connections(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic
        source = win.editor.toPlainText()
        original = schematic.layout
        turns = original.rotations.get("RA", 0)
        _, a, b, _ = next(symbol for symbol in original.symbols if symbol[0].name == "RA")
        pos = schematic.mapFromScene(point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)))
        menu_event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, pos, schematic.viewport().mapToGlobal(pos)
        )
        with patch("circuitlab.viewer.schematic.QMenu") as menu:
            schematic.contextMenuEvent(menu_event)
            actions = dict(call.args for call in menu.return_value.addAction.call_args_list)
            self.assertEqual(list(actions), ["Edit value…", "Rotate clockwise", "Rotate counterclockwise", "Optimize wiring"])
            actions["Rotate counterclockwise"]()
            self.assertEqual(schematic.layout.rotations.get("RA", 0), (turns - 1) % 4)
            self.assertTrue(preserves_nets(schematic.levels[None, "auto"], schematic.layout))
            schematic.undo_placement()
            actions["Rotate clockwise"]()
        self.assertEqual(schematic.layout.rotations.get("RA", 0), (turns + 1) % 4)
        self.assertTrue(preserves_nets(schematic.levels[None, "auto"], schematic.layout))
        self.assertEqual(win.editor.toPlainText(), source)
        self.assertFalse(win.editor.document().isModified())
        schematic.reset_placement()
        self.assertIs(schematic.layout, original)

    def test_builtin_calls_are_purple_and_show_pin_order(self):
        editor = self.window.editor
        editor.setPlainText("transQP(plus, mirror, tail, npn);\nnor(vcc, 0, trg, idle);")
        editor.highlighter.rehighlight()
        for index, name, signature in (
            (0, "transQP", "transQP(base, collector, emitter"),
            (1, "nor", "nor(vcc, gnd, a"),
        ):
            block = editor.document().findBlockByNumber(index)
            cursor = QTextCursor(block)
            cursor.setPosition(block.position() + 1)
            self.assertIn(signature, editor.hover_help(cursor))
            self.assertTrue(any(
                item.start == 0 and item.length == len(name)
                and item.format.foreground().color().name() == "#e3b5f5"
                for item in block.layout().formats()
            ))

    def test_spice_button_exports_unsaved_editor_text_without_changing_source_file(
        self,
    ):
        win = self.window
        with TemporaryDirectory() as directory:
            path = Path(directory) / "example.ckt"
            path.write_text("V1 out 0 2\nR1 out 0 1k\n", encoding="ascii")
            win.open_path(path)
            win.editor.setPlainText("I1 = 10m : wire = out;\nR1 out 0 1k")
            win.editor.document().setModified(True)
            button = next(
                b
                for b in win.findChildren(QPushButton)
                if b.text() == "Export to SPICE"
            )
            with patch(
                "circuitlab.viewer.legacy.QFileDialog.getSaveFileName",
                return_value=(str(path.with_suffix("")), "SPICE netlists (*.cir)"),
            ):
                button.click()
            self.assertIn("I1 0 out DC 0.01", path.with_suffix(".cir").read_text())
            self.assertIn("V1 out 0 2", path.read_text())
            self.assertEqual(win.path, path)
            self.assertTrue(win.editor.document().isModified())

    def test_spice_export_reports_source_error_before_opening_save_dialog(self):
        win = self.window
        win.editor.setPlainText("V1 in 0 12\nR1 in 0 invalid")
        with (
            patch("circuitlab.viewer.legacy.QMessageBox.warning") as warning,
            patch("circuitlab.viewer.legacy.QFileDialog.getSaveFileName") as save_dialog,
        ):
            self.assertFalse(win.export_netlist())
        self.assertIn("Line 2", warning.call_args.args[2])
        self.assertIn("Line 2", win.issues.item(0).text())
        save_dialog.assert_not_called()

    def test_verilog_menu_exports_current_chip_source(self):
        win = self.window
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "chip.ckt"
            source = "chip Inverter(a, y) { a = input; y = output; y = a; }"
            source_path.write_text(source, encoding="ascii")
            win.open_path(source_path)
            self.assertEqual(win.status.text(), "Schematic rendered.")
            self.assertEqual(win.issues.item(0).text(), "No compiler diagnostics.")
            self.assertEqual([part.label for part, *_ in win.schematic.layout.symbols], ["Inverter"])

            win.editor.setPlainText(source.replace("y = a;", "y = !a;"))
            win.editor.document().setModified(True)
            export = next(
                action
                for menu in win.menuBar().actions()
                if menu.menu() is not None
                for action in menu.menu().actions()
                if "Export Verilog" in action.text().replace("&", "")
            )
            with (
                patch(
                    "circuitlab.viewer.legacy.QFileDialog.getSaveFileName",
                    return_value=(str(source_path.with_suffix("")), "Verilog (*.v)"),
                ) as save_dialog,
                patch("circuitlab.viewer.legacy.QMessageBox.warning") as warning,
            ):
                export.trigger()
            save_dialog.assert_called_once()
            warning.assert_not_called()
            self.assertIn("assign y = !a;", source_path.with_suffix(".v").read_text())
            self.assertEqual(source_path.read_text(), source)
            self.assertTrue(win.editor.document().isModified())

    def test_invalid_chip_reports_source_line_in_editor_and_export(self):
        win = self.window
        win.editor.setPlainText(
            "chip Inverter(a, y) {\n"
            "    a = input; y = output;\n"
            "    y = !missing;\n"
            "}"
        )
        win.compile_source()
        self.assertIn("Line 3", win.issues.item(0).text())
        win.select_diagnostic(win.issues.item(0))
        self.assertEqual(win.editor.textCursor().blockNumber(), 2)
        with (
            patch("circuitlab.viewer.legacy.QMessageBox.warning") as warning,
            patch("circuitlab.viewer.legacy.QFileDialog.getSaveFileName") as save_dialog,
        ):
            self.assertFalse(win.export_verilog())
        self.assertIn("Line 3", warning.call_args.args[2])
        save_dialog.assert_not_called()

    def test_packages_expand_and_collapse_on_the_same_schematic(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        schematic = win.schematic

        def click_package(label, action="Open", name=None):
            package = next(
                item for item in schematic.scene().items()
                if item.data(0) == "hood" and item.toolTip() == f"{action} {label}"
                and not hasattr(item, "text")
                and (name is None or item.data(1) == name)
            )
            pos = schematic.mapFromScene(package.sceneBoundingRect().center())
            QTest.mouseClick(schematic.viewport(), Qt.MouseButton.LeftButton, pos=pos)

        self.assertEqual(len(schematic.layout.symbols), 6)
        self.assertIn("control", schematic.anchors)
        outer_layout = schematic.layout
        self.assertFalse({"supply", "reset_n", "discharge", "timing"} & {
            net for net, *_ in outer_layout.connectors
        })
        click_package("timer555")
        click_package("srLatch")
        self.assertIs(schematic.layout, outer_layout)
        latch = next(key for key, (part, _) in schematic.packages.items() if part.label == "srLatch")
        latch_layout = schematic.level_layouts[latch, "auto"]
        self.assertEqual([part.label for part, *_ in latch_layout.symbols], ["nor", "nor"])
        gate = latch_layout.symbols[0][0]
        click_package(gate.label, name=gate.name)
        self.assertTrue(any(key == gate.name for key, _ in schematic.level_layouts))
        click_package("timer555", "Close")
        self.assertEqual(len(schematic.packages), 1)
        self.assertIs(schematic.layout, outer_layout)
        click_package("timer555")
        self.assertIs(schematic.level_layouts[latch, "auto"], latch_layout)

    def test_ltspice_button_exports_the_current_render(self):
        win = self.window
        win.editor.setPlainText("V1 = 12 : wire = in;\nR1 in out 2k\nR2 out 0 1k")
        win.render_source()
        drawing = win.schematic.layout
        button = next(
            b
            for b in win.findChildren(QPushButton)
            if b.text() == "Export LTspice .asc"
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "divider.asc"
            with patch(
                "circuitlab.viewer.legacy.QFileDialog.getSaveFileName",
                return_value=(str(path), "LTspice schematics (*.asc)"),
            ):
                button.click()
            self.assertIn("SYMBOL res ", path.read_text(encoding="ascii"))
            self.assertIs(win.schematic.layout, drawing)
            self.assertIsNone(win.path)

    def test_ltspice_export_saves_the_555_block_files_beside_the_main_sheet(self):
        win = self.window
        win.open_path(FIXTURES / "refactor" / "timer555.ckt")
        drawing = win.schematic.layout
        with TemporaryDirectory() as directory:
            path = Path(directory) / "timer555.asc"
            with patch(
                "circuitlab.viewer.legacy.QFileDialog.getSaveFileName",
                return_value=(str(path), "LTspice schematics (*.asc)"),
            ):
                self.assertTrue(win.export_ltspice())
                self.assertTrue(win.export_ltspice())
            content = path.read_text(encoding="ascii")
            name = next(row.split()[1] for row in content.splitlines() if row.startswith("SYMBOL ec_timer555_"))
            self.assertTrue(path.with_name(name + ".asc").is_file())
            self.assertTrue(path.with_name(name + ".asy").is_file())
            self.assertIs(win.schematic.layout, drawing)

    def test_editor_completes_unsaved_method_and_preserves_existing_arguments(self):
        editor = self.window.editor
        editor.setFocus()
        editor.setPlainText("main {\n    \n}\nseries R10K { R1 = 1k; }")
        cursor = editor.textCursor()
        cursor.setPosition(len("main {\n    "))
        editor.setTextCursor(cursor)
        QTest.keyClicks(editor, "r")
        popup = editor.completer.popup()
        self.assertTrue(popup.isVisible())
        self.assertEqual(popup.currentIndex().data(), "R10K")
        QTest.keyClick(popup, Qt.Key.Key_Tab)
        self.assertEqual(editor.textCursor().block().text(), "    R10K();")
        QTest.keyClicks(editor, "Vin, out")
        self.assertEqual(editor.textCursor().block().text(), "    R10K(Vin, out);")

        cursor = editor.textCursor()
        cursor.setPosition(len("main {\n    R"))
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(popup.isVisible())
        QTest.keyClick(popup, Qt.Key.Key_Tab)
        self.assertEqual(editor.textCursor().block().text(), "    R10K(Vin, out);")

    def test_editor_indents_nested_blocks_and_hovers_over_op_amp(self):
        editor = self.window.editor
        editor.setFocus()
        QTest.keyClicks(editor, "main {")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "parallel {")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "R1 = 1k;")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "}")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "op7(out, buffered, buffered);")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "}")
        self.assertEqual(
            editor.toPlainText(),
            "main {\n    parallel {\n        R1 = 1k;\n    }\n    op7(out, buffered, buffered);\n}",
        )
        cursor = QTextCursor(editor.document().findBlockByNumber(4))
        cursor.movePosition(
            QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, 5
        )
        pos = editor.cursorRect(cursor).center()
        QApplication.sendEvent(
            editor.viewport(),
            QHelpEvent(QEvent.Type.ToolTip, pos, editor.viewport().mapToGlobal(pos)),
        )
        self.assertIn("op7(in+, in-, out)", QToolTip.text())
        self.assertIn("negative feedback", QToolTip.text())
        QToolTip.hideText()
        original = editor.toPlainText()
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        self.assertEqual(
            editor.toPlainText(),
            "\n".join("    " + line for line in original.splitlines()),
        )
        QTest.keyClick(editor, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(editor.toPlainText(), original)

    def test_mixer_uses_routed_nets_and_mixed_solution(self):
        win = self.window
        win.open_path(FIXTURES / "three_band_mixer.ckt")
        self.assertEqual(len(win.solution.circuit.components), 63)
        self.assertEqual(set(win.schematic.paths), set(win.solution.circuit.nodes))
        self.assertEqual(win.solution.circuit.mode, "mixed")
        win.probe("node", "outP")
        self.assertIn("AC RMS", win.status.text())
        self.assertIn("DC", win.status.text())

    def test_unsolved_circuit_renders_and_reports_error(self):
        win = self.window
        win.editor.setPlainText("V1 a 0 1\nR1 x y 1k")
        win.compile_source()
        self.assertIsNone(win.solution)
        self.assertIn("floating", win.issues.item(0).text())
        self.assertEqual(set(win.schematic.paths), {"0", "a", "x", "y"})
        win.editor.setPlainText("V1 a 0 1\nR1 a 0 made-up")
        win.compile_source()
        win.select_diagnostic(win.issues.item(0))
        self.assertEqual(win.editor.textCursor().blockNumber(), 1)

    def test_split_ground_filter_renders_even_when_the_dc_return_floats(self):
        win = self.window
        win.editor.setPlainText("""
        battery V1 { Vpk = 3; f = 900; Vdc = 4; wire = in; ground = agnd; }
        main {
            series { R1 = 1k; }
            parallel {
                C1 = 1u;
                wire = a;
                ground = bgnd;
                R2 = 10k;
                ground = agnd;
            }
            op1(a, buffered, buffered);
            measure = V buffered agnd;
        }
        """)
        win.compile_source()
        self.assertIsNone(win.solution)
        self.assertIn("Floating ground return in DC: bgnd", win.issues.item(0).text())
        self.assertIn("agnd", win.schematic.anchors)
        self.assertIn("bgnd", win.schematic.anchors)
        self.assertLess(
            win.schematic.anchors["agnd"][0], win.schematic.anchors["bgnd"][0]
        )
        self.assertLess(
            win.schematic.anchors["bgnd"][0], win.schematic.anchors["buffered"][0]
        )
        win.render_source()
        self.assertEqual(win.status.text(), "Schematic rendered.")

    def test_preserves_source_and_file_encoding(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "legacy.ckt"
            original = "\ufeff# Legacy Ω\r\nV1 in 0 2\r\nR1 in 0 1k\r\n".encode()
            path.write_bytes(original)
            self.assertTrue(self.window.open_path(path))
            self.assertTrue(self.window.save())
            self.assertEqual(path.read_bytes(), original)
            editor = self.window.editor
            editor.moveCursor(editor.textCursor().MoveOperation.End)
            editor.insertPlainText("# additional comment\n")
            self.assertTrue(self.window.save())
            self.assertEqual(path.read_bytes(), original + b"# additional comment\r\n")

    def test_pin_placement_rename_search_and_reopen(self):
        win = self.window
        win.open_path(FIXTURES / "three_way_crossover.ckt")
        pos = win.schematic.mapFromScene(point(win.schematic.anchors["midOut"]))
        QTest.mouseClick(win.pin_button, Qt.MouseButton.LeftButton)
        with patch(
            "circuitlab.viewer.legacy.QInputDialog.getText", return_value=("Mid speaker", True)
        ):
            QTest.mouseClick(
                win.schematic.viewport(), Qt.MouseButton.LeftButton, pos=pos
            )
        self.assertEqual(win.pins, {"midOut": "Mid speaker"})
        self.assertIs(win.views.currentWidget(), win.results)
        self.assertEqual(win.results.search.text(), "Mid speaker")
        reading = next(
            row for row in win.results.rows if row[0] == "Mid speaker · AC voltage RMS"
        )
        self.assertIn("801.92", reading[2])
        self.assertTrue(
            any(
                "current away" in row[0]
                for row in win.results.rows
                if row[0].startswith("Mid speaker")
            )
        )

        with patch(
            "circuitlab.viewer.legacy.QInputDialog.getText", return_value=("My_Speaker", True)
        ):
            win.schematic.pin_renamed.emit("midOut")
        self.assertEqual(win.pins, {"midOut": "My_Speaker"})
        win.results.search.setText("my speaker")
        self.assertGreater(win.results.table.rowCount(), 5)
        win.wire_style.setCurrentIndex(win.wire_style.findData("labeled"))
        self.assertEqual(len(win.schematic.pin_items), 2)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "pins.ckt"
            self.assertTrue(win.write_path(path))
            self.assertTrue(win.open_path(path))
            self.assertEqual(win.pins, {"midOut": "My_Speaker"})
            self.assertEqual(len(win.schematic.pin_items), 2)
            win.schematic.pin_removed.emit("midOut")
            self.assertEqual(win.pins, {})
            self.assertFalse(win.schematic.pin_items)

    def test_mixed_pin_has_dc_ac_and_waveform_results(self):
        win = self.window
        win.open_path(FIXTURES / "three_band_mixer.ckt")
        win.write_pin("outP", "Speaker P")
        labels = {row[0] for row in win.results.rows}
        self.assertIn("Speaker P · DC voltage", labels)
        self.assertIn("Speaker P · AC voltage RMS", labels)
        self.assertIn("Speaker P · v(t)", labels)
