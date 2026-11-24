"""Two-face routing keeps fixed parts, physical Split islands, and saved layers safe."""

from copy import deepcopy
from dataclasses import replace
import json
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.board_layers import layer_wires, wire_subset
from circuitlab.layout.board_routing import optimize_board_wiring
from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.route_quality import wire_length


class BoardRoutingSafetyTest(unittest.TestCase):
    def simple(self):
        circuit = compile_circuit("perf[A-R,1-24];\nR1(1k) between(a,b);\nR2(2k) between(a,c);",
                                  render_only=True)
        first, second = circuit.components
        symbols = [(first, (2, -2), (2, -4), "+"), (second, (12, -2), (12, -4), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         compact_labels=frozenset({"R1"}),
                         wires={"a": [((2, -2), (2, 0)), ((2, 0), (12, 0)), ((12, 0), (12, -2))]})
        return circuit, drawing

    def labeled_crossing(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        R1(1k) between(a,b);
        R2(2k) between(a,c);
        R3(3k) between(a,d);
        R4(4k) between(a,e);
        """, render_only=True)
        first, second, third, fourth = circuit.components
        symbols = [(first, (2, -4), (2, -6), "+"), (second, (12, -4), (12, -6), "+"),
                   (third, (7, 0), (9, 0), "+"), (fourth, (7, -9), (9, -9), "+")]
        top = [((2, -4), (2, -1)), ((2, -1), (12, -1)), ((12, -1), (12, -4))]
        back = [((7, 0), (7, -7)), ((7, -7), (1, -7)), ((1, -7), (1, -9)), ((1, -9), (7, -9))]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"a": [*top, *back]}, underside_wires={"a": back},
                         connectors=[("a", (2, -4), "shared", True, False),
                                     ("a", (7, 0), "shared", False, True)])
        mark_crossings(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertFalse(preserves_nets(circuit, replace(drawing, connectors=[])))
        return circuit, drawing

    def assert_safe(self, circuit, drawing):
        self.assertTrue(preserves_nets(circuit, drawing))
        top = [edge for edges in layer_wires(drawing).values() for edge in edges]
        self.assertTrue(clear_of_bodies(top, drawing, underside=False))
        self.assertTrue(all(wire_subset(edges, drawing.wires.get(net, ()))
                            for net, edges in drawing.underside_wires.items()))
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1]
                            for edges in drawing.wires.values() for a, b in edges))

    def test_shorter_route_keeps_parts_values_display_preferences_and_input_unchanged(self):
        circuit, original = self.simple()
        snapshot = deepcopy(original)
        routed = optimize_board_wiring(circuit, original)
        self.assert_safe(circuit, routed)
        self.assertLess(wire_length(routed), wire_length(original))
        self.assertEqual(routed.symbols, original.symbols)
        self.assertEqual(routed.positions, original.positions)
        self.assertEqual(routed.rotations, original.rotations)
        self.assertEqual(routed.compact_labels, original.compact_labels)
        self.assertEqual(original, snapshot)
        saved = json.loads(json.dumps(pack_layout(routed)))
        restored = restore_layout(saved, circuit, (), editable_perfboard=True)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.wires, routed.wires)
        self.assertEqual(restored.underside_wires, routed.underside_wires)

    def test_insulated_same_net_crossing_does_not_merge_matching_label_islands(self):
        circuit, original = self.labeled_crossing()
        snapshot = deepcopy(original)
        routed = optimize_board_wiring(circuit, original)
        self.assert_safe(circuit, routed)
        self.assertLess(wire_length(routed), wire_length(original))
        self.assertEqual(routed.connectors, original.connectors)
        self.assertFalse(preserves_nets(circuit, replace(routed, connectors=[])))
        self.assertEqual(original, snapshot)

    def test_reference_routes_keep_split_ownership_after_a_part_moves_in_placement_mode(self):
        circuit, reference = self.labeled_crossing()
        first, a, b, top = reference.symbols[0]
        moved = first, (a[0] + 1, a[1]), (b[0] + 1, b[1]), top
        drawing = replace(reference, symbols=[moved, *reference.symbols[1:]],
                          positions={**reference.positions, first.name: moved[1]})
        before, reference_before = deepcopy(drawing), deepcopy(reference)
        routed = optimize_board_wiring(circuit, drawing, reference=reference)
        self.assert_safe(circuit, routed)
        self.assertEqual(routed.symbols, drawing.symbols)
        self.assertEqual(routed.connectors, reference.connectors)
        self.assertFalse(preserves_nets(circuit, replace(routed, connectors=[])))
        self.assertEqual(drawing, before)
        self.assertEqual(reference, reference_before)

    def test_external_labeled_endpoint_is_not_clipped_to_the_board(self):
        circuit, original = self.simple()
        outside = (-3, -2)
        original = replace(original, wires={"a": [*original.wires["a"], (outside, (2, -2))]},
                           connectors=[("a", outside, "external", True, False)])
        routed = optimize_board_wiring(circuit, original)
        self.assert_safe(circuit, routed)
        self.assertEqual(routed.connectors, original.connectors)
        self.assertTrue(any(segments_intersect((outside, outside), edge) for edge in routed.wires["a"]))

    def test_unfixable_foreign_pin_collision_returns_original_without_partial_changes(self):
        circuit, original = self.simple()
        second = replace(circuit.components[1], p="foreign")
        circuit = replace(circuit, components=[circuit.components[0], second])
        original = replace(original, symbols=[original.symbols[0], (second, (2, -2), (4, -2), "+")],
                           positions={**original.positions, second.name: (2, -2)})
        snapshot = deepcopy(original)
        self.assertFalse(preserves_nets(circuit, original))
        self.assertIs(optimize_board_wiring(circuit, original), original)
        self.assertEqual(original, snapshot)


if __name__ == "__main__":
    unittest.main()
