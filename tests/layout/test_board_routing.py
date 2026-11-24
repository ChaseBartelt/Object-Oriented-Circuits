"""Automatic two-face routing shortens fixed boards without changing the circuit."""

from dataclasses import replace
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.board_layers import layer_wires, wire_subset
from circuitlab.layout.board_routing import optimize_board_wiring
from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout
from circuitlab.layout.route_quality import crossing_count, wire_length


class BoardRoutingTest(unittest.TestCase):
    def detour(self, *, foreign_pin=False):
        circuit = compile_circuit("""perf[A-R,1-24];
        Rleft(1k) between(a,b);
        Rright(2k) between(a,c);
        Rblock(3k) between(x,y);
        """, render_only=True)
        left, right, obstacle = circuit.components
        symbols = [(left, (2, -4), (0, -4), "+"), (right, (14, -4), (16, -4), "+"),
                   (obstacle, (8, -4 if foreign_pin else -2), (8, -6), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"a": [((2, -4), (4, -4)), ((4, -4), (4, -8)),
                                      ((4, -8), (14, -8)), ((14, -8), (14, -4))]})
        mark_crossings(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        return circuit, drawing

    def test_reroutes_a_detour_under_the_component_at_fixed_positions(self):
        circuit, original = self.detour()
        before = pack_layout(original)
        optimized = optimize_board_wiring(circuit, original)
        self.assertLess(wire_length(optimized), wire_length(original))
        self.assertEqual(wire_length(optimized), 12)
        self.assertTrue(optimized.underside_wires)
        self.assertEqual(optimized.symbols, original.symbols)
        self.assertEqual(optimized.positions, original.positions)
        self.assertEqual(optimized.rotations, original.rotations)
        self.assertEqual(pack_layout(original), before)
        self.assertTrue(preserves_nets(circuit, optimized))
        self.assertTrue(clear_of_bodies([edge for edges in layer_wires(optimized).values() for edge in edges],
                                        optimized, underside=False))
        self.assertTrue(all(wire_subset(edges, optimized.wires[net]) for net, edges in optimized.underside_wires.items()))

    def test_equal_length_insulated_crossing_uses_both_faces_without_extra_vias(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        Rleft(1k) between(a,left);
        Rright(2k) between(a,right);
        Rtop(3k) between(b,top);
        Rbottom(4k) between(b,bottom);
        """, render_only=True)
        left, right, top, bottom = circuit.components
        symbols = [(left, (3, -7), (1, -7), "+"), (right, (13, -7), (15, -7), "+"),
                   (top, (7, -3), (7, -1), "+"), (bottom, (7, -11), (7, -13), "+")]
        original = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                          wires={"a": [((3, -7), (13, -7))], "b": [((7, -3), (7, -11))]})
        mark_crossings(original)
        self.assertEqual(crossing_count(original), 1)
        optimized = optimize_board_wiring(circuit, original)
        self.assertEqual(wire_length(optimized), 18)
        self.assertEqual(crossing_count(optimized), 0)
        self.assertTrue(layer_wires(optimized))
        self.assertTrue(layer_wires(optimized, underside=True))
        self.assertEqual(sum(map(len, optimized.wires.values())), 2)
        self.assertTrue(preserves_nets(circuit, optimized))

    def test_back_routes_do_not_run_through_foreign_terminal_holes(self):
        circuit, original = self.detour(foreign_pin=True)
        optimized = optimize_board_wiring(circuit, original)
        self.assertLess(wire_length(optimized), wire_length(original))
        self.assertTrue(preserves_nets(circuit, optimized))
        self.assertFalse(any(segments_intersect(((8, -4), (8, -4)), edge) for edge in optimized.wires["a"]))

    def test_already_short_valid_route_is_an_identity_noop(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        Rleft(1k) between(a,b);
        Rright(2k) between(a,c);
        """, render_only=True)
        left, right = circuit.components
        symbols = [(left, (2, -4), (0, -4), "+"), (right, (14, -4), (16, -4), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"a": [((2, -4), (14, -4))]})
        self.assertIs(optimize_board_wiring(circuit, drawing), drawing)

    def test_repair_routes_new_pin_positions_using_the_unchanged_reference(self):
        circuit, original = self.detour()
        moved = [(part, (a[0], a[1] - 6), (b[0], b[1] - 6), top) if part.name == "Rright"
                 else (part, a, b, top) for part, a, b, top in original.symbols]
        drawing = replace(original, symbols=moved, positions={part.name: a for part, a, *_ in moved})
        self.assertFalse(preserves_nets(circuit, drawing))
        optimized = optimize_board_wiring(circuit, drawing, reference=original)
        self.assertEqual(optimized.symbols, moved)
        self.assertTrue(preserves_nets(circuit, optimized))


if __name__ == "__main__":
    unittest.main()
