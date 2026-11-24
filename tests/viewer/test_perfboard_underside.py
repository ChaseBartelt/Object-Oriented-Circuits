"""The solder-side drawing edits the same board through mirrored coordinates."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.board_layers import layer_wires, move_board_wire
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout
from circuitlab.viewer.perfboard import board_point, underside_offset
from circuitlab.viewer.schematic import SchematicView, point


SOURCE = """perf[A-J,1-16];
R1(1k) between(a,b);
R2(2k) between(b,c);
R3(3k) between(b,d);
"""


class PerfboardUndersideTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        r1, r2, r3 = self.circuit.components
        symbols = [(r1, (2, -2), (4, -2), "+"), (r2, (8, -2), (10, -2), "+"),
                   (r3, (12, -6), (14, -6), "+")]
        self.original = Layout(symbols=symbols, positions={p.name: a for p, a, *_ in symbols}, pin_grid=1,
                               wires={"b": [((4, -2), (4, -6)), ((4, -6), (8, -6)),
                                             ((8, -6), (8, -2)), ((8, -6), (12, -6))]})
        self.view = SchematicView()
        self.view.resize(1400, 750)
        self.view.show()
        self.view.draw(self.circuit, layout=self.original)
        self.app.processEvents()
        self.key = None, "auto"

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def at(self, at, *, back=False):
        if back:
            at = board_point(self.circuit.perfboard, at, underside=True,
                             offset=underside_offset(self.circuit.perfboard))
        return self.view.mapFromScene(point(at))

    def choose(self, label, at, *, back=False):
        position = self.at(at, back=back)
        found = []

        def trigger():
            menu = self.app.activePopupWidget()
            found.extend(action.text() for action in menu.actions())
            action = next((a for a in menu.actions() if a.text() == label), None)
            if action:
                action.trigger()
            menu.close()

        QTimer.singleShot(0, trigger)
        self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position,
                                                   self.view.viewport().mapToGlobal(position)))
        self.assertIn(label, found)

    def test_only_double_clicking_empty_board_toggles_mirrored_second_board(self):
        self.assertFalse(self.view.underside_visible)
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at((1, -8)))
        self.assertFalse(self.view.underside_visible)
        QTest.mouseDClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at((1, -8)))
        self.assertTrue(self.view.underside_visible)
        boards = [i for i in self.view.scene().items() if i.data(0) == "perfboard"]
        self.assertEqual({i.data(2) for i in boards}, {"top", "underside"})
        self.assertEqual(self.view.layout, self.original)
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at((1, -8), back=True))
        self.assertTrue(self.view.underside_visible)
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at((1, -8)))
        self.assertTrue(self.view.underside_visible)
        QTest.mouseDClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at((1, -8), back=True))
        self.assertFalse(self.view.underside_visible)

    def test_double_clicking_a_part_or_wire_does_not_close_the_underside(self):
        self.view.toggle_underside()
        for at in ((3, -2), (6, -6)):
            QTest.mouseDClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at(at))
            QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.at(at))
            self.assertTrue(self.view.underside_visible)

    def test_right_click_pin_transfers_only_its_branch_and_undo_restores_it(self):
        self.choose("Wire underneath", (4, -2))
        drawing = self.view.layout
        self.assertTrue(self.view.underside_visible)
        self.assertEqual(drawing.symbols, self.original.symbols)
        self.assertEqual(drawing.wires, self.original.wires)
        self.assertEqual(set(layer_wires(drawing, underside=True)["b"]),
                         {((4, -6), (4, -2)), ((4, -6), (8, -6))})
        self.assertTrue(preserves_nets(self.circuit, drawing))
        self.assertTrue(any(i.data(0) == "back-wire" for i in self.view.scene().items()))
        self.view.undo()
        self.assertEqual(self.view.layout, self.original)

    def test_wire_menu_can_return_a_back_branch_to_the_component_side(self):
        self.choose("Wire underneath", (6, -6))
        self.choose("Wire on top", (6, -6), back=True)
        self.assertFalse(self.view.layout.underside_wires)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_mirrored_drag_moves_back_wire_and_leaves_front_branches_fixed(self):
        self.choose("Wire underneath", (4, -2))
        front = layer_wires(self.view.layout)
        start, end = self.at((4, -4), back=True), self.at((5, -4), back=True)
        self.assertLess(end.x(), start.x())
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertIsNotNone(self.view.drag)
        self.assertTrue(self.view.drag.mirrored)
        self.assertTrue(self.view.drag.underside)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(layer_wires(self.view.layout), front)
        back = layer_wires(self.view.layout, underside=True)["b"]
        self.assertTrue(any(a[0] == b[0] == 5 for a, b in back))
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_underside_assignment_reopens_and_survives_hidden_wiring(self):
        self.choose("Wire underneath", (4, -2))
        assigned = self.view.layout.underside_wires
        self.view.set_wires_visible(False)
        saved = pack_layout(self.view.layout)
        self.view.clear()
        self.view.placement_records[self.key] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assertEqual(self.view.layout.underside_wires, assigned)
        self.view.set_wires_visible(True)
        self.assertTrue(self.view.layout.underside_wires)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_back_wires_remain_pickable_with_screen_tolerance_outside_board(self):
        self.choose("Wire underneath", (4, -2))
        drawing, _ = move_board_wire(self.circuit, self.view.layout, "b", ((4, -6), (8, -6)),
                                     10, underside=True, repair=True)
        self.assertIsNotNone(drawing)
        self.view._commit_layout_edit(self.key, drawing, selected=None)
        at = self.at((6, 4), back=True) + QPoint(0, 3)
        self.assertTrue(self.view._underside_at(at))
        self.assertEqual(self.view.nearest_wire(at), (self.key, "b", ((4, 4), (8, 4))))


if __name__ == "__main__":
    unittest.main()
