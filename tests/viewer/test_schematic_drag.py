"""Regressions for predictable grid movement without changing electrical nets."""
# Pointer movement proposes snapped valid geometry without changing the circuit graph.
# Invalid candidates, hidden grids, and expanded packages must recover predictably and remain
# undoable.

import os
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.language.syntax import parse_wires
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol, move_wire
from circuitlab.layout.geometry import Layout
from circuitlab.layout.hierarchy import hood_levels
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

    def test_hand_pan_can_center_top_left_and_fit_uses_drawing_bounds(self):
        view = self.view
        viewport = view.viewport()
        button = Qt.MouseButton.LeftButton
        center = viewport.rect().center()
        drawing = view.scene().itemsBoundingRect()
        fitted = view.transform().m11()
        self.assertGreater(view.mapFromScene(drawing).boundingRect().width(), .75 * viewport.width())

        start = QPoint(20, 20)
        top_left = drawing.topLeft()
        end = start + center - view.mapFromScene(top_left)
        self.assertIsNone(view.target(start))
        self.assertTrue(viewport.rect().contains(end))
        QTest.mousePress(viewport, button, pos=start)
        QTest.mouseMove(viewport, end)
        QTest.mouseRelease(viewport, button, pos=end)
        self.assertLessEqual((view.mapFromScene(top_left) - center).manhattanLength(), 2)
        self.assertFalse(view.fitted)

        cursor = viewport.rect().bottomRight() - QPoint(20, 20)
        QTest.mouseMove(viewport, cursor)
        fixed = view.mapToScene(cursor)
        view.zoom(1 / 1.22)
        self.assertLessEqual((view.mapFromScene(fixed) - cursor).manhattanLength(), 2)

        view.fit()
        self.assertAlmostEqual(view.transform().m11(), fitted)
        self.assertLessEqual((view.mapFromScene(drawing.center()) - center).manhattanLength(), 2)
        self.assertTrue(view.fitted)

    def test_boundary_drag_can_pass_another_terminal_on_the_same_column(self):
        r1, r2 = self.circuit.components
        first, neighbor = (-3, 0), (-3, 2)
        drawing = Layout(
            symbols=[(r1, (0, 0), (4, 0), "+"), (r2, (0, 2), (4, 2), "+")],
            positions={"R1": (0, 0), "R2": (0, 2)}, pin_grid=.25,
            boundary={"a": first, "c": neighbor},
            wires={"a": [(first, (0, 0))], "c": [(neighbor, (0, 2))]},
        )
        self.view.draw(self.circuit, layout=drawing)
        button = Qt.MouseButton.LeftButton
        QTest.mousePress(self.view.viewport(), button, pos=self.view.mapFromScene(point(first)))
        self.assertEqual(self.view.drag.kind, "boundary")
        QTest.mouseRelease(self.view.viewport(), button, pos=self.view.mapFromScene(point(neighbor)))
        moved = self.view.layout
        x, y = moved.boundary["a"]
        self.assertAlmostEqual(abs(x - neighbor[0]) + abs(y - neighbor[1]), .25)
        self.assertEqual(moved.boundary["c"], neighbor)
        self.assertEqual(moved.wires["c"], drawing.wires["c"])
        self.assertEqual(list(moved.pins()), list(drawing.pins()))
        self.assertEqual(moved.connectors, [])
        self.assertTrue(preserves_nets(self.circuit, moved))
        self.view.undo_placement()
        self.assertIs(self.view.layout, drawing)

    def test_powered_opamp_hit_area_is_the_triangle_with_its_own_selection_bounds(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,vee);", render_only=True)
        amp, = circuit.components
        drawing = Layout(symbols=[(amp, (0, 0), (4, 0), "+")],
                         positions={amp.name: (0, 0)}, pin_grid=.25)
        self.view.draw(circuit, layout=drawing)
        center = self.view.mapFromScene(point((1.9, 0)))
        outside = self.view.mapFromScene(point((3.5, .7)))
        self.assertEqual(self.view.target(center), ("component", amp.name))
        self.assertIsNone(self.view.target(outside))
        self.view.selected_symbols = {amp.name}
        self.view.selection_key = self.key
        self.view.paint_selection()
        outline, = self.view.selection_items
        self.assertLess(outline.rect().height(), 2 * SCALE)
        self.assertLess(outline.rect().width(), 2.1 * SCALE)

    def test_wire_drag_keeps_a_grid_square_around_a_chip(self):
        circuit = compile_circuit(
            "chip Block(a, b) { Rinside(1k) between(a, b); }\n"
            "main { Block(left, right); Rload(1k) between(feed, out); }",
            render_only=True,
        )
        _, root, _, _ = next(hood_levels(circuit, set()))
        package = next(part for part in root.components if part.kind == "H")
        load = next(part for part in root.components if part.kind == "R")
        wire = (-3, 1.25), (8, 1.25)
        drawing = Layout(
            symbols=[(package, (0, 0), (6.5, 0), "+"), (load, (8, 1.25), (10, 1.25), "+")],
            positions={package.name: (0, 0), load.name: (8, 1.25)}, pin_grid=.25,
            boundary={"feed": wire[0]}, wires={"feed": [wire]},
        )
        self.view.draw(circuit, layout=drawing)
        self.start_pos = self.view.mapFromScene(point((3, 1.25)))
        self.view.start_drag("wire", "feed", self.key, self.start_pos, edge=wire)
        self.move(0, -.25)
        self.assertTrue(self.view.drag.blocked)
        self.assertIs(self.view.layout, drawing)
        self.move(0, .25)
        self.assertFalse(self.view.drag.blocked)
        self.assertIsNot(self.view.layout, drawing)

    def test_component_drag_carries_only_its_own_ground_and_undoes_together(self):
        circuit = compile_circuit('V1(5) between(a,0);\nR1(1k) between(b,0);', render_only=True)
        button = Qt.MouseButton.LeftButton
        for marker in ((0, -3), (0, -2.5)):
            with self.subTest(marker=marker):
                v1, r1 = circuit.components
                local_lead = [((0, -2.5), marker)] if marker != (0, -2.5) else []
                drawing = Layout(
                    symbols=[(v1, (0, 0), (0, -2.5), '+'), (r1, (8, 0), (12, 0), '+')],
                    positions={'V1': (0, 0), 'R1': (8, 0)}, grounds=[marker, (13, 0)],
                    wires={'0': local_lead + [((12, 0), (13, 0))]},
                    ground_rotations={marker: 1}, label_offsets={('ground', *marker): (.25, .25)},
                    pin_grid=.25,
                )
                self.view.clear()
                self.view.draw(circuit, layout=drawing)
                self.start_pos = self.view.mapFromScene(point((0, -1.25)))
                self.view.start_drag('component', 'V1', self.key, self.start_pos)
                end = self.move(2, -2)
                self.assertFalse(self.view.drag.blocked)
                preview = self.view.layout
                dx, dy = preview.positions['V1']
                self.assertNotEqual((dx, dy), (0, 0))
                moved_marker = marker[0] + dx, marker[1] + dy
                self.assertEqual(preview.grounds, [moved_marker, (13, 0)])
                self.assertEqual(preview.ground_rotations, {moved_marker: 1})
                self.assertEqual(preview.label_offsets[('ground', *moved_marker)], (.25, .25))
                self.assertEqual(preview.positions['R1'], (8, 0))
                self.assertTrue(preserves_nets(circuit, preview))
                QTest.mouseRelease(self.view.viewport(), button, pos=end)
                self.assertIs(self.view.layout, preview)

                self.start_pos = self.view.mapFromScene(point(moved_marker))
                self.view.start_drag('ground', '0', self.key, self.start_pos, terminal=moved_marker)
                end = self.move(0, -1)
                self.assertFalse(self.view.drag.blocked)
                self.assertNotEqual(self.view.layout.grounds[0], moved_marker)
                self.assertEqual(self.view.layout.positions['V1'], preview.positions['V1'])
                self.assertTrue(preserves_nets(circuit, self.view.layout))
                QTest.mouseRelease(self.view.viewport(), button, pos=end)
                self.view.undo_placement()
                self.assertIs(self.view.layout, preview)
                self.view.undo_placement()
                self.assertIs(self.view.layout, drawing)

    def test_shared_ground_moves_only_with_its_entire_selected_group(self):
        circuit = compile_circuit(
            'R1(1k) between(a,0);\nR2(2k) between(b,0);\nR3(3k) between(c,0);',
            render_only=True,
        )
        r1, r2, r3 = circuit.components
        ground = (4, -2)
        separate = (13, 0)
        drawing = Layout(
            symbols=[(r1, (0, 0), (4, 0), '+'), (r2, (0, 6), (4, 6), '+'), (r3, (8, 0), (12, 0), '+')],
            positions={'R1': (0, 0), 'R2': (0, 6), 'R3': (8, 0)}, grounds=[ground, separate],
            wires={'0': [(ground, (4, 0)), ((4, 0), (4, 6)), ((12, 0), separate)]}, pin_grid=.25,
        )
        self.view.draw(circuit, layout=drawing)
        self.start_pos = self.view.mapFromScene(point((2, 0)))
        self.view.start_drag('component', 'R1', self.key, self.start_pos)
        end = self.move(0, -1)
        self.assertFalse(self.view.drag.blocked)
        self.assertEqual(self.view.layout.grounds, [ground, separate])
        self.assertTrue(preserves_nets(circuit, self.view.layout))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.view.undo_placement()

        self.view.selection_key = self.key
        self.view.selected_symbols = {'R1', 'R2'}
        self.start_pos = self.view.mapFromScene(point((2, 0)))
        self.view.start_drag('component', 'R1', self.key, self.start_pos)
        end = self.move(0, -1)
        self.assertFalse(self.view.drag.blocked)
        dx, dy = self.view.layout.positions['R1']
        self.assertNotEqual((dx, dy), (0, 0))
        self.assertEqual(self.view.layout.grounds, [(ground[0] + dx, ground[1] + dy), separate])
        self.assertEqual(self.view.layout.positions['R2'], (dx, 6 + dy))
        self.assertEqual(self.view.layout.positions['R3'], (8, 0))
        self.assertTrue(preserves_nets(circuit, self.view.layout))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.view.undo_placement()
        self.assertIs(self.view.layout, drawing)

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

    def test_wire_slide_moves_a_full_row_through_close_tees_without_jumping_a_gap(self):
        circuit = compile_circuit(
            "Rleft(1k) between(a,d);\nRtee1(1k) between(a,b);\n"
            "Rtee2(1k) between(a,c);\nRisland(1k) between(a,e);",
            render_only=True,
        )
        left, first, second, island = circuit.components
        symbols = [
            (left, (0, -2), (0, -5), "+"),
            (first, (5, 4), (5, 7), "+"),
            (second, (5.25, 2), (9.25, 2), "+"),
            (island, (12, -2), (12, -5), "+"),
        ]
        clicked = ((0, 0), (5, 0))
        separate = [((12, -2), (12, 0)), ((12, 0), (14, 0))]
        drawing = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=.25,
            connectors=[("a", (10, 0), "a", True, False), ("a", (14, 0), "a", True, False)],
            wires={"a": [((0, -2), (0, 0)), clicked, ((5, 0), (5.25, 0)),
                          ((5.25, 0), (10, 0)), ((5, 0), (5, 4)), ((5.25, 0), (5.25, 2)),
                          *separate]},
        )
        self.view.draw(circuit, layout=drawing)
        self.view.start_drag("wire", "a", self.key, self.view.mapFromScene(point((2, 0))), clicked)
        end = self.view.mapFromScene(point((2, -1)))
        self.view.preview_drag(end)
        self.assertFalse(self.view.drag.blocked)
        preview = self.view.layout
        self.assertEqual(preview.symbols, symbols)
        self.assertEqual(preview.connectors, drawing.connectors)
        self.assertIn(((5, -1), (5.25, -1)), preview.wires["a"])
        self.assertIn(((5.25, -1), (10, -1)), preview.wires["a"])
        self.assertEqual([edge for edge in preview.wires["a"] if edge[0][0] >= 12], separate)
        self.assertNotIn(((5, 0), (5.25, 0)), preview.wires["a"])
        self.assertTrue(preserves_nets(circuit, preview))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.view.undo_placement()
        self.assertEqual(self.view.layout, drawing)

    def test_wire_slide_folds_back_an_unanchored_return_bend(self):
        circuit = compile_circuit("Rleft(1k) between(a,d);\nRright(1k) between(a,b);", render_only=True)
        left, right = circuit.components
        clicked = ((0, 0), (10, 0))
        drawing = Layout(
            symbols=[(left, (0, -2), (0, -5), "+"), (right, (4, 2), (4, 5), "+")],
            positions={"Rleft": (0, -2), "Rright": (4, 2)}, pin_grid=.25,
            wires={"a": [((0, -2), (0, 0)), clicked, ((10, 0), (10, 2)), ((4, 2), (10, 2))]},
        )
        moved, _ = move_wire(circuit, drawing, "a", clicked, -1)
        self.assertTrue(preserves_nets(circuit, moved))
        self.assertEqual(moved.symbols, drawing.symbols)
        self.assertEqual(moved.wires["a"], [((0, -2), (0, -1)), ((0, -1), (4, -1)), ((4, -1), (4, 2))])

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

    def test_opamp_context_menu_flips_only_inputs_and_preserves_wiring(self):
        for source in ('op1(plus,minus,out);', 'op1(plus,minus,out,vcc,vee);',
                       'op1(plus,minus,out,vcc,vee,gain1,gain8,bypass);'):
            with self.subTest(source=source):
                circuit = compile_circuit(source, render_only=True)
                amp = circuit.components[0]
                drawing = Layout(symbols=[(amp, (0, 0), (4, 0), '+')],
                                 positions={amp.name: (0, 0)}, pin_grid=.25)
                pins = dict(drawing.pins())
                drawing.boundary = {
                    net: ((-4, y) if net in {'plus', 'minus'} else (7, y) if net == 'out' else (x, 2 * y))
                    for net, (x, y) in pins.items()
                }
                drawing.wires = {net: [(pin, drawing.boundary[net])] for net, pin in pins.items()}
                self.view.draw(circuit, layout=drawing)
                at = self.view.mapFromScene(point((2, 0)))
                action_text = 'Flip +/− inputs'
                actions = []

                def choose_flip():
                    menu = self.app.activePopupWidget()
                    actions.extend(action.text() for action in menu.actions())
                    action = next((action for action in menu.actions() if action.text() == action_text), None)
                    if action is not None:
                        action.trigger()
                    menu.close()

                QTimer.singleShot(0, choose_flip)
                self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                                            at, self.view.mapToGlobal(at)))
                self.assertIn(action_text, actions)
                flipped = self.view.layout
                self.assertEqual(flipped.positions, drawing.positions)
                self.assertEqual(dict(flipped.pins()), {**pins, 'plus': pins['minus'], 'minus': pins['plus']})
                for net in pins.keys() - {'plus', 'minus'}:
                    self.assertEqual(flipped.wires[net], drawing.wires[net])
                self.assertTrue(preserves_nets(circuit, flipped))
                self.assertEqual(restore_layout(pack_layout(flipped), circuit, tuple(drawing.boundary)), flipped)
                self.view.undo_placement()
                self.assertEqual(self.view.layout, drawing)

    def test_chip_drag_carries_feedback_and_side_passives_and_alt_moves_it_alone(self):
        path = Path(__file__).resolve().parents[2] / 'examples/advanced/synth/HeadphoneJack_DaisySeed3.ckt'
        # Keep this drag fixture independent of edits to the interactive demo.
        source = '''
        include("LT1677");
        f = 1k;
        Vpos(5V) between(vcc, 0);
        Vneg(-5V) between(vee, 0);
        Vleft(0.1 Vrms) between(audio_left, 0);
        Vright(0.1 Vrms) between(audio_right, 0);
        circuit AudioChannel(input, output, positive, negative, ground) {
            Cinput(2.2uF) between(input, coupled);
            Rin(10k) between(coupled, inverting);
            Rfeedback(10k) between(output, inverting);
            Cfeedback(47pF) between(output, inverting);
            LT1677(trim1, inverting, ground, negative, nc5, output, positive, trim8);
            Cpositive(100nF) between(positive, ground);
            Cnegative(100nF) between(negative, ground);
        }
        main {
            AudioChannel(audio_left, preamp_left, vcc, vee, 0);
            AudioChannel(audio_right, preamp_right, vcc, vee, 0);
            RloadLeft(10k) between(preamp_left, 0);
            RloadRight(10k) between(preamp_right, 0);
        }
        '''
        circuit = compile_circuit(source, path=path, render_only=True)
        view = self.view
        view.draw(circuit)
        original = view.layout
        chip, a, b, _ = next(symbol for symbol in original.symbols if symbol[0].name == '@method:1')
        center = (a[0] + b[0]) / 2, a[1]
        stage = {chip.name, 'Rfeedback_AudioChannel_1', 'Cfeedback_AudioChannel_1',
                 'Rin_AudioChannel_1', 'Cinput_AudioChannel_1', 'RloadLeft'}
        start = view.mapFromScene(point(center))
        end = view.mapFromScene(point((center[0], center[1] + 1)))

        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(view.drag.stage_group, stage)
        view.preview_drag(end)
        self.assertFalse(view.drag.blocked)
        self.assertTrue(view.selection_items)
        preview = view.layout
        for name, (x, y) in original.positions.items():
            self.assertEqual(preview.positions[name], (x, y + 1) if name in stage else (x, y))
        for net in ('AudioChannel_1.inverting', 'preamp_left'):
            self.assertEqual(preview.wires[net], [((x, y + 1), (u, v + 1))
                                                  for (x, y), (u, v) in original.wires[net]])
        self.assertTrue(preserves_nets(view.levels[self.key], preview))
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        view.undo_placement()
        self.assertEqual(view.layout, original)

        view.selection_key = self.key
        view.selected_symbols.update(stage)
        start = view.mapFromScene(point(center))
        end = view.mapFromScene(point((center[0] + .5, center[1] - .25)))
        alt = Qt.KeyboardModifier.AltModifier
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, alt, start)
        self.assertFalse(view.drag.stage_group or view.drag.selection or view.drag.attached_grounds)
        view.preview_drag(end)
        self.assertFalse(view.drag.blocked)
        preview = view.layout
        self.assertEqual(preview.positions, {**original.positions, chip.name: (a[0] + .5, a[1] - .25)})
        self.assertEqual(preview.grounds, original.grounds)
        self.assertTrue(preserves_nets(view.levels[self.key], preview))
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, alt, end)
        view.undo_placement()
        self.assertEqual(view.layout, original)

        resistor = 'Rfeedback_AudioChannel_1'
        _, a, b, _ = next(symbol for symbol in original.symbols if symbol[0].name == resistor)
        center = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        start = view.mapFromScene(point(center))
        end = view.mapFromScene(point((center[0] + .5, center[1])))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertFalse(view.drag.stage_group)
        view.preview_drag(end)
        self.assertFalse(view.drag.blocked)
        self.assertEqual(view.layout.positions, {**original.positions, resistor: (a[0] + .5, a[1])})
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


class WireParsingTypesTest(unittest.TestCase):
    def test_single_wire_is_required_before_indexing(self):
        self.assertEqual(parse_wires("output", 1), ("output",))
        self.assertEqual(parse_wires("", 0), ())
        for invalid in ("", "a,b", "a,,b"):
            with self.subTest(value=invalid), self.assertRaises(CircuitError):
                parse_wires(invalid, 1)


if __name__ == "__main__":
    unittest.main()
