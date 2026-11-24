"""Interchangeable identities follow nearby nets without moving physical slots."""

from dataclasses import replace
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.assignment import assign_interchangeable_parts
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.geometry import Layout, routed_pins
from circuitlab.layout.perfboard import reroute_perfboard
from circuitlab.layout.persistence import pack_layout
from circuitlab.layout.wire_edit import physical_wire_islands


class PartAssignmentTest(unittest.TestCase):
    def board(self, count=3):
        source = "perf[A-R,1-24];\n" + "\n".join(
            f"R{i}({i + 1}k) between(a{i},b{i});\nC{i}(100n) between(a{i},b{i});"
            for i in range(count))
        circuit = compile_circuit(source, render_only=True)
        capacitors = (17, 3, 11) if count == 3 else (13, 5)
        anchors = (2, 10, 18) if count == 3 else (2, 16)
        symbols = []
        for part in circuit.components:
            index = int(part.name[1:])
            x = anchors[index] if part.kind == "R" else capacitors[index]
            symbols.append((part, (x, -2), (x, -4), "+"))
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1)
        return circuit, drawing

    def test_global_three_way_matching_keeps_slots_source_objects_and_values(self):
        circuit, original = self.board()
        before = pack_layout(original)
        assigned = assign_interchangeable_parts(circuit, original)
        self.assertEqual([part for part, *_ in assigned.symbols], circuit.components)
        self.assertEqual({name: assigned.positions[name] for name in ("C0", "C1", "C2")},
                         {"C0": (3, -2), "C1": (11, -2), "C2": (17, -2)})
        self.assertEqual(sorted((a, b) for _, a, b, _ in assigned.symbols),
                         sorted((a, b) for _, a, b, _ in original.symbols))
        self.assertEqual(pack_layout(original), before)
        routed = reroute_perfboard(circuit, assigned, reference=original)
        self.assertTrue(preserves_nets(circuit, routed))
        self.assertIs(assign_interchangeable_parts(circuit, assigned), assigned)
        self.assertEqual(assign_interchangeable_parts(circuit, original), assigned)

    def test_component_label_offsets_and_compact_style_follow_the_physical_slots(self):
        circuit, original = self.board()
        original = replace(original, label_offsets={("component", "C0"): (1, 2),
                                                    ("component", "C1"): (-.5, 1)},
                           compact_labels=frozenset({"C0"}))
        assigned = assign_interchangeable_parts(circuit, original)
        self.assertEqual(assigned.label_offsets["component", "C2"], (1, 2))
        self.assertEqual(assigned.label_offsets["component", "C0"], (-.5, 1))
        self.assertEqual(assigned.compact_labels, frozenset({"C2"}))

    def test_nonpolar_terminal_reversal_keeps_source_pin_associations(self):
        circuit, original = self.board(2)
        # Start with identities at their nearby slots, but one physical part reversed.
        symbols = [(part, (5, -4), (5, -2), top) if part.name == "C0"
                   else (part, (13, -2), (13, -4), top) if part.name == "C1"
                   else (part, a, b, top) for part, a, b, top in original.symbols]
        original = replace(original, symbols=symbols, positions={part.name: a for part, a, *_ in symbols})
        assigned = assign_interchangeable_parts(circuit, original)
        symbol = next(symbol for symbol in assigned.symbols if symbol[0].name == "C0")
        self.assertEqual(routed_pins(symbol, 0, 1), (("a0", (5, -2)), ("b0", (5, -4))))
        self.assertEqual(symbol[0], circuit.components[1])

    def test_excluded_or_differently_valued_parts_are_not_interchangeable(self):
        circuit, original = self.board(2)
        excluded = replace(circuit, perfboard=replace(circuit.perfboard, exclusions=("C0",)))
        self.assertIs(assign_interchangeable_parts(excluded, original), original)
        changed = [replace(part, value=200e-9) if part.name == "C1" else part for part in circuit.components]
        different = replace(circuit, components=changed)
        symbols = [(changed[index], a, b, top) for index, (_, a, b, top) in enumerate(original.symbols)]
        drawing = replace(original, symbols=symbols)
        self.assertIs(assign_interchangeable_parts(different, drawing), drawing)
        modeled = [replace(part, model="special") if part.name == "C1" else part for part in circuit.components]
        drawing = replace(original, symbols=[(modeled[index], a, b, top)
                                             for index, (_, a, b, top) in enumerate(original.symbols)])
        self.assertIs(assign_interchangeable_parts(replace(circuit, components=modeled), drawing), drawing)
        for change in ({"kind": "R"}, {"value": None}):
            with self.subTest(change=change):
                parts = [replace(part, **change) if part.name == "C1" else part for part in circuit.components]
                drawing = replace(original, symbols=[(parts[index], a, b, top)
                                                      for index, (_, a, b, top) in enumerate(original.symbols)])
                self.assertIs(assign_interchangeable_parts(replace(circuit, components=parts), drawing), drawing)

    def test_equal_cost_parallel_parts_keep_their_current_identities(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        C0(100n) between(a,b);
        C1(100n) between(a,b);
        """, render_only=True)
        first, second = circuit.components
        symbols = [(first, (3, -2), (3, -4), "+"), (second, (7, -2), (7, -4), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         boundary={"a": (0, -2), "b": (0, -4)})
        for _ in range(3):
            self.assertIs(assign_interchangeable_parts(circuit, drawing), drawing)

    def test_split_returns_are_scored_as_independent_physical_islands(self):
        source = """perf[A-R,1-24];
        R0(1k) between(a0,c0);
        R1(2k) between(a1,c1);
        C0(100n) between(a0,b);
        C1(100n) between(a1,b);
        """
        circuit = compile_circuit(source, render_only=True)
        r0, r1, c0, c1 = circuit.components
        symbols = [(r0, (2, -2), (2, -4), "+"), (r1, (16, -2), (16, -4), "+"),
                   (c0, (13, -2), (13, -4), "+"), (c1, (5, -2), (5, -4), "+")]
        original = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                          wires={"b": [((13, -4), (14, -4)), ((5, -4), (4, -4))]},
                          connectors=[("b", (14, -4), "return", True, False),
                                      ("b", (4, -4), "return", False, True)])
        # Swapping would shorten the inputs but equally lengthen these separate returns.
        self.assertIs(assign_interchangeable_parts(circuit, original), original)
        routed = reroute_perfboard(circuit, original)
        _, islands = physical_wire_islands(routed, "b")
        self.assertNotEqual(islands[(14, -4)], islands[(4, -4)])
        self.assertTrue(preserves_nets(circuit, routed))

    def test_hidden_wiring_reference_stays_unchanged(self):
        circuit, original = self.board(2)
        hidden = replace(original, wires_visible=False,
                         wiring_positions={part.name: (a, b, 0) for part, a, b, _ in original.symbols})
        assigned = assign_interchangeable_parts(circuit, hidden, reference=original)
        self.assertFalse(assigned.wires_visible)
        self.assertEqual(assigned.wiring_positions, hidden.wiring_positions)
        self.assertEqual(assigned.wires, hidden.wires)
        self.assertEqual(original.positions["C0"], (13, -2))


if __name__ == "__main__":
    unittest.main()
