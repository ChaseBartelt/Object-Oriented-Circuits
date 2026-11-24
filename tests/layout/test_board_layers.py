"""Board faces retain electrical contacts while their routes move independently."""

from dataclasses import replace
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.board_layers import (
    layer_wires, move_board_wire, remap_wire_layers, set_wire_layer, wire_subset,
)
from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets
from circuitlab.layout.geometry import Layout
from circuitlab.layout.perfboard import reroute_perfboard
from circuitlab.layout.persistence import pack_layout


class BoardLayersTest(unittest.TestCase):
    def branch(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        R1(1k) between(a,b);
        R2(2k) between(a,c);
        R3(3k) between(a,d);
        """, render_only=True)
        first, second, third = circuit.components
        symbols = [(first, (0, 0), (0, -2), "+"), (second, (6, 0), (8, 0), "+"),
                   (third, (3, 4), (5, 4), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"a": [((0, 0), (6, 0)), ((3, 0), (3, 4))]})
        return circuit, drawing

    def crossing(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        R1(1k) between(a,b);
        R2(2k) between(a,c);
        R3(3k) between(x,y);
        R4(4k) between(x,z);
        """, render_only=True)
        first, second, third, fourth = circuit.components
        symbols = [(first, (0, 0), (0, -2), "+"), (second, (6, 0), (6, -2), "+"),
                   (third, (3, 3), (5, 3), "+"), (fourth, (3, -3), (5, -3), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"a": [((0, 0), (6, 0))], "x": [((3, -3), (3, 3))]})
        mark_crossings(drawing)
        return circuit, set_wire_layer(drawing, "x", pin=(3, 3))

    def test_coverage_spans_collinear_pieces_and_rejects_extension(self):
        self.assertTrue(wire_subset([((0, 0), (3, 0))], [((0, 0), (1, 0)), ((3, 0), (1, 0))]))
        self.assertTrue(wire_subset([((1, 1), (2, 2))], [((0, 0), (3, 3))]))
        self.assertFalse(wire_subset([((0, 0), (3, 0))], [((0, 0), (1, 0)), ((2, 0), (3, 0))]))
        self.assertFalse(wire_subset([((0, 0), (4, 0))], [((0, 0), (3, 0))]))

    def test_pin_branch_transfer_stops_at_junction_and_keeps_union_unchanged(self):
        circuit, original = self.branch()
        before = pack_layout(original)
        back = set_wire_layer(original, "a", pin=(0, 0))
        self.assertEqual(layer_wires(back, underside=True), {"a": [((0, 0), (3, 0))]})
        self.assertTrue(wire_subset([((3, 0), (6, 0)), ((3, 0), (3, 4))], layer_wires(back)["a"]))
        self.assertEqual(back.wires, original.wires)
        self.assertEqual(back.symbols, original.symbols)
        self.assertEqual(pack_layout(original), before)
        self.assertTrue(preserves_nets(circuit, back))
        front = set_wire_layer(back, "a", pin=(0, 0), underside=False)
        self.assertFalse(front.underside_wires)

    def test_clicked_long_wire_selects_only_the_branch_at_the_pointer(self):
        _, original = self.branch()
        back = set_wire_layer(original, "a", edge=((0, 0), (6, 0)), point=(5, 0))
        self.assertEqual(layer_wires(back, underside=True), {"a": [((3, 0), (6, 0))]})

    def test_partial_layer_span_splits_a_merged_union_and_can_return_to_top(self):
        circuit, original = self.branch()
        partial = replace(original, underside_wires={"a": [((1, 0), (2, 0))]})
        self.assertEqual(layer_wires(partial, underside=True), {"a": [((1, 0), (2, 0))]})
        self.assertTrue(wire_subset([((0, 0), (1, 0)), ((2, 0), (6, 0))], layer_wires(partial)["a"]))
        front = set_wire_layer(partial, "a", edge=((1, 0), (2, 0)), point=(1.5, 0), underside=False)
        self.assertFalse(front.underside_wires)
        floating = replace(original, underside_wires={"a": [((1.5, 0), (2.5, 0))]})
        mark_crossings(floating)
        self.assertTrue(preserves_nets(circuit, floating))
        self.assertEqual(layer_wires(floating, underside=True), {"a": [((1.5, 0), (2.5, 0))]})

    def test_back_slide_passes_under_bodies_and_crosses_front_without_moving_it(self):
        circuit, original = self.crossing()
        front = layer_wires(original)
        moved, _ = move_board_wire(circuit, original, "x", ((3, -3), (3, 3)), 1, underside=True)
        self.assertIsNotNone(moved)
        self.assertEqual(layer_wires(moved), front)
        self.assertTrue(any(a[0] == b[0] == 4 for a, b in layer_wires(moved, underside=True)["x"]))
        self.assertTrue(preserves_nets(circuit, moved))
        back = layer_wires(moved, underside=True)["x"]
        self.assertTrue(clear_of_bodies(back, moved))
        self.assertFalse(clear_of_bodies(back, moved, underside=False))
        self.assertFalse(moved.crossings)

    def test_front_slide_cannot_pass_through_a_body_or_foreign_physical_pin(self):
        circuit, original = self.crossing()
        moved, _ = move_board_wire(circuit, original, "a", ((0, 0), (6, 0)), 2.75)
        self.assertIsNone(moved)
        moved, _ = move_board_wire(circuit, original, "a", ((0, 0), (6, 0)), 3)
        self.assertIsNone(moved)

    def test_back_slide_keeps_front_attachment_and_branches_fixed(self):
        circuit, original = self.branch()
        original = set_wire_layer(original, "a", pin=(0, 0))
        front = layer_wires(original)
        moved, _ = move_board_wire(circuit, original, "a", ((0, 0), (3, 0)), -1, underside=True)
        self.assertIsNotNone(moved)
        self.assertEqual(layer_wires(moved), front)
        self.assertTrue(preserves_nets(circuit, moved))
        self.assertTrue(wire_subset([((3, -1), (3, 0))], layer_wires(moved, underside=True)["a"]))

    def test_global_reroute_retains_back_pin_intent_without_recoloring_other_branches(self):
        circuit, original = self.branch()
        original = set_wire_layer(original, "a", pin=(0, 0))
        routed = reroute_perfboard(circuit, original)
        remapped = remap_wire_layers(original, routed)
        self.assertTrue(remapped.underside_wires)
        self.assertTrue(preserves_nets(circuit, remapped))
        self.assertFalse(wire_subset([((3, 0), (3, 4))], layer_wires(remapped, underside=True).get("a", ())))

    def test_same_net_crossing_on_different_faces_needs_a_real_hole_attachment(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        R1(1k) between(a,b);
        R2(2k) between(a,c);
        R3(3k) between(a,d);
        R4(4k) between(a,e);
        """, render_only=True)
        parts = circuit.components
        symbols = [(parts[0], (0, 0), (-2, 0), "+"), (parts[1], (6, 0), (8, 0), "+"),
                   (parts[2], (3, -3), (5, -3), "+"), (parts[3], (3, 3), (5, 3), "+")]
        back = ((3, -3), (3, 3))
        disconnected = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                              wires={"a": [((0, 0), (6, 0)), back]}, underside_wires={"a": [back]})
        mark_crossings(disconnected)
        self.assertFalse(preserves_nets(circuit, disconnected))
        joined = replace(disconnected, wires={"a": [((0, 0), (3, 0)), ((3, 0), (6, 0)), back]})
        mark_crossings(joined)
        self.assertTrue(preserves_nets(circuit, joined))

    def test_explicit_through_hole_endpoint_cannot_touch_a_foreign_back_wire(self):
        circuit, original = self.crossing()
        self.assertTrue(preserves_nets(circuit, original))
        joined = replace(original, wires={**original.wires, "a": [((0, 0), (3, 0)), ((3, 0), (6, 0))]})
        mark_crossings(joined)
        self.assertFalse(preserves_nets(circuit, joined))


if __name__ == "__main__":
    unittest.main()
