"""Optional op-amp supply contacts stay connected through layout edits."""
# Power contacts above/below the amplifier must remain attached in every wiring style.
# Move, rotate, and save/restore cases guard against a visually present but disconnected rail.

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.math.report import component_labels


class PoweredOpampLayoutTest(unittest.TestCase):
    def test_optional_supply_pins_leave_the_original_signal_geometry_unchanged(self):
        legacy = compile_circuit('op1(plus,minus,out);', render_only=True).components[0]
        powered = compile_circuit('op1(plus,minus,out,vcc,vee);', render_only=True).components[0]
        original = list(symbol_pins(legacy, (2, 3), (6, 3), '+', grid=.25))
        actual = list(symbol_pins(powered, (2, 3), (6, 3), '+', grid=.25))
        self.assertEqual(actual[:3], original)
        self.assertEqual(actual[3:], [('vcc', (4, 4.25)), ('vee', (4, 1.75))])

    def test_supply_wires_route_in_every_style_and_survive_rotation_and_save(self):
        circuit = compile_circuit(
            'V1(2) between(input,0);\nV2(5) between(vcc,0);\nV3(-5) between(vee,0);\n'
            'R1(10k) between(input,minus);\nR2(10k) between(out,minus);\n'
            'op1(0,minus,out,vcc,vee);'
        )
        for mode in ('auto', 'routed', 'labeled', 'fast'):
            with self.subTest(mode=mode):
                labels = component_labels(circuit)
                drawing = (fast_layout(circuit, labels) if mode == 'fast'
                           else schematic_layout(circuit, labels, mode))
                self.assertTrue(preserves_nets(circuit, drawing))
                amp = next(symbol for symbol in drawing.symbols if symbol[0].name == 'op1')
                self.assertEqual([net for net, _ in symbol_pins(*amp)][-2:], ['vcc', 'vee'])
                self.assertIn('vcc', drawing.wires)
                self.assertIn('vee', drawing.wires)

        single = compile_circuit('op1(0,minus,out,vcc,vee);', render_only=True)
        drawing = align_layout(single, fast_layout(single, component_labels(single)))
        self.assertIsNotNone(drawing)
        for turns in range(4):
            with self.subTest(turns=turns):
                moved = move_symbol(single, drawing, 'op1', (2, 2), rotate=turns)
                self.assertIsNotNone(moved)
                self.assertTrue(preserves_nets(single, moved))
                restored = restore_layout(pack_layout(moved), single, ())
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(moved.pins()))
                self.assertEqual(len(list(restored.pins())), 5)


if __name__ == '__main__':
    unittest.main()
