"""Physical lead spans are editable without changing electrical values."""

import math
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.geometry import Layout, routed_pins
from circuitlab.layout.persistence import pack_layout
from circuitlab.viewer.schematic import SchematicView, point


SOURCE = """perf[A-J,1-16];
C1(560nF) between(input,output);
C2(560nF) between(input,output);
C3(560nF) between(input,output);
R1(10k) between(u,v);
R2(20k) between(v,w);
"""


class ComponentLengthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        c1, c2, c3, r1, r2 = self.circuit.components
        symbols = [(c1, (1, -2), (3, -2), "+"), (c2, (1, -4), (3, -4), "+"),
                   (c3, (1, -6), (3, -6), "+"), (r1, (10, -2), (12, -2), "+"),
                   (r2, (10, -5), (12, -5), "+")]
        self.original = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=1,
            wires={"input": [((1, -6), (1, -2))], "output": [((3, -6), (3, -2))],
                   "v": [((12, -2), (13, -2)), ((13, -7), (13, -2)), ((9, -7), (13, -7)),
                         ((9, -7), (9, -5)), ((9, -5), (10, -5))]},
        )
        self.assertTrue(preserves_nets(self.circuit, self.original))
        self.view = SchematicView()
        self.view.resize(1000, 750)
        self.view.show()
        self.view.draw(self.circuit, layout=self.original)
        self.app.processEvents()
        self.key = None, "auto"

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def contacts(self, name="C1"):
        symbol = next(symbol for symbol in self.view.layout.symbols if symbol[0].name == name)
        return [at for _, at in routed_pins(symbol, self.view.layout.rotations.get(name, 0), 1)]

    def select_parts(self, names):
        for name in names:
            symbol = next(symbol for symbol in self.view.layout.symbols if symbol[0].name == name)
            _, a, b, _ = symbol
            at = self.view.mapFromScene(point(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)))
            QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, at)
        self.assertEqual(self.view.selected_symbols, set(names))

    def choose_menu(self, label, name=None):
        if name is None:
            at = self.view.viewport().rect().bottomRight()
        else:
            caption = next(item for item in self.view.captions
                           if item.data(0) == "component" and item.data(1) == name)
            at = self.view.mapFromScene(caption.sceneBoundingRect().center())
        actions = []

        def choose():
            menu = self.app.activePopupWidget()
            actions.extend(action.text() for action in menu.actions())
            action = next((action for action in menu.actions() if action.text() == label), None)
            if action:
                action.trigger()
            menu.close()

        QTimer.singleShot(0, choose)
        self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, at,
                                                   self.view.viewport().mapToGlobal(at)))
        self.assertIn(label, actions)

    def adjust(self, holes, accepted=True):
        with patch("circuitlab.viewer.schematic.QInputDialog.getInt", return_value=(holes, accepted)) as dialog:
            self.view.adjust_component_length(self.key, "C1")
        return dialog

    def test_right_click_sets_seven_holes_and_keeps_other_parts_and_values(self):
        caption = next(item for item in self.view.captions
                       if item.data(0) == "component" and item.data(1) == "C1")
        at = self.view.mapFromScene(caption.sceneBoundingRect().center())
        actions = []

        def choose():
            menu = self.app.activePopupWidget()
            actions.extend(action.text() for action in menu.actions())
            action = next((action for action in menu.actions() if action.text() == "Adjust length…"), None)
            if action:
                action.trigger()
            menu.close()

        with patch("circuitlab.viewer.schematic.QInputDialog.getInt", return_value=(7, True)) as dialog:
            QTimer.singleShot(0, choose)
            self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, at,
                                                       self.view.viewport().mapToGlobal(at)))
        self.assertIn("Adjust length…", actions)
        self.assertEqual(dialog.call_args.args[3:7], (3, 3, 256, 1))
        self.assertIn("including both ends", dialog.call_args.args[2])
        self.assertEqual(self.contacts(), [(1, -2), (7, -2)])
        self.assertEqual(self.view.layout.symbols[1:], self.original.symbols[1:])
        self.assertEqual(self.view.layout.wires["v"], self.original.wires["v"])
        self.assertEqual(self.circuit.components[0].value, 560e-9)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_length_can_shrink_and_cancel_or_undo_preserves_geometry(self):
        self.adjust(7)
        resized = self.view.layout
        self.adjust(3)
        self.assertEqual(math.dist(*self.contacts()), 2)
        self.view.undo()
        self.assertIs(self.view.layout, resized)
        self.adjust(12, accepted=False)
        self.assertIs(self.view.layout, resized)
        self.view.undo()
        self.assertIs(self.view.layout, self.original)

    def test_resized_capacitor_survives_save_reopen_align_and_optimize(self):
        self.adjust(7)
        saved = pack_layout(self.view.layout)
        expected = self.contacts()
        self.view.clear()
        self.view.placement_records[self.key] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assertEqual(self.contacts(), expected)
        self.assertTrue(self.view.align_to_grid())
        self.view.optimize_wiring()
        self.assertEqual(self.contacts(), expected)

    def test_hidden_resize_is_retained_when_wires_are_added(self):
        self.view.set_wires_visible(False)
        reference = dict(self.view.layout.wiring_positions)
        old_wires = self.view.layout.wires
        self.adjust(7)
        self.assertFalse(self.view.layout.wires_visible)
        self.assertEqual(self.view.layout.wiring_positions, reference)
        self.assertEqual(self.view.layout.wires, old_wires)
        self.assertEqual(self.contacts(), [(1, -2), (7, -2)])
        self.view.set_wires_visible(True)
        self.assertEqual(self.contacts(), [(1, -2), (7, -2)])
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_rotated_capacitor_keeps_first_contact_and_orientation(self):
        circuit = compile_circuit("perf[A-R,1-24]; C1(560nF) between(input,output);", render_only=True)
        cap, = circuit.components
        drawing = Layout(symbols=[(cap, (5, -7), (7, -7), "+")], positions={"C1": (5, -7)}, pin_grid=1)
        self.view.draw(circuit, layout=drawing)
        self.view.rotate_component(self.key, "C1")
        first = self.contacts()[0]
        rotation = self.view.layout.rotations["C1"]
        self.adjust(7)
        self.assertEqual(self.contacts()[0], first)
        self.assertEqual(math.dist(*self.contacts()), 6)
        self.assertEqual(self.view.layout.rotations["C1"], rotation)
        self.assertTrue(all(value == round(value) for at in self.contacts() for value in at))

    def test_three_selected_capacitors_resize_in_one_dialog_and_one_undo(self):
        names = {"C1", "C2", "C3"}
        self.select_parts(sorted(names))
        first = {name: self.contacts(name)[0] for name in names}
        history = len(self.view.undo_history)
        with patch("circuitlab.viewer.schematic.QInputDialog.getInt", return_value=(7, True)) as dialog:
            self.choose_menu("Adjust length…", "C1")
        dialog.assert_called_once()
        self.assertIn("3 selected parts", dialog.call_args.args[2])
        for name in names:
            self.assertEqual(self.contacts(name)[0], first[name])
            self.assertEqual(math.dist(*self.contacts(name)), 6)
        self.assertEqual(self.view.layout.wires["output"], [((7, -6), (7, -2))])
        self.assertEqual(self.view.layout.symbols[3:], self.original.symbols[3:])
        self.assertEqual(self.view.layout.wires["v"], self.original.wires["v"])
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))
        self.assertEqual(len(self.view.undo_history), history + 1)
        self.assertEqual(self.view.selected_symbols, names)
        self.view.undo()
        self.assertIs(self.view.layout, self.original)

    def test_unselected_part_is_adjusted_independently_of_existing_selection(self):
        self.select_parts(["C1", "C2", "C3"])
        with patch("circuitlab.viewer.schematic.QInputDialog.getInt", return_value=(4, True)):
            self.choose_menu("Adjust length…", "R1")
        self.assertEqual(math.dist(*self.contacts("R1")), 3)
        for name in ("C1", "C2", "C3"):
            self.assertEqual(math.dist(*self.contacts(name)), 2)

    def test_selection_menu_resizes_all_without_wires_and_reopens_correctly(self):
        self.select_parts(["C1", "C2", "C3"])
        self.view.set_wires_visible(False)
        before = self.view.layout
        with patch("circuitlab.viewer.schematic.QInputDialog.getInt", return_value=(7, False)):
            self.choose_menu("Adjust length…")
        self.assertIs(self.view.layout, before)
        with patch("circuitlab.viewer.schematic.QInputDialog.getInt", return_value=(7, True)):
            self.choose_menu("Adjust length…")
        saved = pack_layout(self.view.layout)
        self.view.clear()
        self.view.placement_records[self.key] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.view.set_wires_visible(True)
        for name in ("C1", "C2", "C3"):
            self.assertEqual(math.dist(*self.contacts(name)), 6)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))

    def test_both_rotation_menus_apply_to_selection_in_one_undo_step(self):
        symbols = [self.original.symbols[0],
                   (self.original.symbols[1][0], (1, -5), (3, -5), "+"),
                   (self.original.symbols[2][0], (1, -8), (3, -8), "+"), *self.original.symbols[3:]]
        drawing = replace(self.original, symbols=symbols,
                          positions={part.name: a for part, a, _, _ in symbols},
                          wires={**self.original.wires, "input": [((1, -8), (1, -2))],
                                 "output": [((3, -8), (3, -2))]})
        self.view.draw(self.circuit, layout=drawing)
        names = {"C1", "C2", "C3"}
        self.select_parts(sorted(names))
        history = len(self.view.undo_history)
        self.choose_menu("Rotate clockwise", "C1")
        self.assertEqual(self.view.layout.rotations, {name: 1 for name in names})
        self.assertEqual(self.view.layout.symbols[3:], drawing.symbols[3:])
        self.assertEqual(self.view.layout.wires["v"], drawing.wires["v"])
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))
        self.assertEqual(len(self.view.undo_history), history + 1)
        self.assertEqual(self.view.selected_symbols, names)
        self.view.undo()
        self.assertIs(self.view.layout, drawing)
        self.choose_menu("Rotate counterclockwise")
        self.assertEqual(self.view.layout.rotations, {name: 3 for name in names})
        self.view.undo()
        self.assertIs(self.view.layout, drawing)

    def test_rotation_selection_also_works_in_ordinary_schematics(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nC1(1u) between(c,d);", render_only=True)
        resistor, capacitor = circuit.components
        drawing = Layout(symbols=[(resistor, (0, 0), (2, 0), "+"), (capacitor, (8, 0), (10, 0), "+")],
                         positions={"R1": (0, 0), "C1": (8, 0)})
        self.view.draw(circuit, layout=drawing)
        self.select_parts(["R1", "C1"])
        self.choose_menu("Rotate counterclockwise", "C1")
        self.assertEqual(self.view.layout.rotations, {"R1": 3, "C1": 3})
        self.assertTrue(preserves_nets(circuit, self.view.layout))
        self.view.undo()
        self.assertIs(self.view.layout, drawing)

    def test_rotation_includes_selected_ground_symbols(self):
        circuit = compile_circuit("R1(1k) between(a,0);\nC1(1u) between(b,0);", render_only=True)
        resistor, capacitor = circuit.components
        drawing = Layout(
            symbols=[(resistor, (0, 0), (2, 0), "+"), (capacitor, (8, 0), (10, 0), "+")],
            positions={"R1": (0, 0), "C1": (8, 0)}, grounds=[(5, -3)],
            wires={"0": [((2, 0), (3, 0)), ((3, -3), (3, 0)), ((3, -3), (5, -3)),
                          ((10, 0), (11, 0)), ((11, -3), (11, 0)), ((5, -3), (11, -3))]},
        )
        self.view.draw(circuit, layout=drawing)
        self.select_parts(["R1", "C1"])
        self.view.selected_symbols.add(("ground", (5, -3)))
        self.choose_menu("Rotate clockwise", "C1")
        self.assertEqual(self.view.layout.rotations, {"R1": 1, "C1": 1})
        self.assertEqual(self.view.layout.ground_rotations, {(5, -3): 1})
        self.assertTrue(preserves_nets(circuit, self.view.layout))
        self.view.undo()
        self.assertIs(self.view.layout, drawing)


if __name__ == "__main__":
    unittest.main()
