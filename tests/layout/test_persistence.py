"""Saved display preferences remain separate from electrical component identity."""

from dataclasses import replace
import json
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.gates import rebind_layout
from circuitlab.layout.geometry import Layout
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.grid import align_layout
from circuitlab.layout.perfboard import move_perfboard_parts, reroute_perfboard
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.shorten import optimize_layout


class CompactLabelPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.circuit = compile_circuit("R1(1k) between(a,b);\nC1(1u) between(b,0);", render_only=True)
        resistor, capacitor = self.circuit.components
        self.drawing = Layout(
            symbols=[(resistor, (0, 0), (2, 0), "+"),
                     (capacitor, (4, 0), (6, 0), "+")],
            positions={"R1": (0, 0), "C1": (4, 0)},
            wires={"b": [((2, 0), (4, 0))]},
        )

    def test_compact_component_labels_round_trip_through_json(self):
        drawing = replace(self.drawing, compact_labels=frozenset({"R1", "C1"}))
        record = json.loads(json.dumps(pack_layout(drawing)))
        self.assertEqual(record["compact_labels"], ["C1", "R1"])
        restored = restore_layout(record, self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.compact_labels, drawing.compact_labels)
        self.assertIsInstance(restored.compact_labels, frozenset)
        self.assertEqual(restored.symbols, drawing.symbols)
        self.assertEqual(restored.wires, drawing.wires)

    def test_older_records_default_to_standard_labels(self):
        record = pack_layout(self.drawing)
        self.assertNotIn("compact_labels", record)
        restored = restore_layout(record, self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.compact_labels, frozenset())

    def test_moving_rotating_and_aligning_preserve_compact_label_choices(self):
        drawing = replace(self.drawing, compact_labels=frozenset({"R1"}))
        for operation, edited in (
            ("move", move_symbol(self.circuit, drawing, "R1", (0, 2))),
            ("rotate", move_symbol(self.circuit, drawing, "R1", (0, 0), rotate=1)),
            ("align", align_layout(self.circuit, drawing)),
            ("optimize", optimize_layout(self.circuit, drawing, move_parts=True)),
        ):
            with self.subTest(operation=operation):
                self.assertIsNotNone(edited)
                self.assertEqual(edited.compact_labels, drawing.compact_labels)

    def test_perfboard_movement_and_rerouting_preserve_compact_label_choices(self):
        circuit = compile_circuit("perf[A-R,1-24];\nR1(1k) between(a,b);\nC1(1u) between(b,0);",
                                  render_only=True)
        drawing = replace(self.drawing, pin_grid=1, compact_labels=frozenset({"C1"}))
        moved = move_perfboard_parts(circuit, drawing, {"R1"}, (0, -2))
        optimized = reroute_perfboard(circuit, moved)
        self.assertEqual(moved.compact_labels, drawing.compact_labels)
        self.assertEqual(optimized.compact_labels, drawing.compact_labels)

    def test_reusable_layout_maps_compact_labels_to_current_component_names(self):
        drawing = replace(self.drawing, compact_labels=frozenset({"R1"}))
        instance = compile_circuit("R2(1k) between(x,y);\nC2(1u) between(y,0);", render_only=True)
        rebound = rebind_layout(drawing, self.circuit, instance,
                                (("a", "x"), ("b", "y"), ("0", "0")), ())
        self.assertIsNotNone(rebound)
        self.assertEqual(rebound.compact_labels, frozenset({"R2"}))
        self.assertEqual(drawing.compact_labels, frozenset({"R1"}))

    def test_invalid_compact_label_containers_are_rejected_safely(self):
        for value in (None, "R1", {"R1": True}, 1, ("R1",)):
            with self.subTest(value=value):
                record = {**pack_layout(self.drawing), "compact_labels": value}
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_invalid_or_unknown_component_names_are_rejected_safely(self):
        for value in ([1], [["R1"]], [None], ["R_missing"], ["R1", "C_missing"]):
            with self.subTest(value=value):
                record = {**pack_layout(self.drawing), "compact_labels": value}
                self.assertIsNone(restore_layout(record, self.circuit, ()))


class UndersideWirePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.circuit = compile_circuit("R1(1k) between(a,b);\nC1(1u) between(b,0);", render_only=True)
        resistor, capacitor = self.circuit.components
        self.drawing = Layout(
            symbols=[(resistor, (0, 0), (2, 0), "+"), (capacitor, (4, 0), (6, 0), "+")],
            positions={"R1": (0, 0), "C1": (4, 0)},
            wires={"b": [((2, 0), (4, 0))]},
        )
        self.underside = {"b": [((2.5, 0), (3.5, 0))]}

    def test_underside_subsegment_round_trip_preserves_the_authoritative_union(self):
        drawing = replace(self.drawing, underside_wires=self.underside)
        restored = restore_layout(json.loads(json.dumps(pack_layout(drawing))), self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.underside_wires, self.underside)
        self.assertEqual(restored.wires, self.drawing.wires)

    def test_old_records_without_underside_metadata_keep_all_wiring_on_top(self):
        record = pack_layout(self.drawing)
        self.assertNotIn("underside_wires", record)
        restored = restore_layout(record, self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.underside_wires, {})

    def test_underside_subsegment_can_span_multiple_collinear_union_pieces(self):
        drawing = replace(self.drawing, underside_wires=self.underside,
                          wires={"b": [((2, 0), (3, 0)), ((3, 0), (4, 0))]})
        restored = restore_layout(pack_layout(drawing), self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.underside_wires, self.underside)

    def test_hidden_wire_mode_retains_underside_assignments_on_reference_routes(self):
        reference = {part.name: (a, b, 0) for part, a, b, _ in self.drawing.symbols}
        drawing = replace(self.drawing, underside_wires=self.underside,
                          wires_visible=False, wiring_positions=reference,
                          symbols=[(self.circuit.components[0], (0, 3), (2, 3), "+"),
                                   self.drawing.symbols[1]],
                          positions={"R1": (0, 3), "C1": (4, 0)})
        record = json.loads(json.dumps(pack_layout(drawing)))
        restored = restore_layout(record, self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertFalse(restored.wires_visible)
        self.assertEqual(restored.underside_wires, self.underside)
        self.assertEqual(restored.wires, drawing.wires)
        record["underside_wires"] = {"b": [[(2, 1), (4, 1)]]}
        self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_reusable_layout_maps_underside_net_names_to_current_instance(self):
        drawing = replace(self.drawing, underside_wires=self.underside)
        instance = compile_circuit("R2(1k) between(x,y);\nC2(1u) between(y,0);", render_only=True)
        rebound = rebind_layout(drawing, self.circuit, instance,
                                (("a", "x"), ("b", "y"), ("0", "0")), ())
        self.assertIsNotNone(rebound)
        self.assertEqual(rebound.underside_wires, {"y": self.underside["b"]})
        self.assertEqual(drawing.underside_wires, self.underside)

    def test_invalid_underside_containers_or_unknown_nets_are_rejected(self):
        for value in (None, [], (), "b", {"missing": []}, {1: []}, {"a": self.underside["b"]},
                      {"b": None}, {"b": ()}, {"b": {}}, {"b": "wire"}):
            with self.subTest(value=value):
                record = {**pack_layout(self.drawing), "underside_wires": value}
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_malformed_points_and_duplicate_underside_edges_are_rejected(self):
        edge = self.underside["b"][0]
        for rows in ([edge, edge], [edge, tuple(reversed(edge))], [[(3, 0), (3, 0)]],
                     [[(True, 0), (3, 0)]], [[(float("nan"), 0), (3, 0)]],
                     [[(2, 0), (float("inf"), 0)]], [[(2,), (3, 0)]],
                     [[(2, 0)]], ["wire"]):
            with self.subTest(rows=rows):
                record = {**pack_layout(self.drawing), "underside_wires": {"b": rows}}
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_underside_edges_must_be_covered_by_the_same_nets_union(self):
        for edges in ([((1, 0), (3, 0))], [((2, 1), (4, 1))], [((2, 0), (4, .5))]):
            with self.subTest(edges=edges):
                record = {**pack_layout(self.drawing), "underside_wires": {"b": edges}}
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_legacy_ground_lead_repair_remaps_underside_metadata_to_retained_routes(self):
        circuit = compile_circuit("R1(1k) between(a,0);", render_only=True)
        resistor = circuit.components[0]
        for edge in (((4, 0), (6, 0)), ((2, 0), (6, 0))):
            with self.subTest(edge=edge):
                drawing = Layout(symbols=[(resistor, (0, 0), (2, 0), "+")], positions={"R1": (0, 0)},
                                 grounds=[(4, 0)], boundary={"0": (6, 0)},
                                 wires={"0": [((2, 0), (4, 0)), ((4, 0), (6, 0))]},
                                 underside_wires={"0": [edge]})
                restored = restore_layout(pack_layout(drawing), circuit, ("0",))
                self.assertIsNotNone(restored)
                self.assertEqual(restored.boundary["0"], (4, 0))
                self.assertEqual(restored.wires["0"], [((2, 0), (4, 0))])
                self.assertEqual(restored.underside_wires,
                                 {} if edge[0] == (4, 0) else {"0": [((2, 0), (4, 0))]})


class WireVisibilityPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.circuit = compile_circuit("R1(1k) between(a,b);\nC1(1u) between(b,0);", render_only=True)
        resistor, capacitor = self.circuit.components
        self.drawing = Layout(
            symbols=[(resistor, (0, 0), (2, 0), "+"),
                     (capacitor, (4, 0), (6, 0), "+")],
            positions={"R1": (0, 0), "C1": (4, 0)},
            wires={"b": [((2, 0), (4, 0))]},
        )
        self.wiring_positions = {part.name: (a, b, self.drawing.rotations.get(part.name, 0))
                                 for part, a, b, _ in self.drawing.symbols}
        self.hidden = replace(
            self.drawing,
            wires_visible=False,
            wiring_positions=self.wiring_positions,
            symbols=[(resistor, (0, 3), (2, 3), "+"), self.drawing.symbols[1]],
            positions={"R1": (0, 3), "C1": (4, 0)},
            rotations={"R1": 1},
        )

    def test_hidden_layout_round_trip_retains_reference_routes_after_parts_move(self):
        self.assertFalse(preserves_nets(self.circuit, self.hidden))
        record = json.loads(json.dumps(pack_layout(self.hidden)))
        self.assertIs(record["wires_visible"], False)
        self.assertEqual(record["wiring_positions"],
                         [["C1", [4, 0], [6, 0], 0], ["R1", [0, 0], [2, 0], 0]])
        restored = restore_layout(record, self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertIs(restored.wires_visible, False)
        self.assertEqual(restored.wiring_positions, self.wiring_positions)
        self.assertEqual(restored.symbols, self.hidden.symbols)
        self.assertEqual(restored.rotations, self.hidden.rotations)
        self.assertEqual(restored.wires, self.hidden.wires)

    def test_old_visible_records_default_to_visible_and_remain_strict(self):
        record = pack_layout(self.drawing)
        self.assertNotIn("wires_visible", record)
        self.assertNotIn("wiring_positions", record)
        restored = restore_layout(record, self.circuit, ())
        self.assertIsNotNone(restored)
        self.assertIs(restored.wires_visible, True)
        self.assertEqual(restored.wiring_positions, {})
        moved_record = pack_layout(replace(self.hidden, wires_visible=True))
        self.assertIsNone(restore_layout(moved_record, self.circuit, ()))

    def test_invalid_visibility_flags_are_rejected(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value):
                record = {**pack_layout(self.hidden), "wires_visible": value}
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_hidden_reference_positions_require_each_component_exactly_once(self):
        valid = pack_layout(self.hidden)["wiring_positions"]
        invalid = (None, "R1", {}, (), [], valid[:1], valid + [valid[0]],
                   [["R_missing", *valid[0][1:]], valid[1]],
                   [[1, *valid[0][1:]], valid[1]], [valid[0][:-1], valid[1]])
        for value in invalid:
            with self.subTest(value=value):
                record = {**pack_layout(self.hidden), "wiring_positions": value}
                self.assertIsNone(restore_layout(record, self.circuit, ()))
        record = pack_layout(self.hidden)
        del record["wiring_positions"]
        self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_hidden_reference_positions_validate_geometry_and_rotation(self):
        invalid = (
            ["C1", [float("nan"), 0], [6, 0], 0],
            ["C1", [4, 0], [float("inf"), 0], 0],
            ["C1", [4, 0], [4, 0], 0],
            ["C1", [True, 0], [6, 0], 0],
            ["C1", [4], [6, 0], 0],
            ["C1", [4, 0], [6, 0], -1],
            ["C1", [4, 0], [6, 0], 4],
            ["C1", [4, 0], [6, 0], True],
            ["C1", [4, 0], [6, 0], 1.0],
        )
        for row in invalid:
            with self.subTest(row=row):
                record = pack_layout(self.hidden)
                record["wiring_positions"][0] = row
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_hidden_layout_still_validates_component_identity_and_net_signatures(self):
        for change in ("missing", "duplicate", "kind", "nets"):
            with self.subTest(change=change):
                record = json.loads(json.dumps(pack_layout(self.hidden)))
                if change == "missing":
                    record["symbols"].pop()
                elif change == "duplicate":
                    record["symbols"][1] = record["symbols"][0]
                elif change == "kind":
                    record["symbols"][0][1] = "C"
                else:
                    record["symbols"][0][2] = ["a", "0"]
                self.assertIsNone(restore_layout(record, self.circuit, ()))

    def test_reusable_hidden_layout_maps_reference_component_names(self):
        instance = compile_circuit("R2(1k) between(x,y);\nC2(1u) between(y,0);", render_only=True)
        rebound = rebind_layout(self.hidden, self.circuit, instance,
                                (("a", "x"), ("b", "y"), ("0", "0")), ())
        self.assertIsNotNone(rebound)
        self.assertIs(rebound.wires_visible, False)
        self.assertEqual(rebound.wiring_positions,
                         {"R2": self.wiring_positions["R1"], "C2": self.wiring_positions["C1"]})
        self.assertEqual(set(self.hidden.wiring_positions), {"R1", "C1"})
        self.assertEqual(rebound.wires, {"y": self.hidden.wires["b"]})

    def test_standard_transforms_retain_wiring_reference_fields(self):
        drawing = replace(self.drawing, wiring_positions=self.wiring_positions)
        for operation, edited in (
            ("move", move_symbol(self.circuit, drawing, "R1", (0, 2))),
            ("rotate", move_symbol(self.circuit, drawing, "R1", (0, 0), rotate=1)),
            ("align", align_layout(self.circuit, drawing)),
            ("optimize", optimize_layout(self.circuit, drawing, move_parts=True)),
        ):
            with self.subTest(operation=operation):
                self.assertIsNotNone(edited)
                self.assertIs(edited.wires_visible, True)
                self.assertEqual(edited.wiring_positions, self.wiring_positions)


if __name__ == "__main__":
    unittest.main()
