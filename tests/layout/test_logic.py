"""Digital diagram placement changes geometry while retaining every endpoint."""
# Move digital blocks and route bends while treating source/target endpoints as invariants.
# Serialized geometry should restore those same connections rather than rebuild the logic.

import json
import unittest

from circuitlab.layout.logic import LogicGeometry


RECTS = {"a": (0, 0, 100, 60), "b": (300, 100, 100, 60), "c": (300, -100, 100, 60)}
ROUTES = {("a", "b"): ((100, 30), (200, 30), (200, 130), (300, 130)),
          ("a", "c"): ((100, 30), (160, 30), (160, -70), (300, -70))}


class LogicGeometryTest(unittest.TestCase):
    def setUp(self):
        self.geometry = LogicGeometry(RECTS, ROUTES)

    def assert_routes(self):
        for edge, path in self.geometry.routes.items():
            for first, second in zip(path, path[1:]):
                self.assertTrue(first[0] == second[0] or first[1] == second[1])
            for name, current, initial in zip(edge, (path[0], path[-1]), (ROUTES[edge][0], ROUTES[edge][-1])):
                moved, original = self.geometry.rects[name], RECTS[name]
                self.assertEqual(current, (initial[0] + moved[0] - original[0], initial[1] + moved[1] - original[1]))

    def test_move_block_snaps_and_preserves_fanout_and_manual_bends(self):
        self.assertTrue(self.geometry.move_block("a", 26, -14))
        self.assertEqual(self.geometry.rects["a"], (30, -10, 100, 60))
        self.assertEqual(set(self.geometry.routes), set(ROUTES))
        self.assertEqual(self.geometry.routes["a", "b"][1:3], ((200, 20), (200, 130)))
        self.assert_routes()
        self.assertFalse(self.geometry.move_block("a", 4, 4))

    def test_move_target_reattaches_terminal_run(self):
        self.geometry.move_block("b", -50, 40)
        self.assertEqual(self.geometry.routes["a", "b"][-1], (250, 170))
        self.assert_routes()

    def test_slide_internal_wire_keeps_pins_and_other_branches(self):
        self.assertTrue(self.geometry.slide_wire(("a", "b"), 1, 36))
        self.assertEqual(self.geometry.routes["a", "b"], ((100, 30), (240, 30), (240, 130), (300, 130)))
        self.assertEqual(self.geometry.routes["a", "c"], ROUTES["a", "c"])
        self.assert_routes()

    def test_sliding_first_and_last_segments_adds_terminal_doglegs(self):
        self.geometry.slide_wire(("a", "b"), 0, -30)
        self.assertEqual(self.geometry.routes["a", "b"][:3], ((100, 30), (100, 0), (200, 0)))
        self.assert_routes()
        self.geometry.slide_wire(("a", "b"), len(self.geometry.routes["a", "b"]) - 2, 20)
        self.assertEqual(self.geometry.routes["a", "b"][-2:], ((300, 150), (300, 130)))
        self.assert_routes()

    def test_straight_wire_moves_and_slides_without_moving_its_pin_ends(self):
        geom = LogicGeometry({"a": (0, 0, 100, 60), "b": (300, 0, 100, 60)},
                             {("a", "b"): ((100, 30), (300, 30))})
        geom.slide_wire(("a", "b"), 0, 20)
        self.assertEqual(geom.routes["a", "b"], ((100, 30), (100, 50), (300, 50), (300, 30)))
        geom.reset()
        geom.move_block("a", 0, 70)
        self.assertEqual(geom.routes["a", "b"], ((100, 100), (200, 100), (200, 30), (300, 30)))

    def test_self_feedback_translates_with_its_owner(self):
        geom = LogicGeometry({"a": RECTS["a"]},
                             {("a", "a"): ((100, 30), (140, 30), (140, -30), (-40, -30), (-40, 30), (0, 30))})
        original = geom.routes["a", "a"]
        geom.move_block("a", 10, 20)
        self.assertEqual(geom.routes["a", "a"], tuple((x + 10, y + 20) for x, y in original))

    def test_snapshot_roundtrip_and_press_baseline_restore(self):
        before = self.geometry.snapshot()
        self.geometry.move_block("a", 30, 20)
        self.geometry.slide_wire(("a", "b"), 1, -20)
        saved = json.loads(json.dumps(self.geometry.snapshot(), allow_nan=False))
        restored = LogicGeometry(RECTS, ROUTES, saved)
        self.assertEqual(restored.snapshot(), saved)
        self.assertTrue(self.geometry.restore(before))
        self.assertEqual(self.geometry.snapshot(), before)
        self.assertFalse(self.geometry.restore(before))
        self.assertFalse(self.geometry.reset())
        self.geometry.move_block("a", 20, 0)
        self.assertTrue(self.geometry.restore(None))
        self.assertEqual(self.geometry.snapshot(), before)

    def test_restore_ignores_unknown_names_invalid_points_and_stale_endpoints(self):
        record = {"version": 1, "nodes": {"a": [10, 20], "missing": [1, 2], "b": [float("nan"), 0]},
                  "wires": [["a", "b", [[100, 30], [300, 130]]],
                            ["missing", "b", [[0, 0], [300, 0]]],
                            ["a", "c", [[100, 30], [160, 30], [160, -70], [300, -70]]]]}
        self.assertTrue(self.geometry.restore(record))
        self.assertEqual(self.geometry.rects["a"][:2], (10, 20))
        self.assertEqual(self.geometry.rects["b"], RECTS["b"])
        self.assert_routes()
        current = self.geometry.snapshot()
        for bad in ([], {"version": 9}, {"version": 1, "nodes": []},
                    {"version": 1, "wires": [None] * 2049}):
            self.assertFalse(self.geometry.restore(bad))
            self.assertEqual(self.geometry.snapshot(), current)

    def test_restore_rejects_coordinates_that_put_pin_ends_outside_bounds(self):
        before = self.geometry.snapshot()
        self.geometry.restore({"version": 1, "nodes": {"a": [1_000_000, 0], "b": [10**1000, 0]}})
        self.assertEqual(self.geometry.snapshot(), before)

    def test_invalid_moves_are_noops_and_geometry_stays_bounded(self):
        before = self.geometry.snapshot()
        for operation in (lambda: self.geometry.move_block("missing", 10, 10),
                          lambda: self.geometry.move_block("a", float("inf"), 0),
                          lambda: self.geometry.slide_wire(("a", "b"), 99, 10),
                          lambda: self.geometry.slide_wire(("a", "b"), 0, 1_000_000)):
            self.assertFalse(operation())
            self.assertEqual(self.geometry.snapshot(), before)

    def test_nearest_segment_uses_scene_distance_and_returns_edge_identity(self):
        self.assertEqual(self.geometry.nearest_segment((204, 80), 5), (("a", "b"), 1))
        self.assertIsNone(self.geometry.nearest_segment((220, 80), 5))
        self.assertIsNone(self.geometry.nearest_segment((float("nan"), 0), 5))


if __name__ == "__main__":
    unittest.main()
