"""Split grounds and reconnect labeled islands without changing electrical nets."""
# Visible islands may reconnect only within the exact same electrical net.
# Splitting and joining wires must handle ground symbols and labels without grounding a sense node.

from copy import deepcopy
from dataclasses import replace
import json
import math
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.automatic import compact_amplifier
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_connector, split_wire
from circuitlab.layout.geometry import Layout
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.wiring import labeled
from circuitlab.math.report import component_labels
from circuitlab.model.graph import Component


class LayoutConnectionsTest(unittest.TestCase):
    def islands(self, count=2):
        source = 'R1(1k) between(a,n);\nR2(2k) between(n,b);'
        if count == 3:
            source += '\nR3(3k) between(n,c);'
        circuit = compile_circuit(source, render_only=True)
        symbols = [(circuit.components[0], (0, 0), (4, 0), '+')]
        symbols += [(part, (0, row * 4), (-4, row * 4), '+')
                    for row, part in enumerate(circuit.components[1:], 1)]
        layout = Layout(symbols=symbols, positions={p.name: a for p, a, *_ in symbols},
                        wires={'n': [((4, 0), (6, 0)), ((0, 4), (4, 4))],
                               'a': [((-4, 0), (0, 0))]},
                        boundary={'a': (-4, 0)},
                        connectors=[('n', (6, 0), 'n', False, False),
                                    ('n', (4, 4), 'n', False, False)])
        if count == 3:
            layout.wires['n'].append(((0, 8), (4, 8)))
            layout.connectors.append(('n', (4, 8), 'n', False, False))
        self.assertTrue(preserves_nets(circuit, layout))
        return circuit, layout

    def grounds(self, count=2):
        circuit = compile_circuit('R1(1k) between(a,0);' + ('\nR2(2k) between(b,0);' if count == 2 else ''), render_only=True)
        symbols = [(part, (0, row * 6), (4, row * 6), '+')
                   for row, part in enumerate(circuit.components)]
        layout = Layout(symbols=symbols, positions={p.name: a for p, a, *_ in symbols},
                        wires={'0': [((4, 0), (4, 6)), ((4, -2), (4, 0))]
                               if count == 2 else [((4, 0), (8, 0))]},
                        grounds=[(4, -2)] if count == 2 else [(8, 0)])
        self.assertTrue(preserves_nets(circuit, layout))
        return circuit, layout

    def assert_roundtrip(self, circuit, layout):
        restored = restore_layout(json.loads(json.dumps(pack_layout(layout))), circuit, layout.boundary)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.wires, layout.wires)
        self.assertEqual(restored.connectors, layout.connectors)
        self.assertEqual(restored.grounds, layout.grounds)
        self.assertEqual(restored.ground_names, layout.ground_names)
        self.assertEqual(restored.ground_rotations, layout.ground_rotations)

    def test_join_interior_of_same_net_removes_both_split_markers(self):
        circuit, original = self.islands()
        before = deepcopy(original)
        joined = move_connector(circuit, original, 0, (2, 4), join_wire=True)
        self.assertIsNotNone(joined)
        self.assertEqual(joined.connectors, [])
        self.assertTrue(preserves_nets(circuit, joined))
        self.assertEqual(original, before)
        self.assertEqual(joined.symbols, original.symbols)
        self.assert_roundtrip(circuit, joined)

    def test_join_three_islands_retains_one_label_per_remaining_island(self):
        circuit, original = self.islands(3)
        joined = move_connector(circuit, original, 0, (2, 4), join_wire=True)
        self.assertIsNotNone(joined)
        self.assertEqual(len(joined.connectors), 2)
        self.assertTrue(preserves_nets(circuit, joined))
        remaining = next(i for i, (_, pos, *_) in enumerate(joined.connectors) if pos == (4, 8))
        joined = move_connector(circuit, joined, remaining, (1, 4), join_wire=True)
        self.assertIsNotNone(joined)
        self.assertEqual(joined.connectors, [])
        self.assertTrue(preserves_nets(circuit, joined))

    def test_join_matching_connector_remains_supported(self):
        circuit, original = self.islands()
        joined = move_connector(circuit, original, 0, original.connectors[1][1], join=1)
        self.assertIsNotNone(joined)
        self.assertEqual(joined.connectors, [])
        self.assertTrue(preserves_nets(circuit, joined))

    def test_rejects_own_stub_different_net_and_empty_space(self):
        circuit, original = self.islands()
        before = deepcopy(original)
        for point in ((5, 0), (-2, 0), (3, 7)):
            with self.subTest(point=point):
                self.assertIsNone(move_connector(circuit, original, 0, point, join_wire=True))
        # 'a' is the wire on the other side of R1: bypassing a resistor is forbidden.
        self.assertNotEqual(circuit.components[0].p, circuit.components[0].n)
        self.assertEqual(original, before)

    def test_opamp_virtual_ground_is_not_a_ground_join_target(self):
        circuit = compile_circuit('V1(1) between(input,0);\nRi(1k) between(input,inv);\n'
                                  'Rf(10k) between(out,inv);\nop1(0,inv,out);', render_only=True)
        original = labeled(circuit, component_labels(circuit))
        self.assertNotIn('inv', circuit.grounds)
        index = next(i for i, (net, *_) in enumerate(original.connectors) if net == 'inv')
        target = original.grounds[0]
        self.assertIsNone(move_connector(circuit, original, index, target, join_wire=True))

    def test_ground_bus_split_uses_local_symbols_and_prunes_old_stub(self):
        circuit, original = self.grounds()
        before = deepcopy(original)
        split = split_wire(circuit, original, '0', ((4, 0), (4, 6)), (4, 3), grid=.25)
        self.assertIsNotNone(split)
        self.assertEqual(split.connectors, [])
        self.assertEqual(len(split.grounds), 2)
        self.assertGreaterEqual(math.dist(*split.grounds), 2)
        self.assertNotIn((4, -2), split.grounds)
        self.assertEqual(set(split.ground_names.values()), {'0'})
        self.assertTrue(preserves_nets(circuit, split))
        self.assertEqual(original, before)
        self.assertEqual(split.symbols, original.symbols)
        self.assert_roundtrip(circuit, split)

    def test_short_ground_split_points_symbols_to_opposite_sides(self):
        circuit, original = self.grounds()
        part, _, _, top = original.symbols[1]
        original.symbols[1] = part, (0, 1), (4, 1), top
        original.positions[part.name] = (0, 1)
        original.wires['0'] = [((4, 0), (4, 1)), ((4, -2), (4, 0))]
        before = deepcopy(original)
        split = split_wire(circuit, original, '0', ((4, 0), (4, 1)), (4, .5), grid=.25)
        self.assertIsNotNone(split)
        self.assertEqual(len(split.grounds), 2)
        lower, upper = sorted(split.grounds, key=lambda point: point[1])
        self.assertEqual(split.ground_rotations[lower], 1)  # Left of lower stub.
        self.assertEqual(split.ground_rotations[upper], 3)  # Right of upper stub.
        self.assertLess(math.dist(lower, upper), 2)
        self.assertTrue(preserves_nets(circuit, split))
        self.assertEqual(original, before)

    def test_ground_lead_split_discards_disconnected_ground_only_tail(self):
        circuit, original = self.grounds(1)
        split = split_wire(circuit, original, '0', ((4, 0), (8, 0)), (6, 0))
        self.assertIsNotNone(split)
        self.assertEqual(len(split.grounds), 1)
        self.assertLess(split.grounds[0][0], 6)
        self.assertEqual(split.connectors, [])
        self.assertTrue(preserves_nets(circuit, split))
        self.assertTrue(all(max(a[0], b[0]) < 6 for a, b in split.wires['0']))

    def test_named_grounds_remain_distinct_when_split_and_restored(self):
        circuit, original = self.grounds()
        circuit.components = [replace(part, n='agnd') for part in circuit.components]
        circuit.grounds = {'agnd', 'dgnd'}
        original.symbols = [(part, *symbol[1:]) for part, symbol in zip(circuit.components, original.symbols)]
        other = Component('R3', 'R', 'other', 'dgnd', 1000)
        circuit.components.append(other)
        original.symbols.append((other, (12, 0), (16, 0), '+'))
        original.positions['R3'] = (12, 0)
        original.wires['agnd'] = original.wires.pop('0')
        original.grounds.append((16, 0))
        original.ground_names = {(4, -2): 'agnd', (16, 0): 'dgnd'}
        self.assertTrue(preserves_nets(circuit, original))
        split = split_wire(circuit, original, 'agnd', ((4, 0), (4, 6)), (4, 3))
        self.assertIsNotNone(split)
        self.assertEqual(split.ground_names[(16, 0)], 'dgnd')
        self.assertEqual(list(split.ground_names.values()).count('agnd'), 2)
        self.assertTrue(preserves_nets(circuit, split))
        self.assert_roundtrip(circuit, split)

    def test_fresh_compact_amplifier_uses_separate_local_grounds(self):
        for source, feedback in [('Ri(1k)', 'Rf(10k)'), ('Zs(2-j2)', 'Zf(4+j4)')]:
            with self.subTest(source=source):
                circuit = compile_circuit(f'Vac(1 Vpk) between(input,0);\nf=60\n'
                                          f'{source} between(input,inv);\n'
                                          f'{feedback} between(out,inv);\nop1(0,inv,out);', render_only=True)
                drawing = compact_amplifier(circuit)
                self.assertIsNotNone(drawing)
                self.assertEqual(len(drawing.grounds), 2)
                self.assertTrue(preserves_nets(circuit, drawing))
                source_symbol = next(symbol for symbol in drawing.symbols if symbol[0].is_source)
                self.assertIn(source_symbol[2], drawing.grounds)
                self.assertTrue(all(abs(a[0] - b[0]) < 1 for a, b in drawing.wires.get('0', ())))

    def test_non_ground_split_keeps_matching_connector_behavior(self):
        circuit = compile_circuit('R1(1k) between(a,n);\nR2(2k) between(n,b);', render_only=True)
        symbols = [(circuit.components[0], (0, 0), (4, 0), '+'),
                   (circuit.components[1], (10, 0), (14, 0), '+')]
        original = Layout(symbols=symbols, positions={p.name: a for p, a, *_ in symbols},
                          wires={'n': [((4, 0), (10, 0))]})
        split = split_wire(circuit, original, 'n', ((4, 0), (10, 0)), (7, 0))
        self.assertIsNotNone(split)
        self.assertEqual(len(split.connectors), 2)
        self.assertEqual(split.grounds, [])
        self.assertTrue(preserves_nets(circuit, split))


if __name__ == '__main__':
    unittest.main()
