"""Manual wire repairs retain contacts and keep separated branches independent."""

from copy import deepcopy
from dataclasses import replace
import unittest

from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_connector, move_ground, move_wire, split_wire
from circuitlab.layout.geometry import Layout
from circuitlab.layout.wire_edit import physical_wire_islands
from circuitlab.model.graph import Circuit, Component


class WireEditingTest(unittest.TestCase):
    def setUp(self):
        self.circuit = Circuit("dc")

    def jog(self):
        points = [(0, 0), (0, 1), (.5, 1), (.5, 3), (0, 3), (0, 5)]
        return Layout(boundary={"a": points[0]},
                      connectors=[("a", points[-1], "a", True, False)],
                      wires={"a": list(zip(points, points[1:]))}, pin_grid=1)

    def unrelated_short(self, drawing):
        return replace(drawing,
                       boundary={**drawing.boundary, "b": (10, 0), "c": (11, 0)},
                       wires={**drawing.wires, "b": [((10, 0), (12, 0))]})

    def test_half_hole_jog_collapses_with_split_overlapping_and_reversed_strokes(self):
        original = self.jog()
        cases = [
            (original.wires["a"], ((.5, 1), (.5, 3))),
            (list(reversed(original.wires["a"])), ((.5, 3), (.5, 1))),
            ([*original.wires["a"], ((.5, 1.5), (.5, 2.5))], ((.5, 2.5), (.5, 1.5))),
        ]
        for edges, clicked in cases:
            with self.subTest(clicked=clicked, edges=edges):
                drawing = replace(original, wires={"a": edges})
                snapshot = deepcopy(drawing)
                moved, offset = move_wire(self.circuit, drawing, "a", clicked, -.5,
                                          snap=.1, pointer=-.49)
                self.assertIsNotNone(moved)
                self.assertEqual(offset, -.5)
                self.assertEqual(moved.wires["a"], [((0, 0), (0, 5))])
                self.assertTrue(preserves_nets(self.circuit, moved))
                self.assertEqual(drawing, snapshot)

    def test_sliding_one_run_does_not_move_a_separate_matching_label_island(self):
        drawing = self.jog()
        separate = ((20, 0), (24, 0))
        drawing = replace(drawing,
                          wires={"a": [*drawing.wires["a"], separate]},
                          connectors=[*drawing.connectors, ("a", separate[0], "a", True, False),
                                      ("a", separate[1], "a", False, True)])
        moved, _ = move_wire(self.circuit, drawing, "a", ((.5, 1), (.5, 3)), -.5)
        self.assertIsNotNone(moved)
        self.assertIn(separate, moved.wires["a"])
        self.assertEqual(moved.connectors, drawing.connectors)
        self.assertTrue(preserves_nets(self.circuit, moved))

    def test_repair_ignores_an_unrelated_short_without_changing_its_wires(self):
        drawing = self.unrelated_short(self.jog())
        clicked = ((.5, 1), (.5, 3))
        self.assertFalse(preserves_nets(self.circuit, drawing))
        self.assertIsNone(move_wire(self.circuit, drawing, "a", clicked, -.5)[0])
        moved, _ = move_wire(self.circuit, drawing, "a", clicked, -.5, repair=True)
        self.assertIsNotNone(moved)
        self.assertEqual(moved.wires["a"], [((0, 0), (0, 5))])
        self.assertEqual(moved.wires["b"], drawing.wires["b"])
        self.assertTrue(preserves_nets(self.circuit, moved, nets={"a"}))
        self.assertFalse(preserves_nets(self.circuit, moved))

    def test_repair_still_rejects_a_new_contact_with_a_foreign_terminal(self):
        drawing = self.unrelated_short(self.jog())
        drawing = replace(drawing, boundary={**drawing.boundary, "d": (1, 2)})
        moved, _ = move_wire(self.circuit, drawing, "a", ((.5, 1), (.5, 3)), .5, repair=True)
        self.assertIsNone(moved)

    def test_scoped_validation_still_requires_the_edited_net_to_be_connected(self):
        drawing = self.unrelated_short(self.jog())
        drawing = replace(drawing, wires={**drawing.wires, "a": [((0, 0), (0, 1))]})
        self.assertFalse(preserves_nets(self.circuit, drawing, nets={"a"}))
        self.assertFalse(preserves_nets(self.circuit, drawing, nets={"missing"}))

    def test_split_and_move_keep_matching_label_branches_independent(self):
        drawing = self.unrelated_short(Layout(
            boundary={"a": (0, 0)}, connectors=[("a", (8, 0), "a", False, True)],
            wires={"a": [((0, 0), (8, 0))]}, pin_grid=1,
        ))
        clicked = drawing.wires["a"][0]
        self.assertIsNone(split_wire(self.circuit, drawing, "a", clicked, (4, 0), grid=.5))
        split = split_wire(self.circuit, drawing, "a", clicked, (4, 0), grid=.5, repair=True)
        self.assertIsNotNone(split)
        _, islands = physical_wire_islands(split, "a")
        self.assertNotEqual(islands[(0, 0)], islands[(8, 0)])
        right = [edge for edge in split.wires["a"] if min(at[0] for at in edge) > 4]
        left = next(edge for edge in split.wires["a"] if max(at[0] for at in edge) < 4)
        moved, _ = move_wire(self.circuit, split, "a", left, 1, repair=True)
        self.assertIsNotNone(moved)
        self.assertTrue(all(edge in moved.wires["a"] for edge in right))
        self.assertTrue(preserves_nets(self.circuit, moved, nets={"a"}))

        index = next(i for i, (_, at, *_) in enumerate(split.connectors) if at[0] < 4)
        self.assertIsNone(move_connector(self.circuit, split, index, (-4, 2)))
        endpoint_moved = move_connector(self.circuit, split, index, (-4, 2), repair=True)
        self.assertIsNotNone(endpoint_moved)
        self.assertEqual(endpoint_moved.connectors[index][1], (-4, 2))
        self.assertTrue(all(edge in endpoint_moved.wires["a"] for edge in right))
        self.assertTrue(preserves_nets(self.circuit, endpoint_moved, nets={"a"}))
        self.assertEqual(endpoint_moved.wires["b"], drawing.wires["b"])
        self.assertIsNone(move_connector(self.circuit, split, index, (11, 0), repair=True))

    def test_split_ground_end_can_move_independently_off_board_during_repair(self):
        first = Component("R1", "R", "one", "0", 1000)
        second = Component("R2", "R", "two", "0", 1000)
        circuit = replace(self.circuit, components=[first, second])
        drawing = self.unrelated_short(Layout(
            symbols=[(first, (0, 0), (4, 0), "+"), (second, (0, 6), (4, 6), "+")],
            positions={"R1": (0, 0), "R2": (0, 6)}, grounds=[(4, -2)],
            wires={"0": [((4, -2), (4, 0)), ((4, 0), (4, 6))]}, pin_grid=1,
        ))
        split = split_wire(circuit, drawing, "0", ((4, 0), (4, 6)), (4, 3), grid=.5, repair=True)
        self.assertIsNotNone(split)
        lower, upper = sorted(split.grounds, key=lambda at: at[1])
        self.assertIsNone(move_ground(circuit, split, lower, (-6, 0)))
        moved = move_ground(circuit, split, lower, (-6, 0), repair=True)
        self.assertIsNotNone(moved)
        self.assertIn((-2, lower[1]), moved.grounds)
        self.assertIn(upper, moved.grounds)
        self.assertIn((upper, (4, 6)), moved.wires["0"])
        self.assertEqual(moved.wires["b"], drawing.wires["b"])
        self.assertTrue(preserves_nets(circuit, moved, nets={"0"}))


if __name__ == "__main__":
    unittest.main()
