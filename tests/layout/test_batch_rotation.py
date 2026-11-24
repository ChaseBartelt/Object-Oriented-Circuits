"""Selected ordinary symbols rotate together without partial geometry edits."""

from copy import deepcopy
from dataclasses import replace
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import rotate_symbols
from circuitlab.layout.geometry import Layout, routed_pins


class BatchRotationTest(unittest.TestCase):
    def disconnected(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nC1(1u) between(c,d);\nRkeep(2k) between(e,f);",
                                  render_only=True)
        first, second, fixed = circuit.components
        symbols = [(first, (0, 0), (4, 0), "+"),
                   (second, (12, 5), (16, 5), "+"),
                   (fixed, (25, 5), (29, 5), "+")]
        return circuit, Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols},
                               rotations={"Rkeep": 2}, compact_labels=frozenset({"C1"}))

    def test_clockwise_and_counterclockwise_rotate_each_selected_part_around_its_center(self):
        circuit, original = self.disconnected()
        snapshot = deepcopy(original)
        for turns, expected in ((1, 1), (-1, 3)):
            with self.subTest(turns=turns):
                rotated = rotate_symbols(circuit, original, {"R1", "C1"}, turns)
                self.assertIsNotNone(rotated)
                self.assertEqual(rotated.rotations, {"Rkeep": 2, "R1": expected, "C1": expected})
                self.assertEqual(rotated.symbols, original.symbols)
                self.assertEqual(rotated.positions, original.positions)
                self.assertEqual(rotated.compact_labels, original.compact_labels)
                for symbol in rotated.symbols[:2]:
                    pins = [at for _, at in routed_pins(symbol, expected)]
                    center = tuple((a + b) / 2 for a, b in zip(symbol[1], symbol[2]))
                    self.assertEqual(tuple((a + b) / 2 for a, b in zip(*pins)), center)
                self.assertTrue(preserves_nets(circuit, rotated))
                self.assertEqual(original, snapshot)

    def test_coupled_chain_reroutes_both_rotated_contacts_together(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nC1(1u) between(b,c);", render_only=True)
        first, second = circuit.components
        original = Layout(symbols=[(first, (0, 0), (4, 0), "+"), (second, (8, 0), (12, 0), "+")],
                          positions={"R1": (0, 0), "C1": (8, 0)},
                          wires={"b": [((4, 0), (8, 0))]})
        snapshot = deepcopy(original)
        rotated = rotate_symbols(circuit, original, {"R1", "C1"}, 1)
        self.assertIsNotNone(rotated)
        self.assertTrue(preserves_nets(circuit, rotated))
        self.assertEqual(rotated.rotations, {"R1": 1, "C1": 1})
        restored = rotate_symbols(circuit, rotated, {"R1", "C1"}, -1)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.rotations, {})
        self.assertEqual(restored.symbols, original.symbols)
        self.assertTrue(preserves_nets(circuit, restored))
        self.assertEqual(original, snapshot)

    def test_whole_turns_or_no_selected_symbols_return_original(self):
        circuit, original = self.disconnected()
        for turns in (0, 4, -4, 8):
            self.assertIs(rotate_symbols(circuit, original, {"R1", "C1"}, turns), original)
        self.assertIs(rotate_symbols(circuit, original, set(), 1), original)
        self.assertIs(rotate_symbols(circuit, original, {"missing"}, 1), original)

    def test_body_overlap_rejects_the_entire_rotation_without_mutation(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nR2(2k) between(c,d);", render_only=True)
        first, second = circuit.components
        original = Layout(symbols=[(first, (0, 0), (4, 0), "+"), (second, (0, 1), (4, 1), "+")],
                          positions={"R1": (0, 0), "R2": (0, 1)})
        snapshot = deepcopy(original)
        self.assertTrue(preserves_nets(circuit, original))
        self.assertIsNone(rotate_symbols(circuit, original, {"R1", "R2"}, 1))
        self.assertEqual(original, snapshot)

    def test_new_foreign_pin_contact_rejects_the_entire_rotation_without_mutation(self):
        circuit, original = self.disconnected()
        fixed = original.symbols[-1][0]
        original = replace(original,
                           symbols=[*original.symbols[:2], (fixed, (2, 2), (6, 2), "+")],
                           positions={**original.positions, fixed.name: (2, 2)}, rotations={})
        snapshot = deepcopy(original)
        self.assertTrue(preserves_nets(circuit, original))
        self.assertIsNone(rotate_symbols(circuit, original, {"R1", "C1"}, 1))
        self.assertEqual(original, snapshot)


if __name__ == "__main__":
    unittest.main()
