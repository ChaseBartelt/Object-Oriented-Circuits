"""Perfboard wires can be repaired independently of physical part placement."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout
from circuitlab.layout.wire_edit import physical_wire_islands
from circuitlab.viewer.schematic import SchematicView, point


SOURCE = "perf[A-J,1-16]; R1(1k) between(a,b); R2(2k) between(b,c);"


class PerfboardWireEditingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        r1, r2 = self.circuit.components
        self.original = Layout(
            symbols=[(r1, (2, -2), (4, -2), "+"), (r2, (8, -2), (10, -2), "+")],
            positions={"R1": (2, -2), "R2": (8, -2)}, pin_grid=1,
            wires={"b": [((4, -2), (4.5, -2)), ((4.5, -2), (4.5, -5)),
                          ((4.5, -5), (5, -5)), ((5, -5), (5, -8)),
                          ((5, -8), (8, -8)), ((8, -8), (8, -2))]},
        )
        self.assertTrue(preserves_nets(self.circuit, self.original))
        self.view = SchematicView()
        self.view.resize(1000, 750)
        self.view.show()
        self.view.draw(self.circuit, layout=self.original)
        self.view.zoom(.6)
        self.app.processEvents()
        self.key = None, "auto"

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def choose(self, label):
        found = []

        def trigger():
            menu = self.app.activePopupWidget()
            found.extend(action.text() for action in menu.actions())
            action = next((action for action in menu.actions() if action.text() == label), None)
            if action:
                action.trigger()
            menu.close()

        position = self.view.viewport().rect().bottomRight()
        QTimer.singleShot(0, trigger)
        self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position,
                                                   self.view.viewport().mapToGlobal(position)))
        self.assertIn(label, found)

    def split(self):
        self.view.split_connection(self.key, "b", ((5, -8), (8, -8)), (6.5, -8))
        self.assertEqual(len(self.view.layout.connectors), 2)
        return self.view.layout

    def test_mouse_drag_straightens_a_half_hole_jog(self):
        start = self.view.mapFromScene(point((5, -6.5)))
        end = self.view.mapFromScene(point((4.5, -6.5)))
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(self.view.drag.kind, "wire")
        self.assertEqual(self.view.drag.grid_step, .5)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertIn(((4.5, -8), (4.5, -2)), self.view.layout.wires["b"])
        self.assertEqual(self.view.layout.symbols, self.original.symbols)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))
        self.view.undo()
        self.assertEqual(self.view.layout.wires, self.original.wires)

    def test_wire_can_move_off_the_board(self):
        edge = (5, -8), (8, -8)
        start = self.view.mapFromScene(point((6.5, -8)))
        end = self.view.mapFromScene(point((6.5, -12)))
        self.view.start_drag("wire", "b", self.key, start, edge)
        self.view.preview_drag(end)
        self.assertFalse(self.view.drag.blocked)
        self.assertTrue(any(a[1] == b[1] == -12 for a, b in self.view.layout.wires["b"]))
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_split_ends_move_independently_outside_the_board(self):
        split = self.split()
        _, islands = physical_wire_islands(split, "b")
        opposite = islands[(8, -2)]
        stationary = [edge for edge in split.wires["b"] if islands[edge[0]] == opposite]
        first, second = split.connectors
        start = self.view.mapFromScene(point(first[1]))
        end = self.view.mapFromScene(point((first[1][0] - 3, first[1][1] - 3)))
        self.view.start_drag("connector", "b", self.key, start, terminal=0)
        self.assertEqual(self.view.drag.grid_step, .5)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertNotEqual(self.view.layout.connectors[0][1], first[1])
        self.assertEqual(self.view.layout.connectors[1], second)
        self.assertTrue(all(edge in self.view.layout.wires["b"] for edge in stationary))
        _, moved_islands = physical_wire_islands(self.view.layout, "b")
        self.assertNotEqual(moved_islands[(4, -2)], moved_islands[(8, -2)])
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_rotation_does_not_reroute_the_whole_board(self):
        with patch("circuitlab.viewer.schematic.reroute_perfboard", side_effect=AssertionError("global reroute")):
            self.view.rotate_component(self.key, "R1")
        self.assertEqual(self.view.layout.symbols[1], self.original.symbols[1])
        self.assertEqual(self.view.layout.positions["R2"], self.original.positions["R2"])
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_remove_arrange_and_add_wires_keeps_placement_and_supports_undo(self):
        self.choose("Remove all wires")
        self.assertFalse(self.view.layout.wires_visible)
        self.assertFalse(any(item.data(0) == "wire" for item in self.view.scene().items()))
        start = self.view.mapFromScene(point((3, -2)))
        end = self.view.mapFromScene(point((3, -5)))
        self.view.start_drag("component", "R1", self.key, start, only_symbol=True)
        with patch("circuitlab.viewer.schematic.move_perfboard_parts", side_effect=AssertionError("routing while placing")):
            QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        with patch("circuitlab.viewer.schematic.reroute_perfboard", side_effect=AssertionError("routing while placing")):
            self.view.rotate_component(self.key, "R1")
        positions = dict(self.view.layout.positions)
        rotations = dict(self.view.layout.rotations)
        self.choose("Add wires")
        self.assertTrue(self.view.layout.wires_visible)
        self.assertEqual(self.view.layout.positions, positions)
        self.assertEqual(self.view.layout.rotations, rotations)
        self.assertTrue(any(item.data(0) == "wire" for item in self.view.scene().items()))
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))
        self.view.undo()
        self.assertFalse(self.view.layout.wires_visible)
        self.assertEqual(self.view.layout.positions, positions)

    def test_hidden_placement_reopens_and_adds_wires_afterwards(self):
        self.view.set_wires_visible(False)
        self.view.rotate_component(self.key, "R1")
        positions = dict(self.view.layout.positions)
        saved = pack_layout(self.view.layout)
        self.view.clear()
        self.view.placement_records[self.key] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assertFalse(self.view.layout.wires_visible)
        self.assertEqual(self.view.layout.positions, positions)
        self.view.set_wires_visible(True)
        self.assertEqual(self.view.layout.positions, positions)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_placement_mode_selection_does_not_include_removed_wires(self):
        self.view.set_wires_visible(False)
        self.view.selection_key = self.key
        self.view.selected_symbols = {"R1", "R2"}
        self.view.paint_selection()
        outline, = self.view.selection_items
        self.assertLess(outline.rect().bottom(), point((0, -4)).y())
        self.assertIsNone(self.view.nearest_wire(self.view.mapFromScene(point((5, -7)))))

    def test_add_wires_preserves_independent_split_ends(self):
        split = self.split()
        self.view.set_wires_visible(False)
        self.view.rotate_component(self.key, "R1")
        self.view.set_wires_visible(True)
        self.assertEqual(self.view.layout.connectors, split.connectors)
        _, islands = physical_wire_islands(self.view.layout, "b")
        contacts = [at for net, at in self.view.layout.pins() if net == "b"]
        self.assertNotEqual(islands[contacts[0]], islands[contacts[1]])
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))


if __name__ == "__main__":
    unittest.main()
