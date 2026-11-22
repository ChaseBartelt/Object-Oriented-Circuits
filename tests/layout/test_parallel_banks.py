"""Parallel passive branches share nearby rails and a consistent orientation."""

import unittest
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import clear_of_bodies, clear_package_wires, preserves_nets
from circuitlab.layout.geometry import Layout, routed_pins
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.lead_spacing import TERMINAL_SPANS
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.viewer.schematic import build_schematic_layout


class ParallelBankTest(unittest.TestCase):
    def build(self, source):
        compiled = compile_circuit(source, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        return circuit, build_schematic_layout(circuit, "auto")

    def assert_bank(self, circuit, drawing, names, *, equal_spans=False):
        members = [symbol for symbol in drawing.symbols if symbol[0].name in names]
        self.assertEqual(len(members), len(names))
        pins = [dict(routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid))
                for symbol in members]
        upper_nets, centers, columns = set(), set(), set()
        for symbol, contacts in zip(members, pins):
            part = symbol[0]
            low, high = sorted(contacts.items(), key=lambda entry: entry[1][1])
            self.assertEqual(low[1][0], high[1][0], part.name)
            self.assertLess(low[1][1], high[1][1], part.name)
            self.assertLessEqual(high[1][1] - low[1][1], TERMINAL_SPANS[part.kind] + .25, part.name)
            upper_nets.add(high[0])
            centers.add((low[1][1] + high[1][1]) / 2)
            columns.add(high[1][0])
        self.assertEqual(len(upper_nets), 1)
        self.assertEqual(len(centers), 1)
        self.assertEqual(len(columns), len(names))
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertTrue(clear_package_wires(drawing))
        edges = [edge for route in drawing.wires.values() for edge in route]
        self.assertTrue(clear_of_bodies(edges, drawing))

        if equal_spans:
            left, right = min(columns), max(columns)
            for net in pins[0]:
                rows = {contacts[net][1] for contacts in pins}
                self.assertEqual(len(rows), 1)
                row, = rows
                intervals = sorted((min(a[0], b[0]), max(a[0], b[0]))
                                   for a, b in drawing.wires[net] if a[1] == b[1] == row)
                reach = left
                for start, end in intervals:
                    if start <= reach:
                        reach = max(reach, end)
                self.assertGreaterEqual(reach, right, net)

    def test_standalone_parallel_capacitors_share_horizontal_rails(self):
        circuit, drawing = self.build("""
        V1(5V) between(supply,0);
        Rfeed(1k) between(supply,signal);
        C1(100n) between(signal,0);
        C2(220n) between(signal,0);
        C3(330n) between(signal,0);
        """)
        self.assert_bank(circuit, drawing, {"C1", "C2", "C3"}, equal_spans=True)

    def test_mixed_parallel_parts_preserve_reversed_terminal_declarations(self):
        circuit, drawing = self.build("""
        V1(5V) between(supply,0);
        Rfeed(1k) between(supply,signal);
        R1(10k) between(signal,0);
        C1(100n) between(0,signal);
        R2(20k) between(0,signal);
        """)
        self.assert_bank(circuit, drawing, {"R1", "C1", "R2"})

    def test_generic_package_fallback_keeps_parallel_bank_together(self):
        circuit, drawing = self.build("""
        chip LoadBox(signal, ground, spare) { model = unavailable; }
        V1(5V) between(supply,0);
        main {
        Rfeed(1k) between(supply,signal);
        C1(100n) between(signal,0);
        C2(220n) between(signal,0);
        C3(330n) between(signal,0);
        LoadBox(signal,0,nc);
        }
        """)
        self.assert_bank(circuit, drawing, {"C1", "C2", "C3"}, equal_spans=True)

    def test_parallel_feedback_parts_keep_matching_orientation(self):
        circuit, drawing = self.build("""
        chip Amp(trim1, minus, plus, vn, nc, out, vp, trim2) {
            op1(plus,minus,out,vp,vn);
        }
        Vpos(5V) between(vp,0);
        Vneg(-5V) between(vn,0);
        Vin(1V) between(signal,0);
        main {
        Rin(10k) between(signal,sense);
        Rfeedback(10k) between(out,sense);
        Cfeedback(47p) between(sense,out);
        Rload(10k) between(out,0);
        Amp(trim1,sense,0,vn,nc,out,vp,trim2);
        }
        """)
        self.assert_bank(circuit, drawing, {"Rfeedback", "Cfeedback"})

    def test_actual_headphone_input_banks_are_side_by_side(self):
        path = (Path(__file__).resolve().parents[2] / "examples" / "advanced" / "synth"
                / "HeadphoneJack_DaisySeed3.ckt")
        compiled = compile_circuit(path.read_text(encoding="utf-8"), path=path, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        drawing = build_schematic_layout(circuit, "auto")
        banks = {}
        for part in circuit.components:
            if part.kind == "C":
                banks.setdefault(frozenset(part.terminal_nets), set()).add(part.name)
        banks = [names for names in banks.values() if len(names) > 1]
        self.assertTrue(banks)
        for names in banks:
            self.assert_bank(circuit, drawing, names, equal_spans=True)

    def test_saved_parallel_parts_keep_their_manual_positions(self):
        circuit = compile_circuit("C1(100n) between(a,b);\nC2(220n) between(a,b);", render_only=True)
        first, second = circuit.components
        original = Layout(
            symbols=[(first, (0, 0), (1, 0), "+"), (second, (0, -2), (1, -2), "+")],
            positions={first.name: (0, 0), second.name: (0, -2)},
            wires={"a": [((0, 0), (0, -2))], "b": [((1, 0), (1, -2))]},
        )
        self.assertTrue(preserves_nets(circuit, original))
        self.assertEqual(restore_layout(pack_layout(original), circuit, ()), original)

    def test_series_neighbors_are_not_members_of_parallel_banks(self):
        from circuitlab.layout.parallel_banks import parallel_bank_names

        circuit = compile_circuit("""
        R1(1k) between(input,middle);
        R2(2k) between(middle,out);
        C1(100n) between(middle,out);
        Rload(10k) between(out,0);
        """, render_only=True)
        self.assertEqual(parallel_bank_names(circuit), {"R2", "C1"})


if __name__ == "__main__":
    unittest.main()
