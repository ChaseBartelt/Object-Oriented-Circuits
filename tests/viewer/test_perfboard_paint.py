"""The mirrored solder face keeps hole identities and readable labels."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QGraphicsScene

from circuitlab.model.perfboard import PerfBoard
from circuitlab.viewer.perfboard import (
    board_point, inverse_board_point, paint_perfboard, underside_offset,
)


class PerfboardPaintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.board = PerfBoard(row_start="C", row_end="E", column_start=7, column_end=12)
        self.scene = QGraphicsScene()

    def tearDown(self):
        self.scene.clear()

    def center(self, item):
        return item.sceneBoundingRect().center()

    def test_component_and_underside_positions_map_to_the_same_physical_holes(self):
        offset = underside_offset(self.board)
        self.assertEqual(offset, (11, 0))
        for at in ((0, 0), (5, -2), (2.5, -1.5), (-1, 1)):
            for underside in (False, True):
                displayed = board_point(self.board, at, underside=underside, offset=offset)
                self.assertEqual(inverse_board_point(self.board, displayed, underside=underside, offset=offset), at)
        self.assertEqual(board_point(self.board, (0, 0), underside=True, offset=offset), (16, 0))
        self.assertEqual(board_point(self.board, (5, -2), underside=True, offset=offset), (11, -2))

    def test_both_faces_have_distinct_frames_and_equal_hole_counts(self):
        top = paint_perfboard(self.scene, self.board, scale=38)
        back = paint_perfboard(self.scene, self.board, scale=38, mirror=True,
                               offset=underside_offset(self.board))
        self.assertEqual((top.data(0), top.data(2)), ("perfboard", "top"))
        self.assertEqual((back.data(0), back.data(2)), ("perfboard", "underside"))
        self.assertFalse(top.rect().intersects(back.rect()))
        holes = [item for item in self.scene.items() if item.data(0) == "perfboard-hole"]
        self.assertEqual(sorted(item.data(1) for item in holes), [18, 18])
        copper, = [item for item in self.scene.items() if item.data(0) == "perfboard-pad"]
        self.assertEqual(copper.data(2), "underside")
        self.assertEqual(copper.data(1), 18)

    def test_underside_column_labels_are_reversed_but_text_is_readable(self):
        paint_perfboard(self.scene, self.board, scale=38, mirror=True,
                       offset=underside_offset(self.board))
        labels = {item.text(): item for item in self.scene.items() if item.data(0) == "perfboard-label"}
        self.assertLess(self.center(labels["12"]).x(), self.center(labels["7"]).x())
        self.assertLess(self.center(labels["C"]).y(), self.center(labels["E"]).y())
        self.assertTrue(all(item.transform().isIdentity() for item in labels.values()))
        self.assertTrue(all(item.data(2) == "underside" for item in labels.values()))
        title, = [item for item in self.scene.items() if item.data(0) == "perfboard-side"]
        self.assertIn("viewed from below", title.text())

    def test_mirrored_hole_path_matches_display_mapping(self):
        scale = 38
        offset = underside_offset(self.board)
        paint_perfboard(self.scene, self.board, scale=scale, mirror=True, offset=offset)
        holes, = [item for item in self.scene.items() if item.data(0) == "perfboard-hole"]
        for at in ((0, 0), (5, -2)):
            x, y = board_point(self.board, at, underside=True, offset=offset)
            self.assertTrue(holes.path().contains(QPointF(x * scale, -y * scale)))
        self.assertFalse(holes.path().contains(QPointF(0, 0)))


if __name__ == "__main__":
    unittest.main()
