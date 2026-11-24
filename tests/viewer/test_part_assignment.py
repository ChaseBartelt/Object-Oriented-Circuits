"""Reconnect identical board parts according to their physical placement."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.geometry import Layout, routed_pins
from circuitlab.layout.persistence import pack_layout
from circuitlab.layout.perfboard import reroute_perfboard
from circuitlab.layout.route_quality import wire_length
from circuitlab.viewer.schematic import SchematicView


SOURCE = """Rleft(1k) between(left,return_left);
Rright(2k) between(right,return_right);
Cleft(100nF) between(left,return_left);
Cright(100nF) between(right,return_right);
perf[A-J,1-20];
"""


class PartAssignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        rleft, rright, cleft, cright = self.circuit.components
        symbols = [(rleft, (2, -2), (2, -4), "+"),
                   (rright, (16, -2), (16, -4), "+"),
                   (cleft, (13, -2), (13, -4), "+"),
                   (cright, (5, -2), (5, -4), "+")]
        self.original = Layout(
            symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
            wires={"left": [((2, -2), (13, -2))],
                   "return_left": [((2, -4), (13, -4))],
                   "right": [((5, -2), (16, -2))],
                   "return_right": [((5, -4), (16, -4))]},
        )
        self.view = SchematicView()
        self.view.resize(1000, 750)
        self.view.draw(self.circuit, layout=self.original)
        self.app.processEvents()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def assert_nearby_assignments(self):
        drawing = self.view.layout
        self.assertEqual(drawing.positions["Cleft"], (5, -2))
        self.assertEqual(drawing.positions["Cright"], (13, -2))
        self.assertEqual(drawing.symbols[:2], self.original.symbols[:2])
        self.assertEqual(sorted((a, b) for _, a, b, _ in drawing.symbols),
                         sorted((a, b) for _, a, b, _ in self.original.symbols))
        self.assertEqual([part for part, *_ in drawing.symbols], self.circuit.components)
        self.assertTrue(preserves_nets(self.circuit, drawing))
        self.assertLess(wire_length(drawing), wire_length(self.original) / 2)
        contacts = {part.name: list(routed_pins(symbol, drawing.rotations.get(part.name, 0), 1))
                    for symbol in drawing.symbols for part in [symbol[0]]}
        self.assertEqual(contacts["Cleft"], [("left", (5, -2)), ("return_left", (5, -4))])
        self.assertEqual(contacts["Cright"], [("right", (13, -2)), ("return_right", (13, -4))])
        captions = {item.data(1): item.text() for item in self.view.captions
                    if item.data(0) == "component"}
        self.assertIn("Cleft", captions["Cleft"])
        self.assertIn("Cright", captions["Cright"])

    def test_optimize_reassigns_identities_and_is_one_undo_step(self):
        self.view.optimize_wiring()
        self.assert_nearby_assignments()
        self.view.undo()
        self.assertEqual(self.view.layout, self.original)

    def test_add_wires_matches_nearby_parts_and_saves_the_names(self):
        self.view.set_wires_visible(False)
        self.view.set_wires_visible(True)
        self.assert_nearby_assignments()
        saved = pack_layout(self.view.layout)
        self.view.clear()
        self.view.placement_records[None, "auto"] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assert_nearby_assignments()
        self.assertEqual(self.circuit.components, compile_circuit(SOURCE, render_only=True).components)

    def test_add_wires_undo_restores_hidden_identity_assignments(self):
        self.view.set_wires_visible(False)
        hidden = self.view.layout
        self.view.set_wires_visible(True)
        self.assert_nearby_assignments()
        self.view.undo()
        self.assertEqual(self.view.layout, hidden)
        self.assertFalse(self.view.layout.wires_visible)

    def test_failed_assignment_route_keeps_the_previous_working_drawing(self):
        valid = reroute_perfboard(self.circuit, self.original)
        self.assertTrue(preserves_nets(self.circuit, valid))
        self.view.draw(self.circuit, layout=valid)
        with patch("circuitlab.viewer.schematic.optimize_board_wiring", side_effect=lambda _, drawing, **kw: drawing):
            self.view.optimize_wiring()
        self.assertEqual(self.view.layout, valid)
        self.assertTrue(preserves_nets(self.circuit, self.view.layout))


if __name__ == "__main__":
    unittest.main()
