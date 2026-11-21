"""Regression cases for unnecessary channel detours and reversed passive leads."""
# Compare candidate route lengths while also checking electrical connectivity and clearance.
# A shorter drawing is not an improvement if it detaches a terminal or passes through a body.
import os
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import mark_crossings, preserves_nets
from circuitlab.layout.geometry import Layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.lead_spacing import shorten_symbol_leads
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.shorten import (
    align_passive_tees, clear_of_bodies, compact_spacing, optimize_layout, rotate_detoured_passives,
    straighten, wire_length,
)
from circuitlab.layout.editing import _symbol_bounds, boxes_overlap
from circuitlab.layout.wire_edit import count_wire_bends
from circuitlab.viewer.schematic import SchematicView
from circuitlab.exporters.ltspice import export_schematic
from tests.exporters.test_ltspice import read_connections


def reversed_resistor():
    circuit = compile_circuit('R1(1k) between(a,b);', render_only=True)
    drawing = Layout(symbols=[(circuit.components[0], (0,0), (4,0), '+')],
        positions={'R1': (0,0)}, boundary={'a': (6,0), 'b': (-2,0)}, pin_grid=0.25,
        wires={'a': [((0,0),(-1,0)), ((-1,0),(-1,2)), ((-1,2),(6,2)), ((6,2),(6,0))],
               'b': [((4,0),(5,0)), ((5,0),(5,-2)), ((5,-2),(-2,-2)), ((-2,-2),(-2,0))]})
    mark_crossings(drawing)
    return circuit, drawing


def elevated_part(kind):
    if kind in {'chip', 'circuit'}:
        source = f'{kind} block(a,b) {{\n R1(1k) between(a,b);\n}}\n'
        if kind == 'chip':
            source += 'main { block(a,b); }'
            circuit = next(hood_levels(compile_circuit(source, render_only=True), set()))[1]
        else:
            source += 'chip outer(a,b) {\n block(a,b);\n}\nmain { outer(a,b); }'
            circuit = list(hood_levels(compile_circuit(source, render_only=True), {'@method:0'}))[1][1]
        width = 8
    else:
        circuit = compile_circuit(f'{kind}1(1k) between(a,b);', render_only=True)
        width = 4
    part = circuit.components[0]
    drawing = Layout(symbols=[(part,(0,6),(width,6),'+')], positions={part.name:(0,6)},
        boundary={'a':(-2,0), 'b':(width+2,0)}, pin_grid=.25,
        wires={'a':[((-2,0),(-2,6)),((-2,6),(0,6))],
               'b':[((width,6),(width+2,6)),((width+2,6),(width+2,0))]})
    return circuit, drawing


def spaced_parts():
    circuit = compile_circuit('R1(1k) between(a,b);\nR2(2k) between(b,0);', render_only=True)
    drawing = Layout(
        symbols=[(circuit.components[0], (0,0), (4,0), '+'),
                 (circuit.components[1], (16,-2), (20,-2), '+')],
        positions={'R1': (0,0), 'R2': (16,-2)}, rotations={'R2': 1}, pin_grid=.25,
        wires={'a': [((-1,0),(0,0))], 'b': [((4,0),(18,0))], '0': [((18,-4),(18,-5))]},
        boundary={'a': (-1,0)}, grounds=[(18,-5)], ground_names={(18,-5): '0'},
        ground_rotations={(18,-5): 1}, connectors=[('b', (8,0), 'tap', False, False)],
        labels={'b': (12,0)}, junctions={'b': [(8,0)]},
        label_offsets={('ground',18,-5): (.5,.25), ('connector',8,0): (-.5,.25),
                       ('net','b'): (.25,.5), ('part','R2'): (.5,1)},
    )
    return circuit, drawing


class RouteShorteningTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rotate_between_parallel_tracks_removes_both_terminal_doglegs(self):
        circuit = compile_circuit("R1(1k) between(a,b);", render_only=True)
        original = Layout(
            symbols=[(circuit.components[0], (0, 0), (2, 0), "+")],
            positions={"R1": (0, 0)}, pin_grid=.25,
            boundary={"a": (-4, 3), "b": (-4, -3)},
            connectors=[("a", (6, 3), "a", False, False), ("b", (6, -3), "b", False, False)],
            wires={
                "a": [((-4, 3), (6, 3)), ((-2, 3), (-2, 0)), ((-2, 0), (0, 0))],
                "b": [((-4, -3), (6, -3)), ((4, -3), (4, 0)), ((2, 0), (4, 0))],
            },
        )
        mark_crossings(original)
        result = rotate_detoured_passives(circuit, original)
        self.assertEqual(result.rotations["R1"], 1)
        self.assertEqual(list(result.pins()), [("a", (1, 1)), ("b", (1, -1))])
        self.assertEqual(count_wire_bends(result), 0)
        self.assertLess(wire_length(result), wire_length(original))
        self.assertTrue(preserves_nets(circuit, result))

    def test_automatic_equalizer_keeps_the_primary_signal_path_together(self):
        source = (Path(__file__).resolve().parents[2] / "examples" / "advanced" / "schematics"
                  / "04_impedance_equalizer.ckt")
        circuit = compile_circuit(source.read_text(encoding="utf-8"), path=source, render_only=True)
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit)
        drawing = view.layout
        amps = {part.name: a for part, a, _, _ in drawing.symbols if part.kind == "O"}
        self.assertLess(amps["op1"][0], amps["op2"][0])
        self.assertLess(amps["op2"][0], amps["op4"][0])
        self.assertEqual(amps["op1"][1], amps["op2"][1])
        self.assertEqual(amps["op2"][1], amps["op4"][1])
        self.assertLess(amps["op3"][1], amps["op2"][1])
        self.assertTrue(preserves_nets(circuit, drawing))

    def test_automatic_multivibrator_keeps_buffers_and_feedback_local(self):
        source = (Path(__file__).resolve().parents[2] / "examples" / "advanced" / "schematics"
                  / "05_transistor_multivibrator.ckt")
        circuit = compile_circuit(source.read_text(encoding="utf-8"), path=source, render_only=True)
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit)
        drawing = view.layout
        self.assertLess(wire_length(drawing), 145)
        self.assertFalse(drawing.connectors)
        for net in ("bufferBase1", "bufferBase2"):
            self.assertLessEqual(sum(abs(a[0] - b[0]) + abs(a[1] - b[1])
                                     for a, b in drawing.wires.get(net, ())), 2)
        self.assertTrue(clear_of_bodies([edge for edges in drawing.wires.values() for edge in edges], drawing))
        self.assertTrue(preserves_nets(view.levels[None, "auto"], drawing))

    def test_shortening_a_doubled_back_lead_removes_its_overhanging_tip(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nR2(1k) between(c,a);", render_only=True)
        r1, r2 = circuit.components
        original = Layout(
            symbols=[(r1, (0, 0), (4, 0), "+"), (r2, (-3, 3), (1, 3), "+")],
            positions={"R1": (0, 0), "R2": (-3, 3)}, boundary={"b": (5, 0)}, pin_grid=.25,
            wires={"a": [((0, 0), (1, 0)), ((1, 0), (1, 3))], "b": [((4, 0), (5, 0))]},
        )
        before = deepcopy(original)
        self.assertTrue(preserves_nets(circuit, original))
        result = shorten_symbol_leads(circuit, original, overlaps_only=True)
        self.assertTrue(preserves_nets(circuit, result))
        self.assertEqual(original, before)
        self.assertEqual(result.symbols[1], original.symbols[1])
        self.assertEqual(result.symbols[0][1], (1, 0))
        self.assertEqual(result.wires["a"], [((1, 0), (1, 3))])
        self.assertEqual(result.wires["b"], [((3, 0), (5, 0))])

    def test_optimizer_moves_grounded_stack_as_one_column(self):
        circuit = compile_circuit(
            "Rdriver(1k) between(input,out);\nRother(1k) between(out,other);\n"
            "Rupper(1k) between(out,mid);\nRlower(1k) between(mid,0);\n"
            "Rtap(1k) between(tap,mid);",
            render_only=True,
        )
        pins = [
            ((-4, 6), (0, 6)), ((2, 8), (6, 8)), ((-2, 3), (-2, 0)),
            ((-2, 0), (-2, -3)), ((-8, 0), (-4, 0)),
        ]
        symbols = [(part, a, b, "+") for part, (a, b) in zip(circuit.components, pins)]
        drawing = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=.25,
            grounds=[(-2, -3)],
            wires={
                "out": [((0, 6), (2, 6)), ((2, 6), (2, 8)), ((2, 4), (2, 6)),
                        ((-2, 4), (2, 4)), ((-2, 3), (-2, 4))],
                "mid": [((-4, 0), (-2, 0))],
            },
        )
        mark_crossings(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        result = optimize_layout(circuit, drawing, move_parts=True, flip_budget=0)
        self.assertTrue(preserves_nets(circuit, result))
        self.assertLessEqual(wire_length(result), wire_length(drawing))
        self.assertLess(count_wire_bends(result), count_wire_bends(drawing))
        ends = {part.name: (a, b) for part, a, b, _ in result.symbols}
        upper, middle = ends["Rupper"]
        self.assertEqual(middle, ends["Rlower"][0])
        self.assertEqual(upper[0], ends["Rlower"][1][0])
        self.assertIn(ends["Rlower"][1], result.grounds)
        docked = upper in (ends["Rdriver"][1], ends["Rother"][0])
        straight = any(upper in (a, b) and a[0] == b[0] for a, b in result.wires.get("out", ()))
        self.assertTrue(docked or straight)

    def test_flip_passive_reconnects_both_pins_and_survives_save(self):
        circuit, drawing = reversed_resistor()
        snapshot = deepcopy(drawing)
        result = optimize_layout(circuit, drawing)
        self.assertEqual(wire_length(drawing), 24)
        self.assertEqual(wire_length(result), 4)
        self.assertEqual(result.rotations['R1'], 2)
        for edges in result.wires.values():
            for edge in edges:
                for point in edge:
                    self.assertTrue(all(abs(v / .25 - round(v / .25)) < 1e-6 for v in point))
        self.assertTrue(preserves_nets(circuit, result))
        self.assertEqual(result.symbols, drawing.symbols)
        self.assertEqual(drawing, snapshot)
        restored = restore_layout(pack_layout(result), circuit, tuple(drawing.boundary))
        self.assertIsNotNone(restored)
        self.assertTrue(preserves_nets(circuit, restored))
        self.assertEqual(wire_length(restored), 4)
        export_circuit = replace(circuit, grounds={"b"})
        before, _ = export_schematic(export_circuit, drawing)
        after, _ = export_schematic(export_circuit, restored)
        self.assertEqual(read_connections(before), read_connections(after))

    def test_detour_collapses_but_junction_and_third_terminal_remain(self):
        circuit = compile_circuit('R1(1k) between(a,b);\nR2(2k) between(b,c);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0],(-6,0),(-2,0),'+'),
                                  (circuit.components[1],(0,0),(0,4),'+')],
            boundary={'a':(-7,0), 'b':(2,0), 'c':(0,5)},
            wires={'a':[((-7,0),(-6,0))], 'c':[((0,4),(0,5))],
                   'b':[((-2,0),(-2,-3)),((-2,-3),(0,-3)),((0,-3),(0,0)),((0,0),(2,0))]})
        mark_crossings(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        result = straighten(circuit, drawing)
        self.assertEqual(wire_length(drawing) - wire_length(result), 6)
        self.assertTrue(preserves_nets(circuit, result))
        self.assertEqual(list(result.pins()), list(drawing.pins()))
        self.assertEqual(result.boundary, drawing.boundary)

    def test_stair_step_route_loses_bends_without_increasing_length(self):
        circuit = compile_circuit('R1(1k) between(a,b);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0], (-4,0), (0,0), '+')],
            positions={'R1': (-4,0)}, boundary={'a': (-5,0), 'b': (6,4)}, pin_grid=.25,
            label_offsets={('net', 'b'): (0.5, 0.25)}, labels={'b': (3,2)},
            wires={'a': [((-5,0),(-4,0))],
                   'b': [((0,0),(2,0)), ((2,0),(2,2)), ((2,2),(4,2)),
                         ((4,2),(4,4)), ((4,4),(6,4))]})
        before = deepcopy(drawing)
        result = straighten(circuit, drawing, simplify=True)
        self.assertEqual(wire_length(result), wire_length(drawing))
        self.assertEqual(len(result.wires['b']), 2)
        self.assertEqual(result.label_offsets, drawing.label_offsets)
        self.assertEqual(result.labels, drawing.labels)
        self.assertEqual(drawing, before)
        self.assertTrue(preserves_nets(circuit, result))

    def test_optimize_rebuilds_a_branched_net_as_one_shared_trunk(self):
        circuit = compile_circuit(
            "R1(1k) between(bus,0);\nR2(2k) between(bus,0);\nR3(3k) between(bus,0);",
            render_only=True,
        )
        symbols = [(part, (x, 0), (x, -4), "+") for x, part in zip((0, 4, 8), circuit.components)]
        drawing = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=.25,
            grounds=[b for _, _, b, _ in symbols],
            wires={"bus": [((0, 0), (0, 3)), ((0, 3), (8, 3)),
                           ((4, 0), (4, 3)), ((8, 3), (8, 0))]},
            labels={"bus": (2, 3)},
        )
        mark_crossings(drawing)
        before = deepcopy(drawing)
        result = optimize_layout(circuit, drawing, move_parts=True, flip_budget=0)
        self.assertEqual(drawing, before)
        self.assertLess(wire_length(result), wire_length(drawing) / 2)
        self.assertIn("bus", result.labels)
        self.assertTrue(preserves_nets(circuit, result))
        self.assertTrue(clear_of_bodies(result.wires["bus"], result))

    def test_passive_input_aligns_through_a_tee_at_equal_wire_length(self):
        circuit = compile_circuit(
            "Vin(1) between(a,0);\nR1(1k) between(a,b);\nR2(1k) between(a,0);\n"
            "R3(1k) between(b,0);\nR4(1k) between(b,0);",
            render_only=True,
        )
        starts = [(-3, 0), (3, -1), (1, 2), (10, -3), (11, -1)]
        stops = [(-3, -2), (7, -1), (1, 4), (14, -3), (15, -1)]
        symbols = [(part, a, b, "+") for part, a, b in zip(circuit.components, starts, stops)]
        drawing = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols},
            grounds=[stops[i] for i in (0, 2, 3, 4)], pin_grid=.25, labels={"a": (-1, 0)},
            wires={
                "a": [((-3, 0), (1, 0)), ((1, 0), (1, 2)), ((1, 0), (1, -1)), ((1, -1), (3, -1))],
                "b": [((7, -1), (9, -1)), ((9, -1), (9, -3)), ((9, -3), (10, -3)), ((9, -1), (11, -1))],
            },
        )
        mark_crossings(drawing)
        before = deepcopy(drawing)
        result = align_passive_tees(circuit, drawing)
        self.assertEqual(result.positions, {**drawing.positions, "R1": (3, 0)})
        self.assertIn(((1, 0), (3, 0)), result.wires["a"])
        self.assertIn(((1, 0), (1, 2)), result.wires["a"])
        self.assertEqual(result.labels, drawing.labels)
        self.assertEqual(wire_length(result), wire_length(drawing))
        self.assertEqual(drawing, before)
        self.assertTrue(preserves_nets(circuit, result))
        self.assertTrue(clear_of_bodies([edge for edges in result.wires.values() for edge in edges], result))
        self.assertIs(align_passive_tees(circuit, result), result)

    def test_explicit_optimize_turns_vertical_passive_to_follow_horizontal_flow(self):
        circuit = compile_circuit('R1(1k) between(a,b);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0], (0,-2), (0,2), '+')],
            positions={'R1': (0,-2)}, boundary={'a': (-4,0), 'b': (4,0)}, pin_grid=.25,
            wires={'a': [((-4,0),(-4,-2)), ((-4,-2),(0,-2))],
                   'b': [((0,2),(4,2)), ((4,2),(4,0))]})
        result = optimize_layout(circuit, drawing, move_parts=True)
        self.assertLess(wire_length(result), wire_length(drawing))
        self.assertEqual(result.rotations['R1'] % 2, 1)
        pins = list(result.pins())
        self.assertEqual(pins[0][1][1], pins[1][1][1])
        self.assertTrue(preserves_nets(circuit, result))
        for _, pin in pins:
            self.assertTrue(all(abs(v/.25 - round(v/.25)) < 1e-6 for v in pin))

    def test_shortcut_does_not_cut_through_another_component(self):
        circuit = compile_circuit('R1(1k) between(a,b);\nR2(2k) between(c,d);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0],(-6,0),(-2,0),'+'),
                                  (circuit.components[1],(0,0),(4,0),'+')],
            boundary={'b':(6,0)}, wires={'b':[((-2,0),(-2,3)),((-2,3),(6,3)),((6,3),(6,0))]})
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertEqual(straighten(circuit, drawing), drawing)

    def test_saved_drawing_only_optimized_explicitly_and_can_be_undone(self):
        circuit, drawing = reversed_resistor()
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=drawing)
        self.assertEqual(wire_length(view.layout), 24)
        view.optimize_wiring()
        self.assertEqual(wire_length(view.layout), 4)
        view.undo_placement()
        self.assertEqual(wire_length(view.layout), 24)
        self.assertTrue(preserves_nets(circuit, view.layout))

    def test_shortcut_cannot_touch_an_unrelated_terminal(self):
        circuit = compile_circuit('R1(1k) between(a,b);\nR2(2k) between(c,d);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0],(-6,0),(-2,0),'+'),
                                  (circuit.components[1],(0,0),(0,4),'+')],
            boundary={'b':(6,0)}, wires={'b':[((-2,0),(-2,-3)),((-2,-3),(6,-3)),((6,-3),(6,0))]})
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertEqual(straighten(circuit, drawing), drawing)

    def test_reposition_passives_chips_and_circuit_blocks_without_changing_nets(self):
        for kind in ('R', 'C', 'chip', 'circuit'):
            with self.subTest(kind=kind):
                circuit, drawing = elevated_part(kind)
                snapshot = deepcopy(drawing)
                self.assertTrue(preserves_nets(circuit, drawing))
                result = optimize_layout(circuit, drawing, move_parts=True)
                self.assertNotEqual(result.symbols, drawing.symbols)
                self.assertLess(wire_length(result), wire_length(drawing))
                self.assertTrue(preserves_nets(circuit, result))
                self.assertEqual(result.boundary, drawing.boundary)
                self.assertEqual(result.symbols[0][0], drawing.symbols[0][0])
                self.assertEqual(drawing, snapshot)
                for _, pin in result.pins():
                    self.assertTrue(all(abs(v/.25 - round(v/.25)) < 1e-6 for v in pin))
                restored = restore_layout(pack_layout(result), circuit, tuple(drawing.boundary))
                self.assertEqual(restored.symbols, result.symbols)
                self.assertTrue(preserves_nets(circuit, restored))

    def test_reposition_is_one_undoable_viewer_action(self):
        circuit, drawing = elevated_part('R')
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=drawing)
        view.optimize_wiring()
        self.assertNotEqual(view.layout.symbols, drawing.symbols)
        view.undo_placement()
        self.assertEqual(view.layout, drawing)

    def test_group_compaction_retains_rotated_parts_ports_and_saved_annotations(self):
        circuit, drawing = spaced_parts()
        snapshot = deepcopy(drawing)
        self.assertTrue(preserves_nets(circuit, drawing))
        result = compact_spacing(circuit, drawing)
        self.assertLess(wire_length(result), wire_length(drawing) / 2)
        self.assertLess(result.positions['R2'][0], drawing.positions['R2'][0])
        self.assertEqual(result.rotations, drawing.rotations)
        for (_, a, b, _), (_, old_a, old_b, _) in zip(result.symbols, drawing.symbols):
            self.assertEqual(tuple(b[i] - a[i] for i in (0,1)),
                             tuple(old_b[i] - old_a[i] for i in (0,1)))
        ground, = result.grounds
        connector = result.connectors[0][1]
        self.assertEqual(result.ground_rotations, {ground: 1})
        self.assertEqual(result.ground_names, {ground: '0'})
        self.assertEqual(result.label_offsets[('ground', *ground)], (.5,.25))
        self.assertEqual(result.label_offsets[('connector', *connector)], (-.5,.25))
        self.assertEqual(result.label_offsets[('part', 'R2')], (.5,1))
        self.assertEqual(result.label_offsets[('net', 'b')], (.25,.5))
        self.assertEqual(result.junctions['b'], [connector])
        self.assertTrue(preserves_nets(circuit, result))
        self.assertTrue(clear_of_bodies([e for edges in result.wires.values() for e in edges], result))
        for point in [p for _, p in result.pins()] + [p for edges in result.wires.values() for e in edges for p in e]:
            self.assertTrue(all(abs(v/.25 - round(v/.25)) < 1e-6 for v in point))
        self.assertEqual(drawing, snapshot)
        restored = restore_layout(pack_layout(result), circuit, tuple(drawing.boundary))
        self.assertEqual(pack_layout(restored), pack_layout(result))

    def test_group_movement_only_happens_on_explicit_optimize_and_undo_restores_everything(self):
        circuit, drawing = spaced_parts()
        saved = pack_layout(drawing)
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=drawing)
        self.assertEqual(pack_layout(view.layout), saved)
        view.optimize_wiring()
        self.assertLess(wire_length(view.layout), wire_length(drawing) / 2)
        self.assertNotEqual(view.layout.positions['R2'], drawing.positions['R2'])
        self.assertEqual(len(view.undo_history), 1)
        self.assertTrue(preserves_nets(circuit, view.layout))
        view.undo_placement()
        self.assertEqual(pack_layout(view.layout), saved)

    def test_compaction_does_not_merge_separate_same_net_ports_on_grid(self):
        circuit, drawing = spaced_parts()
        drawing.grounds += [(8,3), (8.25,3)]
        drawing.ground_rotations.update({(8,3): 2, (8.25,3): 3})
        drawing.connectors += [('b', (8,2), 'tap', False, False),
                               ('b', (8.25,2), 'tap', False, False)]
        drawing.wires['0'].append(((6,3),(10,3)))
        drawing.wires['b'].append(((6,2),(10,2)))
        drawing.label_offsets.update({('ground',8,3): (1,0), ('ground',8.25,3): (2,0),
                                      ('connector',8,2): (3,0), ('connector',8.25,2): (4,0)})
        self.assertTrue(preserves_nets(circuit, drawing))
        result = compact_spacing(circuit, drawing)
        self.assertEqual(len(set(result.grounds)), len(set(drawing.grounds)))
        self.assertEqual(len({p for _, p, *_ in result.connectors}), len(drawing.connectors))
        self.assertEqual(sorted(result.ground_rotations.values()), [1,2,3])
        self.assertEqual(sorted(result.label_offsets.values()), sorted(drawing.label_offsets.values()))
        self.assertTrue(preserves_nets(circuit, result))

    def test_disconnected_groups_can_compact_without_reducing_wire_length(self):
        circuit = compile_circuit('R1(1k) between(a,b);\nR2(2k) between(c,d);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0], (0,0), (4,0), '+'),
                                  (circuit.components[1], (20,0), (24,0), '+')], pin_grid=.25)
        result = compact_spacing(circuit, drawing)
        self.assertLess(result.symbols[1][1][0], 10)
        self.assertEqual(wire_length(result), wire_length(drawing))
        self.assertTrue(preserves_nets(circuit, result))

    def test_already_simple_layout_reports_no_change_without_an_undo_entry(self):
        circuit = compile_circuit('R1(1k) between(a,b);', render_only=True)
        drawing = Layout(symbols=[(circuit.components[0], (0,0), (4,0), '+')],
            positions={'R1': (0,0)}, boundary={'a': (-1,0), 'b': (5,0)}, pin_grid=.25,
            wires={'a': [((-1,0),(0,0))], 'b': [((4,0),(5,0))]})
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=drawing)
        messages = []
        view.optimization_finished.connect(messages.append)
        view.optimize_wiring()
        self.assertEqual(view.layout, drawing)
        self.assertFalse(view.undo_history)
        self.assertEqual(len(messages), 1)
        self.assertIn('No safe wiring improvement', messages[0])

    def test_reposition_avoids_other_component_bodies(self):
        circuit, drawing = elevated_part('R')
        blocker = compile_circuit('R2(1k) between(c,d);', render_only=True).components[0]
        circuit = replace(circuit, components=[*circuit.components, blocker])
        drawing = replace(drawing, symbols=[*drawing.symbols, (blocker,(0,0),(4,0),'+')])
        result = optimize_layout(circuit, drawing, move_parts=True)
        self.assertLess(wire_length(result), wire_length(drawing))
        self.assertTrue(preserves_nets(circuit, result))
        bounds = [_symbol_bounds(s, result.rotations.get(s[0].name, 0), body_only=True)
                  for s in result.symbols]
        self.assertFalse(boxes_overlap(*bounds))
        self.assertTrue(clear_of_bodies([e for edges in result.wires.values() for e in edges], result))


if __name__ == '__main__':
    unittest.main()

