"""All eight amplifier contacts survive routing, rotation and persistence."""

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.math.report import component_labels


class AudioLayoutTest(unittest.TestCase):
    def test_every_style_preserves_all_eight_pin_nets_without_model_symbols(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,gnd,g1,g8,bypass);", render_only=True)
        for style in ("auto", "routed", "labeled", "fast"):
            with self.subTest(style=style):
                labels = component_labels(circuit)
                drawing = fast_layout(circuit, labels) if style == "fast" else schematic_layout(circuit, labels, style)
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertEqual(len(drawing.symbols), 1)
                self.assertEqual(len(list(drawing.pins())), 8)
                self.assertEqual(labels, {"op1": ["op1"]})
                self.assertEqual([n for n, _ in symbol_pins(*drawing.symbols[0])],
                                 ["plus", "minus", "out", "vcc", "gnd", "g1", "g8", "bypass"])

    def test_rotation_move_and_saved_layout_preserve_each_contact(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,0,g1,g8,bypass);", render_only=True)
        drawing = align_layout(circuit, fast_layout(circuit, component_labels(circuit)))
        self.assertIsNotNone(drawing)
        for turns in range(4):
            with self.subTest(turns=turns):
                moved = move_symbol(circuit, drawing, "op1", (2, 2), rotate=turns)
                self.assertIsNotNone(moved)
                self.assertTrue(preserves_nets(circuit, moved))
                restored = restore_layout(pack_layout(moved), circuit, ())
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(moved.pins()))
