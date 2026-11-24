"""Explicit board optimization can use the solder side without moving parts."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.board_layers import layer_wires
from circuitlab.layout.checks import mark_crossings, preserves_nets
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout
from circuitlab.layout.route_quality import crossing_count, wire_length
from circuitlab.viewer.schematic import SchematicView


SOURCE = """perf[A-N,1-18];
Rleft(1k) between(left_return,a);
Rright(2k) between(a,right_return);
Rtop(3k) between(top_return,b);
Rbottom(4k) between(b,bottom_return);
"""


class PerfboardOptimizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        left, right, top, bottom = self.circuit.components
        symbols = [(left, (1, -7), (3, -7), "+"), (right, (13, -7), (15, -7), "+"),
                   (top, (7, -1), (7, -3), "+"), (bottom, (7, -11), (7, -13), "+")]
        self.original = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                               wires={"a": [((3, -7), (13, -7))], "b": [((7, -3), (7, -11))]})
        mark_crossings(self.original)
        self.assertTrue(preserves_nets(self.circuit, self.original))
        self.assertEqual(crossing_count(self.original), 1)
        self.view = SchematicView()
        self.view.resize(1400, 750)
        self.view.draw(self.circuit, layout=self.original)
        self.messages = []
        self.view.optimization_finished.connect(self.messages.append)
        self.app.processEvents()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def assert_two_faces(self):
        result = self.view.layout
        self.assertTrue(result.underside_wires)
        self.assertTrue(layer_wires(result))
        self.assertEqual(result.symbols, self.original.symbols)
        self.assertEqual(result.positions, self.original.positions)
        self.assertTrue(preserves_nets(self.circuit, result))
        self.assertLessEqual(wire_length(result), wire_length(self.original))
        self.assertEqual(crossing_count(result), 0)

    def test_optimize_automatically_uses_both_faces_and_opens_the_back_view(self):
        self.view.optimize_wiring()
        self.assert_two_faces()
        self.assertTrue(self.view.underside_visible)
        self.assertIn("1 fewer crossing", self.messages[-1])
        self.assertTrue(any(item.data(0) == "back-wire" for item in self.view.scene().items()))
        self.view.undo()
        self.assertEqual(self.view.layout, self.original)
        self.assertFalse(self.view.layout.underside_wires)

    def test_two_face_optimization_survives_save_reopen_and_repeated_optimization(self):
        self.view.optimize_wiring()
        self.assert_two_faces()
        saved = pack_layout(self.view.layout)
        self.view.clear()
        self.view.placement_records[None, "auto"] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assert_two_faces()
        before = self.view.layout
        self.view.optimize_wiring()
        self.assert_two_faces()
        self.assertLessEqual(wire_length(self.view.layout), wire_length(before))
        self.assertEqual(self.circuit.components, compile_circuit(SOURCE, render_only=True).components)


if __name__ == "__main__":
    unittest.main()
