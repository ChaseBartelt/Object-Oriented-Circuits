"""The schematic uses the LT1677's physical pin order even with an implicit port."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import clear_package_wires, preserves_nets
from circuitlab.layout.geometry import package_contacts, quad_package, symbol_pins
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.viewer.schematic import SchematicView


class ChipPackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def assert_title_inside_package(self, view, part):
        _, body = view.packages[part.name]
        title = next(item for item in view.scene().items()
                     if isinstance(item, QGraphicsSimpleTextItem) and item.data(0) == "hood"
                     and item.data(1) == part.name)
        text = title.sceneBoundingRect()
        self.assertTrue(body.contains(text))
        self.assertAlmostEqual(text.center().x(), body.center().x())
        self.assertLess(text.center().y(), body.center().y())

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
        part, a, b, _ = symbol
        self.assert_title_inside_package(view, part)
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

    def test_transistor_package_title_is_centered_inside_the_top(self):
        circuit = compile_circuit("transistor Q1(base, collector, 0, npn);", render_only=True)
        view = SchematicView()
        self.addCleanup(view.close)
        view.draw(circuit)
        self.assert_title_inside_package(view, view.layout.symbols[0][0])

    def test_33_pin_chip_has_a_square_body_and_four_numbered_sides(self):
        pins = ", ".join(f"p{i}" for i in range(1, 34))
        nets = ", ".join(f"n{i}" for i in range(1, 34))
        loads = " ".join(f"Rload{i}(10k) between(n{i}, 0);" for i in (2, 13, 20, 28))
        circuit = compile_circuit(
            f"chip Large({pins}) {{ R1(10k) between(p1, p2); }}"
            f"main {{ Large({nets}); {loads} }}", render_only=True,
        )
        for mode in ("auto", "labeled"):
            with self.subTest(mode=mode):
                view = SchematicView()
                self.addCleanup(view.close)
                view.layout_mode = mode
                view.draw(circuit)
                drawing = view.layout
                part, a, b, _ = next(s for s in drawing.symbols if s[0].kind == "H")
                self.assertTrue(quad_package(part, a, b))
                _, body = view.packages[part.name]
                self.assertAlmostEqual(body.width(), body.height())
                self.assert_title_inside_package(view, part)
                contacts = list(package_contacts(part, a, b, drawing.pin_grid))
                self.assertEqual(len(contacts), 33)
                by_number = {int(item.text()): item.sceneBoundingRect().center()
                             for item in view.scene().items()
                             if isinstance(item, QGraphicsSimpleTextItem) and item.text().isdigit()}
                for index, ((label, net, side, _, _), expected_side) in enumerate(zip(
                    contacts, ("left",) * 9 + ("bottom",) * 8 + ("right",) * 8 + ("top",) * 8,
                ), 1):
                    self.assertEqual((label, net, side), (f"p{index}", f"n{index}", expected_side))
                    number = by_number[index]
                    self.assertTrue({"left": number.x() < body.left(),
                                     "bottom": number.y() > body.bottom(),
                                     "right": number.x() > body.right(),
                                     "top": number.y() < body.top()}[side])
                flat = view.levels[None, mode]
                self.assertTrue(preserves_nets(flat, drawing))
                self.assertTrue(clear_package_wires(drawing))
                restored = restore_layout(pack_layout(drawing), flat, drawing.boundary)
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(drawing.pins()))
