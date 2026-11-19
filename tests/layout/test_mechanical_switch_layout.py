"""Mechanical contacts retain two terminals while moving and toggling state."""
# A state toggle changes the lever, not the two contact identities or saved positions.
# Routing and rotation must treat the manual switch as a two-terminal component.

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.automatic import series_runs
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import component_pins, symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.math.report import component_labels


class MechanicalSwitchLayoutTest(unittest.TestCase):
    def test_series_contact_routes_with_only_two_terminals_in_each_style(self):
        for state in ('open', 'closed'):
            circuit = compile_circuit(
                'V1(5) between(supply,0);\n'
                f'main {{ series(supply,0) {{ S1={state}; R1=1k; }} }}',
                render_only=True,
            )
            contact = next(part for part in circuit.components if part.kind == 'K')
            pins = component_pins([contact])
            self.assertEqual(tuple(net for _, net, _, _ in pins), contact.terminal_nets)
            self.assertEqual(len(pins), 2)
            runs, _ = series_runs(circuit)
            self.assertTrue(any(
                {circuit.components[i].name for i in run} == {'S1', 'R1'} for run in runs
            ))
            for mode in ('auto', 'routed', 'labeled', 'fast'):
                with self.subTest(state=state, mode=mode):
                    labels = component_labels(circuit)
                    drawing = (fast_layout(circuit, labels) if mode == 'fast'
                               else schematic_layout(circuit, labels, mode))
                    self.assertTrue(preserves_nets(circuit, drawing))
                    placed = next(symbol for symbol in drawing.symbols if symbol[0].name == 'S1')
                    self.assertEqual(list(symbol_pins(*placed)),
                                     [(contact.p, placed[1]), (contact.n, placed[2])])

    def test_all_rotations_move_and_restore_both_contacts_on_grid(self):
        circuit = compile_circuit('S1(open) between(left,right);', render_only=True)
        drawing = align_layout(circuit, fast_layout(circuit, component_labels(circuit)))
        self.assertIsNotNone(drawing)
        original = pack_layout(drawing)
        for turns in range(4):
            with self.subTest(turns=turns):
                moved = move_symbol(circuit, drawing, 'S1', (2, 2), rotate=turns)
                self.assertIsNotNone(moved)
                self.assertTrue(preserves_nets(circuit, moved))
                self.assertEqual(len(list(moved.pins())), 2)
                for _, pin in moved.pins():
                    self.assertTrue(all(abs(value * 4 - round(value * 4)) < 1e-7 for value in pin))
                restored = restore_layout(pack_layout(moved), circuit, ())
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(moved.pins()))
        self.assertEqual(pack_layout(drawing), original)

    def test_toggling_state_reuses_saved_positions_and_wires(self):
        circuit = compile_circuit('S1(open) between(left,right);', render_only=True)
        drawing = fast_layout(circuit, component_labels(circuit))
        record = pack_layout(drawing)
        toggled = compile_circuit('S1(closed) between(left,right);', render_only=True)
        restored = restore_layout(record, toggled, ())
        self.assertIsNotNone(restored)
        self.assertEqual(pack_layout(restored), record)
        self.assertTrue(restored.symbols[0][0].closed)


if __name__ == '__main__':
    unittest.main()
