"""The schematic uses the LT1677's physical pin order even with an implicit port."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.geometry import symbol_pins
from circuitlab.viewer.schematic import SchematicView


class ChipPackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_lt1677_pin_rows_and_number_labels_follow_the_physical_package(self):
        # All eight nets are distinct, so the model also needs an implicit 0 port.
        circuit = compile_circuit("""
            include("LT1677");
            main { LT1677(trim1, minus, plus, vee, nc, out, vcc, trim2); }
        """, render_only=True)
        view = SchematicView()
        self.addCleanup(view.close)
        view.draw(circuit)
        drawing = view.layout
        symbol, = drawing.symbols
        _, a, b, _ = symbol
        pins = dict(symbol_pins(*symbol, grid=drawing.pin_grid))
        for side, column in (
            (a[0], ("trim1", "minus", "plus", "vee")),
            (b[0], ("trim2", "vcc", "out", "nc")),
        ):
            self.assertEqual([net for net, (x, y) in sorted(pins.items(), key=lambda item: -item[1][1])
                              if x == side and net != "0"], list(column))
        self.assertLess(pins["0"][1], pins["vee"][1])

        labels = {item.text(): item.sceneBoundingRect().center()
                  for item in view.scene().items() if isinstance(item, QGraphicsSimpleTextItem)}
        for left, right in zip("1234", "8765"):
            self.assertLess(labels[left].x(), labels[right].x())
            self.assertAlmostEqual(labels[left].y(), labels[right].y())
        self.assertLess(labels["1"].y(), labels["4"].y())
        self.assertNotIn("9", labels)
        self.assertTrue(preserves_nets(view.levels[None, "auto"], drawing))
