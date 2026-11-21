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
from circuitlab.layout.checks import mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.editing import move_connector, split_wire
from circuitlab.layout.geometry import Layout, rotate_point
from circuitlab.layout.ground_clearance import clear_ground_marks, ground_strokes
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.tee_cleanup import reattach_tee_runs
from circuitlab.layout.wiring import labeled
from circuitlab.math.report import component_labels
from circuitlab.model.graph import Component


class LayoutConnectionsTest(unittest.TestCase):
    def test_fresh_ground_marks_face_outward_only_on_clear_horizontal_leads(self):
        for terminals, a, b, ground, turns in (
            ("0,a", (0, 0), (4, 0), (-.5, 0), 1),
            ("a,0", (0, 0), (4, 0), (4.5, 0), 3),
            ("a,0", (0, 4), (0, 0), (0, -.5), 0),
        ):
            with self.subTest(ground=ground):
                circuit = compile_circuit(f'R1(1k) between({terminals});', render_only=True)
                part = circuit.components[0]
                pin = a if part.p == "0" else b
                drawing = Layout(
                    symbols=[(part, a, b, '+')], positions={part.name: a},
                    grounds=[ground], wires={'0': [(pin, ground)]}, pin_grid=.25,
                )
                self.assertIs(clear_ground_marks(circuit, drawing), drawing)
                faced = clear_ground_marks(circuit, drawing, face_leads=True)
                self.assertEqual(faced.ground_rotations.get(ground, 0), turns)
                self.assertEqual(faced.grounds, drawing.grounds)
                self.assertEqual(faced.wires, drawing.wires)
                self.assertTrue(preserves_nets(circuit, faced))
                boundary = replace(drawing, boundary={'0': ground})
                self.assertIs(clear_ground_marks(circuit, boundary, face_leads=True), boundary)

        circuit = compile_circuit('R1(1k) between(a,0);\nR2(1k) between(b,c);', render_only=True)
        first, second = circuit.components
        drawing = Layout(
            symbols=[(first, (0, 0), (4, 0), '+'), (second, (6.5, .25), (10.5, .25), '+')],
            positions={first.name: (0, 0), second.name: (6.5, .25)},
            grounds=[(4.5, 0)], boundary={'b': (4.75, .25)}, pin_grid=.25,
            wires={'0': [((4, 0), (4.5, 0))], 'b': [((4.75, .25), (6.5, .25))]},
        )
        self.assertIs(clear_ground_marks(circuit, drawing, face_leads=True), drawing)

    def test_exclusive_ground_lead_loses_jogs_without_moving_aligned_marker(self):
        circuit = compile_circuit('R1(1k) between(a,0);', render_only=True)
        part = circuit.components[0]
        pin, ground = (0, 0), (0, -1)
        run = [pin, (0, -.25), (.25, -.25), (.25, -.75), (0, -.75), ground]
        drawing = Layout(
            symbols=[(part, (-4, 0), pin, '+')], positions={part.name: (-4, 0)},
            grounds=[ground], wires={'0': list(zip(run, run[1:]))}, pin_grid=.25,
        )
        before = deepcopy(drawing)
        result = clear_ground_marks(circuit, drawing)
        self.assertEqual(drawing, before)
        self.assertEqual(result.grounds, [ground])
        self.assertEqual(result.wires['0'], [(ground, pin)])
        self.assertTrue(preserves_nets(circuit, result))

    def test_optimize_shortens_opamp_ground_jog_without_crossing_input_wire(self):
        circuit = compile_circuit('R1(1k) between(minus,a);\nop1(0,minus,out);', render_only=True)
        resistor, amp = circuit.components
        pin, ground = (0, -.25), (-.75, -2)
        for lane, new_ground, turns in ((-1.5, (-.25, -.25), 1), (-.5, (0, -.5), 0)):
            with self.subTest(input_lane=lane):
                input_wire = [((0, .25), (lane, .25)), ((lane, .25), (lane, -3))]
                drawing = Layout(
                    symbols=[(amp, (0, 0), (4, 0), '-'), (resistor, (lane, -3), (lane, -6), '+')],
                    positions={amp.name: (0, 0), resistor.name: (lane, -3)},
                    grounds=[ground], pin_grid=.25,
                    wires={'0': [(pin, (0, -2)), ((0, -2), ground)], 'minus': input_wire},
                )
                mark_crossings(drawing)
                before = deepcopy(drawing)
                self.assertTrue(preserves_nets(circuit, drawing))
                result = clear_ground_marks(circuit, drawing, opamp_sideways=True)
                self.assertEqual(result.grounds, [new_ground])
                self.assertEqual(result.ground_rotations.get(new_ground, 0), turns)
                self.assertEqual(result.wires['0'], [(new_ground, pin)])
                self.assertEqual(result.wires['minus'], input_wire)
                self.assertFalse(result.crossings)
                self.assertTrue(preserves_nets(circuit, result))
                self.assertEqual(drawing, before)

    def test_ground_bars_clear_crossing_wire_at_each_rotation(self):
        circuit = compile_circuit('R1(1k) between(a,0);\nR2(1k) between(b,c);', render_only=True)
        for turns in range(4):
            with self.subTest(turns=turns):
                def rotate(point):
                    return rotate_point(point, (0, 0), (0, 0), turns)

                first, second = circuit.components
                ground = rotate((0, -1))
                symbols = [(first, rotate((-4, 0)), rotate((0, 0)), '+'),
                           (second, rotate((1, -1)), rotate((5, -1)), '+')]
                wire = [(rotate((1, -1)), rotate((.25, -1))),
                        (rotate((.25, -1)), rotate((.25, -2.25)))]
                drawing = Layout(
                    symbols=symbols, positions={part.name: a for part, a, _, _ in symbols},
                    grounds=[ground], ground_rotations={ground: turns}, pin_grid=.25,
                    boundary={'b': rotate((.25, -2.25))},
                    wires={'0': [(rotate((0, 0)), ground)], 'b': wire},
                )
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertTrue(any(
                    segments_intersect(edge, stroke) for edge in wire for stroke in ground_strokes(ground, turns)
                ))
                before = deepcopy(drawing)
                result = clear_ground_marks(circuit, drawing)
                new_ground = rotate((0, -.5))
                self.assertEqual(result.grounds, [new_ground])
                self.assertEqual(result.ground_rotations, {new_ground: turns})
                self.assertEqual(result.wires['b'], wire)
                self.assertFalse(any(
                    segments_intersect(edge, stroke) for edge in wire for stroke in ground_strokes(new_ground, turns)
                ))
                self.assertTrue(preserves_nets(circuit, result))
                self.assertEqual(drawing, before)
                self.assertIs(clear_ground_marks(circuit, result), result)
                boundary = replace(drawing, boundary={**drawing.boundary, '0': ground})
                self.assertIs(clear_ground_marks(circuit, boundary), boundary)

    def test_tee_detour_reconnects_at_incoming_row_without_moving_ports(self):
        source = "Rfeed(1k) between(n,d);\nRtop(1k) between(n,b);\nRbottom(1k) between(n,c);"
        circuit = compile_circuit(source, render_only=True)
        feed, top, bottom = circuit.components
        symbols = [(feed, (0, 6), (-4, 6), "+"), (top, (10, 8), (10, 11), "+"),
                   (bottom, (10, 0), (10, -3), "+")]
        drawing = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=.25,
            wires={"n": [((0, 6), (8, 6)), ((8, 6), (8, 2)), ((8, 2), (10, 2)),
                         ((10, 0), (10, 2)), ((10, 2), (10, 8))]},
            junctions={"n": [(10, 2)]},
        )
        self.assertTrue(preserves_nets(circuit, drawing))
        before = deepcopy(drawing)
        cleaned = reattach_tee_runs(circuit, drawing)
        self.assertEqual(cleaned.symbols, symbols)
        self.assertEqual(cleaned.wires["n"], [((0, 6), (10, 6)), ((10, 0), (10, 6)), ((10, 6), (10, 8))])
        self.assertEqual(cleaned.junctions, {"n": [(10, 6)]})
        self.assertTrue(preserves_nets(circuit, cleaned))
        self.assertEqual(drawing, before)
        self.assertIs(reattach_tee_runs(circuit, cleaned), cleaned)

        anchored = replace(drawing, boundary={"n": (8, 2)})
        self.assertIs(reattach_tee_runs(circuit, anchored), anchored)
        crowded = compile_circuit(source + "\nRblock(1k) between(x,y);", render_only=True)
        block = crowded.components[-1]
        blocked = replace(drawing, symbols=[*symbols, (block, (9, 5), (9, 7), "+")],
                          positions={**drawing.positions, block.name: (9, 5)})
        self.assertTrue(preserves_nets(crowded, blocked))
        self.assertIs(reattach_tee_runs(crowded, blocked), blocked)

        # A short leg turning directly into a fixed pin can also be removed.
        pin_symbols = [(feed, (8, 6), (4, 6), "+"), *symbols[1:]]
        pin_jog = replace(drawing, symbols=pin_symbols,
                          positions={**drawing.positions, feed.name: (8, 6)},
                          wires={"n": drawing.wires["n"][1:]})
        straight = reattach_tee_runs(circuit, pin_jog)
        self.assertEqual(straight.symbols, pin_symbols)
        self.assertIn(((8, 6), (10, 6)), straight.wires["n"])
        self.assertNotIn(((8, 6), (8, 2)), straight.wires["n"])
        self.assertTrue(preserves_nets(circuit, straight))

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
