"""Regressions for predictable grid movement without changing electrical nets."""
# Pointer movement proposes snapped valid geometry without changing the circuit graph.
# Invalid candidates, hidden grids, and expanded packages must recover predictably and remain
# undoable.

import os
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.language.syntax import parse_wires
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.model.graph import CircuitError
from circuitlab.viewer.schematic import SCALE, SchematicView, point
from circuitlab.viewer.snapping import grid_step


class SchematicDragTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit("R1(1k) between(a,b);\nR2(2k) between(c,d);", render_only=True)
        r1, r2 = self.circuit.components
        self.original = Layout(
            symbols=[(r1, (0, 0), (4, 0), "+"), (r2, (8, 3), (12, 3), "+")],
            positions={"R1": (0, 0), "R2": (8, 3)},
            boundary={"a": (-3, 0), "b": (6, 0), "c": (8, 3), "d": (12, 3)},
            wires={"a": [((-3, 0), (0, 0))], "b": [((4, 0), (6, 0))]},
        )
        self.view = SchematicView()
        self.view.resize(800, 500)
        self.view.show()
        self.view.draw(self.circuit, layout=self.original)
        self.app.processEvents()
        self.key = None, "auto"

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def start(self):
        self.start_pos = self.view.mapFromScene(point((2, 0)))
        self.view.start_drag("component", "R1", self.key, self.start_pos)

    def move(self, dx, dy):
        end = self.start_pos + self.view.mapFromScene(point((dx, dy))) - self.view.mapFromScene(point((0, 0)))
        self.view.preview_drag(end)
        return end

    def test_split_end_drag_joins_wire_interior_and_undo_restores_markers(self):
        circuit = compile_circuit('R1(1k) between(a,n);\nR2(2k) between(n,b);', render_only=True)
        r1, r2 = circuit.components
        drawing = Layout(
            symbols=[(r1, (0, 0), (4, 0), '+'), (r2, (0, 4), (-4, 4), '+')],
            positions={'R1': (0, 0), 'R2': (0, 4)},
            wires={'n': [((4, 0), (6, 0)), ((0, 4), (4, 4))], 'a': [((-4, 0), (0, 0))]},
            connectors=[('n', (6, 0), 'n', False, False), ('n', (4, 4), 'n', False, False)],
        )
        self.view.draw(circuit, layout=drawing)
        self.view.start_drag('connector', 'n', self.key, self.view.mapFromScene(point((6, 0))), terminal=0)
        end = self.view.mapFromScene(point((2, 4)))
        self.view.preview_drag(end)
        self.assertFalse(self.view.drag.blocked)
        self.assertEqual(self.view.layout.connectors, [])
        self.assertTrue(preserves_nets(circuit, self.view.layout))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(self.view.layout.connectors, [])
        self.view.undo_placement()
        self.assertEqual(self.view.layout.connectors, drawing.connectors)
        # The input wire is before R1: dragging here must never bypass R1.
        self.view.start_drag('connector', 'n', self.key, self.view.mapFromScene(point((6, 0))), terminal=0)
        self.view.preview_drag(self.view.mapFromScene(point((-2, 0))))
        self.assertTrue(self.view.layout.connectors)
        self.assertTrue(preserves_nets(circuit, self.view.layout))

    def test_ground_wire_context_menu_splits_into_symbols_and_can_undo(self):
        circuit = compile_circuit('R1(1k) between(a,0);\nR2(2k) between(b,0);', render_only=True)
        r1, r2 = circuit.components
        drawing = Layout(
            symbols=[(r1, (0, 0), (4, 0), '+'), (r2, (0, 6), (4, 6), '+')],
            positions={'R1': (0, 0), 'R2': (0, 6)},
            wires={'0': [((4, 0), (4, 6)), ((4, -2), (4, 0))]}, grounds=[(4, -2)],
        )
        self.view.draw(circuit, layout=drawing)
        at = self.view.mapFromScene(point((4, 3)))
        actions = []

        def choose_split():
            menu = self.app.activePopupWidget()
            actions.extend(action.text() for action in menu.actions())
            for action in menu.actions():
                if action.text() == 'Split':
                    action.trigger()
                    break
            menu.close()

        QTimer.singleShot(0, choose_split)
        self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, at, self.view.mapToGlobal(at)))
        self.assertIn('Split', actions)
        self.assertEqual(self.view.layout.connectors, [])
        self.assertGreaterEqual(len(self.view.layout.grounds), 2)
        self.assertTrue(preserves_nets(circuit, self.view.layout))
        self.view.undo_placement()
        self.assertEqual(self.view.layout.grounds, drawing.grounds)
        self.assertEqual(self.view.layout.wires, drawing.wires)

    def test_small_moves_follow_each_grid_row_and_keep_the_view_still(self):
        self.start()
        scale = self.view.transform().m11()
        stationary = self.view.mapFromScene(point((8, 3)))
        step = self.view.drag.grid_step
        previous = self.original.positions["R1"]
        for row in range(1, 5):
            self.move(0, -row * step)
            moved = self.view.layout
            self.assertFalse(self.view.drag.blocked)
            self.assertNotEqual(moved.positions["R1"], previous)
            self.assertAlmostEqual(moved.positions["R1"][1], -row * step)
            self.assertEqual(moved.positions["R2"], self.original.positions["R2"])
            self.assertTrue(preserves_nets(self.circuit, moved))
            previous = moved.positions["R1"]
        end = self.move(0, -5 * step)
        preview = self.view.layout
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertIs(self.view.layout, preview)
        self.assertAlmostEqual(self.view.transform().m11(), scale)
        self.assertLessEqual((self.view.mapFromScene(point((8, 3))) - stationary).manhattanLength(), 2)
        self.view.undo_placement()
        self.assertIs(self.view.layout, self.original)

    def test_invalid_move_keeps_last_valid_preview_and_recovers(self):
        self.start()
        self.move(0, -1)
        accepted = self.view.layout
        self.move(8, 3)  # Directly on R2's body.
        self.assertTrue(self.view.drag.blocked)
        self.assertIs(self.view.layout, accepted)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))
        self.move(0, -2)
        self.assertFalse(self.view.drag.blocked)
        self.assertNotEqual(self.view.layout.positions["R1"], accepted.positions["R1"])
        accepted = self.view.layout
        end = self.move(8, 3)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertIs(self.view.layout, accepted)
        self.view.undo_placement()
        self.assertIs(self.view.layout, self.original)

    def test_alt_and_hidden_grid_still_snap_and_grid_moves_persist(self):
        for visible in (True, False):
            self.view.set_grid_enabled(visible)
            self.start()
            end = self.start_pos + QPoint(19, 17)
            step = self.view.drag.grid_step
            QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.AltModifier, end)
            for value in self.view.layout.positions["R1"]:
                self.assertAlmostEqual(value / step, round(value / step))
            self.assertTrue(preserves_nets(self.circuit, self.view.layout))
            saved = pack_layout(self.view.layout)
            restored = restore_layout(saved, self.circuit, tuple(self.original.boundary))
            self.assertIsNotNone(restored)
            self.assertEqual(restored.positions, self.view.layout.positions)
            self.assertTrue(preserves_nets(self.circuit, restored))
            self.view.undo_placement()

    def test_connected_off_grid_pin_cannot_pull_a_component_off_grid(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nR2(2k) between(b,d);", render_only=True)
        r1, r2 = circuit.components
        drawing = Layout(
            symbols=[(r1, (0, 0), (4, 0), "+"), (r2, (8.1, 3.1), (12.1, 3.1), "+")],
            positions={"R1": (0, 0), "R2": (8.1, 3.1)},
            wires={"b": [((4,0), (6,0)), ((6,0), (6,3.1)), ((6,3.1), (8.1,3.1))]},
        )
        self.view.draw(circuit, layout=drawing)
        self.start()
        step = self.view.drag.grid_step
        end = self.move(4.1, 3.1)
        self.assertIsNone(self.view.drag.snapped_terminal)
        self.assertFalse(self.view.drag.blocked)
        for value in self.view.layout.positions["R1"]:
            self.assertAlmostEqual(value / step, round(value / step))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertTrue(preserves_nets(circuit, self.view.layout))

    def test_wire_magnet_cannot_snap_to_an_off_grid_row(self):
        # Stay clear of the half-grid threshold: converting scene coordinates
        # to integer mouse pixels can push .12 across .125 at some zoom levels.
        off_grid = .08
        drawing = replace(self.original,
            boundary={**self.original.boundary, "a": (-3, off_grid)},
            wires={**self.original.wires, "a": [((-3,off_grid),(-3,0)), ((-3,0),(0,0))]})
        self.view.draw(self.circuit, layout=drawing)
        start = self.view.mapFromScene(point((-1.5, 0)))
        self.view.start_drag("wire", "a", self.key, start, ((-3,0),(0,0)))
        end = start + self.view.mapFromScene(point((0,off_grid))) - self.view.mapFromScene(point((0,0)))
        self.view.preview_drag(end)
        self.assertIsNone(self.view.drag.snapped_offset)
        self.assertEqual(self.view.layout.wires, drawing.wires)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_wire_slides_grid_rows_without_moving_its_terminals(self):
        edge = self.original.wires["a"][0]
        start = self.view.mapFromScene(point((-1.5, 0)))
        self.view.start_drag("wire", "a", self.key, start, edge)
        step = self.view.drag.grid_step
        for row in range(1, 4):
            end = start + QPoint(31, round(row * step * SCALE * self.view.transform().m11()))
            self.view.preview_drag(end)
            self.assertFalse(self.view.drag.blocked)
            self.assertEqual(self.view.layout.positions, self.original.positions)
            self.assertTrue(any(a[1] == b[1] == -row * step for a, b in self.view.layout.wires["a"]))
            self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_clear_bodies_can_move_even_when_lead_bounds_overlap(self):
        # Horizontal leads overlap in x, but the component bodies are separate.
        moved = move_symbol(self.circuit, self.original, "R1", (3.5, 3))
        self.assertIsNotNone(moved)
        self.assertEqual(moved.positions["R1"], (3.5, 3))
        self.assertTrue(preserves_nets(self.circuit, moved))
        self.assertIsNone(move_symbol(self.circuit, self.original, "R1", (8, 3)))

    def test_expanded_chip_keeps_its_contents_anchored_during_and_after_a_drag(self):
        circuit = compile_circuit('chip Test(a,b) { R1(1k) between(a,b); }\nmain { Test(a,b); }', render_only=True)
        view = self.view
        view.draw(circuit)
        view.toggle_package("@method:0")
        key = "@method:0", "auto"
        drawing = view.level_layouts[key]
        offset = view.level_offsets[key]
        part, a, b, _ = drawing.symbols[0]
        start = view.mapFromScene(point(tuple((x + y) / 2 for x, y in zip(a, b))) + offset)
        view.start_drag("component", part.name, key, start)
        end = start + QPoint(30, 40)
        view.preview_drag(end)
        self.assertFalse(view.drag.blocked)
        self.assertEqual(view.level_offsets[key], offset)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(view.level_offsets[key], offset)
        self.assertTrue(preserves_nets(view.levels[key], view.manual_layouts[key]))


class WireParsingTypesTest(unittest.TestCase):
    def test_single_wire_is_required_before_indexing(self):
        self.assertEqual(parse_wires("output", 1), ("output",))
        self.assertEqual(parse_wires("", 0), ())
        for invalid in ("", "a,b", "a,,b"):
            with self.subTest(value=invalid), self.assertRaises(CircuitError):
                parse_wires(invalid, 1)


if __name__ == "__main__":
    unittest.main()
