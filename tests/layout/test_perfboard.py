"""Physical board placement keeps holes unique and electrical connections intact."""

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import clear_of_bodies, preserves_nets
from circuitlab.layout.geometry import ChipPackage, Layout, routed_pins
from circuitlab.layout.editing import split_wire
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.perfboard import (
    excluded_parts, move_perfboard_parts, perfboard_issues, perfboard_layout,
    reroute_perfboard, resize_perfboard_part, resize_perfboard_parts, rotate_perfboard_part,
    rotate_perfboard_parts, valid_perfboard_layout,
)
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.wire_edit import physical_wire_islands
from circuitlab.model.graph import CircuitError


class PerfboardLayoutTest(unittest.TestCase):
    def build(self, source, path=None):
        compiled = compile_circuit(source, path=path, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        drawing = perfboard_layout(circuit)
        self.assertTrue(valid_perfboard_layout(circuit, drawing))
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertTrue(clear_of_bodies([edge for route in drawing.wires.values() for edge in route], drawing))
        return circuit, drawing

    def test_three_hole_minimum_and_one_hole_width_fit_an_exact_strip(self):
        circuit, drawing = self.build("perf[A-A,1-3];\nR1(10k) between(a,b);")
        part, a, b, _ = drawing.symbols[0]
        self.assertEqual({a, b}, {(0, 0), (2, 0)})
        self.assertEqual(part.value, 10000)

    def test_too_small_board_reports_fit_error_at_declaration(self):
        with self.assertRaises(CircuitError) as caught:
            self.build("\nperf[A-A,1-2];\nR1(10k) between(a,b);")
        self.assertEqual(caught.exception.line, 2)
        self.assertIn("do not fit", str(caught.exception))
        self.assertIn("larger perf range", str(caught.exception))

    def test_editable_small_board_keeps_fitted_and_overflow_parts_visible(self):
        compiled = compile_circuit("perf[A-A,1-3];\nR1(1k) between(a,b);\nR2(2k) between(b,c);", render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        drawing = perfboard_layout(circuit, editable=True)
        self.assertEqual({part.name for part, *_ in drawing.symbols}, {"R1", "R2"})
        first = next(symbol for symbol in drawing.symbols if symbol[0].name == "R1")
        self.assertEqual({first[1], first[2]}, {(0, 0), (2, 0)})
        overflow = next(symbol for symbol in drawing.symbols if symbol[0].name == "R2")
        self.assertTrue(all(x > 2 for _, (x, _) in routed_pins(overflow, 0, 1)))
        self.assertTrue(preserves_nets(circuit, drawing))
        warning, = perfboard_issues(circuit, drawing)
        self.assertEqual((warning.code, warning.line), ("PerfboardFit", 1))
        self.assertIn("outside the perfboard", warning.message)
        self.assertIn("R2", warning.message)

        enlarged = replace(circuit, perfboard=replace(circuit.perfboard, row_end="R", column_end=24))
        fixed = reroute_perfboard(enlarged, drawing)
        self.assertEqual(fixed.positions, drawing.positions)
        self.assertTrue(valid_perfboard_layout(enlarged, fixed))
        self.assertFalse(perfboard_issues(enlarged, fixed))

    def test_editable_routing_failure_retains_every_component_and_connection(self):
        compiled = compile_circuit("perf[A-R,1-24];\nR1(1k) between(a,b);\nR2(2k) between(b,c);", render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        with patch("circuitlab.layout.perfboard._route", side_effect=CircuitError("No wire space.")):
            drawing = perfboard_layout(circuit, editable=True)
        self.assertEqual(len(drawing.symbols), len(circuit.components))
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertIn("b", drawing.wires)

    def test_editable_chip_overflow_does_not_prevent_smaller_parts_from_fitting(self):
        compiled = compile_circuit("""perf[A-C,1-3];
        chip Header(a,b,c,d,e,f,g,h) { model = unavailable; }
        main { Header(n1,n2,n3,n4,n5,n6,n7,n8); R1(1k) between(n1,n2); }
        """, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        drawing = perfboard_layout(circuit, editable=True)
        self.assertEqual(len(drawing.symbols), 2)
        chip = next(symbol for symbol in drawing.symbols if symbol[0].kind == "H")
        resistor = next(symbol for symbol in drawing.symbols if symbol[0].kind == "R")
        self.assertTrue(all(x > 2 for _, (x, _) in routed_pins(chip, 0, 1)))
        self.assertTrue(all(0 <= x <= 2 and -2 <= y <= 0 for _, (x, y) in routed_pins(resistor, 0, 1)))
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertIn("Header", perfboard_issues(circuit, drawing)[0].message)

    def test_routing_warning_does_not_claim_a_correct_footprint_is_wrong(self):
        circuit, drawing = self.build("perf[A-R,1-24];\nR1(1k) between(a,b);\nR2(2k) between(b,c);")
        broken = replace(drawing, wires={})
        warning, = perfboard_issues(circuit, broken)
        self.assertEqual(warning.code, "PerfboardRouting")
        self.assertNotIn("distinct holes", warning.message)

    def movable_chain(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        R1(1k) between(a,b);
        R2(2k) between(b,c);
        R3(3k) between(c,d);
        """, render_only=True)
        first, second, third = circuit.components
        symbols = [(first, (2, -2), (4, -2), "+"),
                   (second, (8, -2), (10, -2), "+"),
                   (third, (8, -6), (10, -6), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols},
                         wires={"b": [((4, -2), (8, -2))],
                                "c": [((10, -2), (12, -2)), ((12, -2), (12, -4)),
                                      ((12, -4), (6, -4)), ((6, -4), (6, -6)),
                                      ((6, -6), (8, -6))]}, pin_grid=1)
        self.assertTrue(valid_perfboard_layout(circuit, drawing))
        return circuit, drawing

    def test_dragging_one_resistor_preserves_other_tracks_and_uses_only_right_angles(self):
        circuit, original = self.movable_chain()
        moved = move_perfboard_parts(circuit, original, {"R1"}, (0, -2))
        self.assertEqual(moved.symbols[1:], original.symbols[1:])
        self.assertEqual(moved.wires["c"], original.wires["c"])
        self.assertEqual(original.wires["b"], [((4, -2), (8, -2))])
        self.assertEqual(moved.positions["R1"], (2, -4))
        self.assertTrue(valid_perfboard_layout(circuit, moved))
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1]
                            for edges in moved.wires.values() for a, b in edges))

    def test_group_drag_translates_internal_wires_without_reorganizing_them(self):
        circuit, original = self.movable_chain()
        moved = move_perfboard_parts(circuit, original, {"R2", "R3"}, (1, -1))
        self.assertEqual(moved.symbols[0], original.symbols[0])
        self.assertEqual(moved.wires["c"], [((a[0] + 1, a[1] - 1), (b[0] + 1, b[1] - 1))
                                           for a, b in original.wires["c"]])
        self.assertTrue(valid_perfboard_layout(circuit, moved))

    def test_drag_does_not_rotate_parts_or_replace_wires_when_the_board_is_invalid(self):
        circuit, original = self.movable_chain()
        original = replace(original, rotations={"R1": 1})
        moved = move_perfboard_parts(circuit, original, {"R1"}, (25, 0))
        self.assertEqual(moved.rotations, original.rotations)
        self.assertEqual(moved.wires["c"], original.wires["c"])
        self.assertEqual(len(moved.symbols), len(circuit.components))
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1]
                            for edges in moved.wires.values() for a, b in edges))
        self.assertIn("PerfboardFit", {issue.code for issue in perfboard_issues(circuit, moved)})

    def test_hybrid_stereo_resistor_drag_does_not_turn_the_board_into_a_ratsnest(self):
        path = Path(__file__).resolve().parents[2] / "examples" / "advanced" / "synth" / "HYBRID01_FPGA_Daisy_LT1677_Stereo.ckt"
        compiled = compile_circuit(path.read_text(encoding="utf-8"), path=path, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        original = perfboard_layout(circuit, editable=True)
        resistor = next(symbol for symbol in original.symbols if symbol[0].name == "RbiasTop")
        affected = {net for net, _ in routed_pins(resistor, 0, 1)}
        moved = move_perfboard_parts(circuit, original, {"RbiasTop"}, (0, -1))
        self.assertEqual(len(moved.symbols), len(original.symbols))
        self.assertEqual(moved.rotations, original.rotations)
        for net, edges in original.wires.items():
            if net not in affected:
                self.assertEqual(moved.wires[net], edges, net)
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1]
                            for edges in moved.wires.values() for a, b in edges))

    def split_chain(self, grid=.5, net="b"):
        circuit = compile_circuit(f"perf[A-R,1-24]; R1(1k) between(a,{net}); R2(2k) between({net},c);",
                                  render_only=True)
        first, second = circuit.components
        symbols = [(first, (2, -2), (4, -2), "+"), (second, (8, -2), (10, -2), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols},
                         wires={net: [((4, -2), (8, -2))]}, pin_grid=1)
        split = split_wire(circuit, drawing, net, ((4, -2), (8, -2)), (6, -2), grid=grid)
        self.assertIsNotNone(split)
        return circuit, split

    def test_optimizing_split_wires_preserves_separate_label_islands_and_exact_endpoints(self):
        for grid in (.5, .25, 0):
            with self.subTest(grid=grid):
                circuit, original = self.split_chain(grid)
                optimized = reroute_perfboard(circuit, original)
                self.assertEqual(optimized.connectors, original.connectors)
                left, right = [item[1] for item in original.connectors]
                _, islands = physical_wire_islands(optimized, "b")
                self.assertNotEqual(islands[left], islands[right])
                self.assertEqual(islands[(4, -2)], islands[left])
                self.assertEqual(islands[(8, -2)], islands[right])
                self.assertTrue(preserves_nets(circuit, optimized))

    def test_moved_part_stays_with_its_split_label_even_when_the_other_label_is_nearer(self):
        circuit, original = self.split_chain()
        moved = move_perfboard_parts(circuit, original, {"R1"}, (8, -4))
        self.assertEqual(moved.connectors, original.connectors)
        left, right = [item[1] for item in original.connectors]
        _, islands = physical_wire_islands(moved, "b")
        self.assertEqual(islands[(12, -6)], islands[left])
        self.assertNotEqual(islands[left], islands[right])
        right_lead = next(edge for edge in original.wires["b"] if (8, -2) in edge)
        self.assertIn(right_lead, moved.wires["b"])
        self.assertTrue(preserves_nets(circuit, moved))

    def test_local_rotation_preserves_other_routes_and_split_label_positions(self):
        circuit, original = self.movable_chain()
        rotated = rotate_perfboard_part(circuit, original, "R1", 1)
        self.assertEqual(rotated.wires["c"], original.wires["c"])
        self.assertEqual(rotated.symbols[1:], original.symbols[1:])
        self.assertEqual(rotated.rotations, {"R1": 1})
        self.assertTrue(preserves_nets(circuit, rotated))
        circuit, original = self.split_chain()
        rotated = rotate_perfboard_part(circuit, original, "R1", 1)
        self.assertEqual(rotated.connectors, original.connectors)
        left, right = [item[1] for item in original.connectors]
        _, islands = physical_wire_islands(rotated, "b")
        self.assertEqual(islands[(3, -3)], islands[left])
        self.assertNotEqual(islands[left], islands[right])
        self.assertTrue(preserves_nets(circuit, rotated))

    def test_optimizing_split_ground_preserves_separate_ground_branches(self):
        circuit, original = self.split_chain(net="0")
        optimized = reroute_perfboard(circuit, original)
        self.assertEqual(optimized.grounds, original.grounds)
        self.assertEqual(optimized.ground_rotations, original.ground_rotations)
        _, islands = physical_wire_islands(optimized, "0")
        self.assertNotEqual(islands[(4, -2)], islands[(8, -2)])
        self.assertTrue(preserves_nets(circuit, optimized))

    def test_hidden_rotation_keeps_saved_wiring_then_restores_the_correct_split_island(self):
        circuit, original = self.split_chain()
        hidden = replace(original, wires_visible=False,
                         wiring_positions={part.name: (a, b, 0) for part, a, b, _ in original.symbols})
        rotated = rotate_perfboard_part(circuit, hidden, "R1", 1, route=False)
        self.assertEqual(rotated.wires, original.wires)
        self.assertEqual(rotated.connectors, original.connectors)
        self.assertEqual(rotated.wiring_positions, hidden.wiring_positions)
        self.assertFalse(rotated.wires_visible)
        self.assertFalse(perfboard_issues(circuit, rotated))
        self.assertTrue(valid_perfboard_layout(circuit, rotated))
        shown = reroute_perfboard(circuit, replace(rotated, wires_visible=True), reference=original)
        left, right = [item[1] for item in original.connectors]
        _, islands = physical_wire_islands(shown, "b")
        self.assertEqual(islands[(3, -3)], islands[left])
        self.assertNotEqual(islands[left], islands[right])
        self.assertTrue(preserves_nets(circuit, shown))

    def test_split_labels_and_routes_can_stay_outside_both_board_edges(self):
        circuit, original = self.split_chain()
        left, right = original.connectors
        external = replace(original, connectors=[(left[0], (-3, -4), *left[2:]),
                                                  (right[0], (35, -4), *right[2:])],
                           wires={"b": [((4, -2), (4, -4)), ((4, -4), (-3, -4)),
                                         ((8, -2), (8, -4)), ((8, -4), (35, -4))]})
        optimized = reroute_perfboard(circuit, external)
        self.assertEqual(optimized.connectors, external.connectors)
        _, islands = physical_wire_islands(optimized, "b")
        self.assertEqual(islands[(4, -2)], islands[(-3, -4)])
        self.assertEqual(islands[(8, -2)], islands[(35, -4)])
        self.assertNotEqual(islands[(-3, -4)], islands[(35, -4)])
        self.assertTrue(preserves_nets(circuit, optimized))

    def test_unknown_exclusion_names_are_diagnosed(self):
        with self.assertRaises(CircuitError) as caught:
            self.build("perf[A-R,1-24] exclude[WrongSource];\nR1(10k) between(a,b);")
        self.assertIn('exclusion "WrongSource"', str(caught.exception))
        self.assertEqual(caught.exception.line, 1)
        circuit = compile_circuit("perf[A-R,1-24] exclude[WrongSource];\nR1(10k) between(a,b);", render_only=True)
        with self.assertRaises(CircuitError):
            perfboard_layout(circuit, editable=True)

    def test_named_batteries_are_kept_off_board_by_their_labels(self):
        path = Path(__file__).resolve().parents[2] / "examples" / "perfboard.ckt"
        circuit, drawing = self.build(path.read_text(encoding="utf-8"), path=path)
        names = excluded_parts(circuit)
        self.assertEqual(names, {"Vbattery1", "Vbattery2", "Vwavegen"})
        self.assertEqual(len(drawing.symbols), len(circuit.components))
        for symbol in drawing.symbols:
            if symbol[0].name in names:
                self.assertTrue(all(y > .5 for _, (_, y) in routed_pins(symbol, 0, 1)))

    def test_quad_packages_with_unequal_side_counts_use_one_hole_per_pin(self):
        for count in (33, 40):
            with self.subTest(count=count):
                pins = ",".join(f"pin{i}" for i in range(count))
                circuit, drawing = self.build(
                    f"perf[A-R,1-24];\nchip Board({pins}) {{ model = unavailable; }} "
                    f"\nmain {{ Board({pins}); }}")
                symbol, = drawing.symbols
                self.assertIsInstance(symbol[0], ChipPackage)
                contacts = [at for _, at in routed_pins(symbol, 0, drawing.pin_grid)]
                self.assertEqual(len(contacts), count)
                self.assertEqual(len(set(contacts)), count)
                self.assertTrue(all(value == round(value) for at in contacts for value in at))

    def test_eight_argument_audio_amplifier_contacts_are_distinct_holes(self):
        circuit, drawing = self.build(
            "perf[A-R,1-24];\nop1(plus,minus,out,vp,vn,gain1,gain8,bypass);")
        symbol, = drawing.symbols
        self.assertEqual(len(set(at for _, at in routed_pins(symbol, 0, 1))), 8)

    def test_large_excluded_chip_remains_entirely_outside_the_board(self):
        pins = ",".join(f"pin{i}" for i in range(40))
        circuit, drawing = self.build(
            f"perf[A-R,1-24] exclude[ExternalBoard];\n"
            f"chip ExternalBoard({pins}) {{ model = unavailable; }}\n"
            f"main {{ ExternalBoard({pins}); R1(10k) between(pin0,pin1); }}")
        symbol = next(symbol for symbol in drawing.symbols if symbol[0].kind == "H")
        self.assertTrue(all(y > .5 for _, (_, y) in routed_pins(symbol, 0, 1)))

    def test_transistor_contacts_keep_unique_holes(self):
        circuit, drawing = self.build("perf[A-R,1-24];\ntransQ1(base,collector,0,npn);")
        symbol, = drawing.symbols
        self.assertEqual(len(set(at for _, at in routed_pins(symbol, 0, 1))), 3)

    def test_current_headphone_circuit_with_parallel_capacitors_fits(self):
        path = (Path(__file__).resolve().parents[2] / "examples" / "advanced" / "synth" / "test2.ckt")
        source = "perf[A-R,1-24] exclude[Vwavegen, Negative, Positive];\n" + path.read_text(encoding="utf-8")
        circuit, drawing = self.build(source, path=path)
        capacitors = [symbol for symbol in drawing.symbols if symbol[0].name in {"C1", "C2", "C3"}]
        self.assertEqual(len(capacitors), 3)
        pins = [dict(routed_pins(symbol, 0, 1)) for symbol in capacitors]
        self.assertEqual(len({list(pin.values())[0][0] for pin in pins}), 3)
        for net in pins[0]:
            self.assertEqual(len({pin[net][1] for pin in pins}), 1)

    def test_validation_rejects_old_scale_and_board_overlap(self):
        circuit, drawing = self.build("perf[A-R,1-24];\nR1(1k) between(a,b);\nR2(2k) between(b,c);")
        first, second = drawing.symbols
        short = replace(drawing, symbols=[(first[0], first[1], (first[1][0] + 1, first[1][1]), first[3]), second])
        overlap = replace(drawing, symbols=[first, (second[0], first[1], first[2], second[3])])
        self.assertFalse(valid_perfboard_layout(circuit, short))
        self.assertFalse(valid_perfboard_layout(circuit, overlap))


class PerfboardPartLengthTest(unittest.TestCase):
    def capacitor(self, a=(8, -9), b=(10, -9), turns=0):
        circuit = compile_circuit("perf[A-R,1-24]; C1(100n) between(a,b);", render_only=True)
        part, = circuit.components
        drawing = Layout(symbols=[(part, a, b, "+")], positions={part.name: a}, pin_grid=1,
                         rotations={part.name: turns} if turns else {})
        return circuit, drawing

    def test_seven_hole_capacitor_keeps_first_contact_in_every_orientation(self):
        for a, b in (((8, -9), (10, -9)), ((10, -9), (8, -9)),
                     ((8, -9), (8, -11)), ((8, -11), (8, -9))):
            for turns in range(4):
                with self.subTest(a=a, b=b, turns=turns):
                    circuit, original = self.capacitor(a, b, turns)
                    before = tuple(original.pins())
                    resized = resize_perfboard_part(circuit, original, "C1", 7)
                    after = tuple(resized.pins())
                    self.assertEqual(after[0], before[0])
                    self.assertEqual([net for net, _ in after], ["a", "b"])
                    self.assertEqual(resized.rotations, original.rotations)
                    first, second = [at for _, at in after]
                    self.assertEqual(abs(first[0] - second[0]) + abs(first[1] - second[1]), 6)
                    self.assertEqual(resized.symbols[0][0], circuit.components[0])
                    self.assertAlmostEqual(resized.symbols[0][0].value, 100e-9)
                    self.assertTrue(preserves_nets(circuit, resized))

    def test_shrink_and_even_length_keep_rotated_anchor_and_noop_keeps_identity(self):
        circuit, original = self.capacitor(turns=1)
        self.assertIs(resize_perfboard_part(circuit, original, "C1", 3), original)
        large = resize_perfboard_part(circuit, original, "C1", 7)
        self.assertIs(resize_perfboard_part(circuit, large, "C1", 7), large)
        even = resize_perfboard_part(circuit, large, "C1", 4)
        first, second = [at for _, at in even.pins()]
        self.assertEqual(first, tuple(original.pins())[0][1])
        self.assertEqual(abs(first[0] - second[0]) + abs(first[1] - second[1]), 3)
        self.assertTrue(all(value == round(value) for at in (first, second) for value in at))
        small = resize_perfboard_part(circuit, even, "C1", 3)
        self.assertEqual(small.symbols, original.symbols)

    def test_invalid_length_or_part_reports_declaration_location(self):
        circuit, drawing = self.capacitor()
        for length in (0, 2, 257, 7.0, "7", True, None):
            with self.subTest(length=length), self.assertRaises(CircuitError) as caught:
                resize_perfboard_part(circuit, drawing, "C1", length)
            self.assertEqual(caught.exception.line, 1)
        with self.assertRaises(CircuitError):
            resize_perfboard_part(circuit, drawing, "Missing", 7)
        circuit = compile_circuit("perf[A-R,1-24]; op1(a,b,out);", render_only=True)
        part, = circuit.components
        drawing = Layout(symbols=[(part, (8, -9), (10, -9), "+")], pin_grid=1)
        with self.assertRaises(CircuitError):
            resize_perfboard_part(circuit, drawing, part.name, 7)

    def wired_capacitor(self):
        circuit = compile_circuit("""perf[A-R,1-24]; C1(100n) between(a,b);
        R2(1k) between(b,c);
        R3(2k) between(c,d);""", render_only=True)
        capacitor, second, third = circuit.components
        symbols = [(capacitor, (2, -4), (4, -4), "+"),
                   (second, (12, -4), (14, -4), "+"), (third, (18, -8), (20, -8), "+")]
        edge = (4, -4), (12, -4)
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"b": [edge], "c": [((14, -4), (16, -4)), ((16, -4), (16, -8)),
                                                     ((16, -8), (18, -8))]})
        return circuit, split_wire(circuit, drawing, "b", edge, (8, -4), grid=.5)

    def test_resize_reconnects_only_its_lead_and_retains_split_branches(self):
        circuit, original = self.wired_capacitor()
        resized = resize_perfboard_part(circuit, original, "C1", 7)
        self.assertEqual(resized.symbols[1:], original.symbols[1:])
        self.assertEqual(resized.wires["c"], original.wires["c"])
        self.assertEqual(resized.connectors, original.connectors)
        left, right = [item[1] for item in original.connectors]
        _, islands = physical_wire_islands(resized, "b")
        self.assertEqual(islands[(8, -4)], islands[left])
        self.assertNotEqual(islands[left], islands[right])
        self.assertIn(next(edge for edge in original.wires["b"] if (12, -4) in edge), resized.wires["b"])
        self.assertTrue(valid_perfboard_layout(circuit, resized))

    def test_hidden_resize_persists_old_reference_for_add_wires(self):
        circuit, original = self.wired_capacitor()
        hidden = replace(original, wires_visible=False,
                         wiring_positions={part.name: (a, b, 0) for part, a, b, _ in original.symbols})
        resized = resize_perfboard_part(circuit, hidden, "C1", 7, route=False)
        self.assertEqual(resized.wires, original.wires)
        self.assertEqual(resized.wiring_positions, hidden.wiring_positions)
        self.assertFalse(resized.wires_visible)
        restored = restore_layout(pack_layout(resized), circuit, (), editable_perfboard=True)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.symbols, resized.symbols)
        self.assertEqual(restored.wiring_positions, hidden.wiring_positions)
        shown = reroute_perfboard(circuit, replace(restored, wires_visible=True), reference=original)
        left, right = [item[1] for item in original.connectors]
        _, islands = physical_wire_islands(shown, "b")
        self.assertEqual(islands[(8, -4)], islands[left])
        self.assertNotEqual(islands[left], islands[right])
        self.assertTrue(preserves_nets(circuit, shown))

    def parallel_capacitors(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        C1(100n) between(a,b);
        C2(200n) between(a,b);
        C3(300n) between(a,b);
        Rguard(1k) between(c,d);
        Rend(2k) between(d,e);
        """, render_only=True)
        first, second, third, guard, end = circuit.components
        symbols = [(first, (4, -4), (4, -6), "+"), (second, (6, -4), (6, -6), "+"),
                   (third, (8, -4), (8, -6), "+"), (guard, (14, -4), (16, -4), "+"),
                   (end, (14, -8), (16, -8), "+")]
        drawing = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                         wires={"a": [((4, -4), (8, -4))], "b": [((4, -6), (8, -6))],
                                "d": [((16, -4), (18, -4)), ((18, -4), (18, -6)),
                                      ((18, -6), (12, -6)), ((12, -6), (12, -8)), ((12, -8), (14, -8))]})
        self.assertTrue(valid_perfboard_layout(circuit, drawing))
        return circuit, drawing

    def test_three_parallel_capacitors_resize_together_and_shift_the_shared_bus(self):
        circuit, original = self.parallel_capacitors()
        selected = {"C3", "C1", "C2"}
        resized = resize_perfboard_parts(circuit, original, selected, 7)
        for before, after in zip(original.symbols[:3], resized.symbols[:3]):
            self.assertEqual(routed_pins(before, 0, 1)[0], routed_pins(after, 0, 1)[0])
            self.assertEqual(after[2], (before[2][0], -10))
            self.assertEqual(after[0], before[0])
        self.assertEqual(resized.symbols[3:], original.symbols[3:])
        self.assertEqual(resized.wires["a"], original.wires["a"])
        self.assertEqual(resized.wires["d"], original.wires["d"])
        self.assertEqual(resized.wires["b"], [((4, -10), (8, -10))])
        self.assertTrue(valid_perfboard_layout(circuit, resized))
        self.assertTrue(preserves_nets(circuit, resized))

    def test_batch_noop_and_invalid_request_leave_the_original_untouched(self):
        circuit, original = self.parallel_capacitors()
        before = pack_layout(original)
        self.assertIs(resize_perfboard_parts(circuit, original, (), 7), original)
        self.assertIs(resize_perfboard_parts(circuit, original, {"C1", "C2", "C3"}, 3), original)
        for names, length in (({"C1", "Missing"}, 7), ({"C1", "C2"}, 2)):
            with self.subTest(names=names, length=length), self.assertRaises(CircuitError):
                resize_perfboard_parts(circuit, original, names, length)
            self.assertEqual(pack_layout(original), before)

    def test_batch_mixed_rotations_keep_each_first_physical_contact(self):
        circuit = compile_circuit("""perf[A-R,1-24];
        C1(100n) between(a1,b1);
        C2(200n) between(a2,b2);
        C3(300n) between(a3,b3);
        """, render_only=True)
        symbols = [(part, (4 + index * 7, -7), (6 + index * 7, -7), "+")
                   for index, part in enumerate(circuit.components)]
        original = Layout(symbols=symbols, positions={part.name: a for part, a, *_ in symbols}, pin_grid=1,
                          rotations={"C2": 1, "C3": 3})
        resized = resize_perfboard_parts(circuit, original, {"C1", "C2", "C3"}, 7)
        self.assertEqual(resized.rotations, original.rotations)
        for before, after in zip(original.symbols, resized.symbols):
            turns = original.rotations.get(before[0].name, 0)
            old_pins, pins = routed_pins(before, turns, 1), routed_pins(after, turns, 1)
            self.assertEqual(pins[0], old_pins[0])
            self.assertEqual(sum(abs(a - b) for a, b in zip(pins[0][1], pins[1][1])), 6)
        self.assertTrue(preserves_nets(circuit, resized))

    def test_hidden_batch_keeps_original_bus_and_reference_until_add_wires(self):
        circuit, original = self.parallel_capacitors()
        hidden = replace(original, wires_visible=False,
                         wiring_positions={part.name: (a, b, 0) for part, a, b, _ in original.symbols})
        resized = resize_perfboard_parts(circuit, hidden, {"C1", "C2", "C3"}, 7, route=False)
        self.assertFalse(resized.wires_visible)
        self.assertEqual(resized.wires, original.wires)
        self.assertEqual(resized.wiring_positions, hidden.wiring_positions)
        shown = reroute_perfboard(circuit, replace(resized, wires_visible=True), reference=original)
        self.assertTrue(all(a[1] == b[1] == -10 for a, b in shown.wires["b"]))
        self.assertTrue(preserves_nets(circuit, shown))

    def rotating_capacitors(self):
        circuit, original = self.parallel_capacitors()
        symbols = [(part, (a[0] + index * 2, a[1]), (b[0] + index * 2, b[1]), top)
                   if index < 3 else (part, a, b, top)
                   for index, (part, a, b, top) in enumerate(original.symbols)]
        symbols[3:] = [(symbols[3][0], (18, -2), (20, -2), "+"),
                       (symbols[4][0], (18, -12), (20, -12), "+")]
        drawing = replace(original, symbols=symbols, positions={part.name: a for part, a, *_ in symbols},
                          wires={**original.wires, "a": [((4, -4), (12, -4))],
                                 "b": [((4, -6), (12, -6))],
                                 "d": [((20, -2), (22, -2)), ((22, -2), (22, -10)),
                                       ((22, -10), (16, -10)), ((16, -10), (16, -12)),
                                       ((16, -12), (18, -12))]})
        self.assertTrue(valid_perfboard_layout(circuit, drawing))
        return circuit, drawing

    def test_batch_rotation_reconnects_parallel_rails_without_shorting_them(self):
        circuit, original = self.rotating_capacitors()
        for turns in (1, -1):
            with self.subTest(turns=turns):
                rotated = rotate_perfboard_parts(circuit, original, {"C1", "C2", "C3"}, turns)
                self.assertEqual(rotated.rotations, {"C1": turns % 4, "C2": turns % 4, "C3": turns % 4})
                self.assertEqual(rotated.symbols[3:], original.symbols[3:])
                self.assertEqual(rotated.wires["d"], original.wires["d"])
                self.assertTrue(preserves_nets(circuit, rotated))
                self.assertTrue(clear_of_bodies([edge for route in rotated.wires.values() for edge in route], rotated))
                for index, symbol in enumerate(rotated.symbols[:3]):
                    pins = routed_pins(symbol, turns % 4, 1)
                    center = 4 + index * 4
                    self.assertEqual(pins[0][1], (center + turns, -5))
                    self.assertEqual(pins[1][1], (center - turns, -5))

    def test_four_batch_quarter_turns_restore_part_geometry_and_keep_nets_valid(self):
        circuit, original = self.rotating_capacitors()
        rotated = original
        for _ in range(4):
            rotated = rotate_perfboard_parts(circuit, rotated, {"C1", "C2", "C3"}, 1)
            self.assertTrue(preserves_nets(circuit, rotated))
        self.assertEqual(rotated.symbols, original.symbols)
        self.assertEqual(rotated.positions, original.positions)
        self.assertEqual(rotated.rotations, original.rotations)
        self.assertEqual(rotated.wires["d"], original.wires["d"])
        self.assertIs(rotate_perfboard_parts(circuit, original, {"C1", "C2"}, 4), original)
        self.assertIs(rotate_perfboard_parts(circuit, original, (), 1), original)

    def test_batch_rotation_preserves_split_islands_and_hidden_wiring(self):
        circuit, original = self.rotating_capacitors()
        original = split_wire(circuit, original, "b", ((4, -6), (12, -6)), (6, -6), grid=.5)
        rotated = rotate_perfboard_parts(circuit, original, {"C1", "C2", "C3"}, 1)
        self.assertEqual(rotated.connectors, original.connectors)
        left, right = [item[1] for item in original.connectors]
        _, islands = physical_wire_islands(rotated, "b")
        self.assertEqual(islands[(3, -5)], islands[left])
        self.assertEqual(islands[(7, -5)], islands[right])
        self.assertEqual(islands[(11, -5)], islands[right])
        self.assertNotEqual(islands[left], islands[right])
        self.assertTrue(preserves_nets(circuit, rotated))
        hidden = replace(original, wires_visible=False,
                         wiring_positions={part.name: (a, b, 0) for part, a, b, _ in original.symbols})
        preview = rotate_perfboard_parts(circuit, hidden, {"C1", "C2", "C3"}, 1, route=False)
        self.assertEqual(preview.wires, hidden.wires)
        self.assertEqual(preview.wiring_positions, hidden.wiring_positions)
        self.assertFalse(preview.wires_visible)
        shown = reroute_perfboard(circuit, replace(preview, wires_visible=True), reference=original)
        self.assertEqual(shown.connectors, original.connectors)
        self.assertTrue(preserves_nets(circuit, shown))


if __name__ == "__main__":
    unittest.main()
