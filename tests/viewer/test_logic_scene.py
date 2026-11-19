"""Exercise interactive, source-backed digital diagrams without changing source."""
# Use input clicks, output probes, and drags as distinct actions on the same live graph.
# Only geometry changes during movement; values and source-backed dependency identities remain
# valid.

import os
from pathlib import Path
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.math.digital import DigitalSimulation
from circuitlab.viewer.logic_scene import HIGH, LOW, LogicSceneView, _layers


ROOT = Path(__file__).resolve().parents[2]
COMBO = 'chip Logic(a,b,y) { a,b=input; y=output; y=a & !b; }'
COUNTER = '''chip Counter(clk,a,y) {
    clk,a=input; y=output; count[3];
    clock(clk,8Hz) { if(a) { count+=1; } }
    y=count[2];
}'''


class LogicSceneTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = LogicSceneView()
        self.view.resize(1000, 700)
        self.view.show()
        self.app.processEvents()
        self.toggles, self.probes = [], []
        self.view.input_toggled.connect(self.toggles.append)
        self.view.signal_probed.connect(self.probes.append)

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def configure(self, source=COMBO, profile='ooc'):
        self.sim = DigitalSimulation(source, profile=profile)
        self.view.configure(self.sim)
        self.app.processEvents()

    def click(self, item):
        point = self.view.mapFromScene(item.sceneBoundingRect().center())
        self.assertTrue(self.view.viewport().rect().contains(point))
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

    def test_inputs_toggle_but_outputs_probe_without_changing_inputs(self):
        self.configure()
        self.click(self.view.input_items['a'])
        self.assertEqual(self.toggles, ['a'])
        self.assertEqual(self.probes, [])
        self.click(self.view.output_items['y'])
        self.assertEqual(self.probes, ['y'])
        self.assertEqual(self.sim.values['a'], 0)  # The owner applies requested changes.

    def test_chip_click_expands_and_header_click_collapses_safely(self):
        self.configure()
        self.click(self.view.chip_item)
        self.assertTrue(self.view.expanded)
        self.assertEqual(set(self.view.edge_items), {('a', 'y'), ('b', 'y')})
        self.click(self.view.chip_item)
        self.assertFalse(self.view.expanded)
        self.assertIn('Logic', self.view.chip_item.caption)

    def test_expanded_wire_click_probes_its_source(self):
        self.configure()
        self.view.toggle_hood()
        edge = self.view.edge_items['a', 'y']
        point = self.view.mapFromScene(edge.path().pointAtPercent(.5))
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.probes, ['a'])

    def test_values_update_colors_without_rebuilding_or_changing_view(self):
        self.configure()
        self.view.toggle_hood()
        self.view.zoom(1.5)
        transform = self.view.transform()
        center = self.view.mapToScene(self.view.viewport().rect().center())
        nodes = dict(self.view.node_items)
        edges = dict(self.view.edge_items)
        self.sim.set_input('a', 1)
        self.view.set_values(self.sim.values)
        self.assertEqual(nodes, self.view.node_items)
        self.assertEqual(edges, self.view.edge_items)
        self.assertEqual(transform, self.view.transform())
        self.assertEqual(center, self.view.mapToScene(self.view.viewport().rect().center()))
        self.assertEqual(self.view.output_items['y'].value, 1)
        self.assertEqual(self.view.edge_items['a', 'y'].pen().color().name(), HIGH)
        self.assertEqual(self.view.edge_items['b', 'y'].pen().color().name(), LOW)

    def test_feedback_and_clock_metadata_are_source_backed(self):
        self.configure(COUNTER)
        self.view.toggle_hood()
        expected = {(source, target) for target, sources in self.sim.dependencies.items()
                    for source in sources if source not in self.sim.clocks}
        self.assertEqual(set(self.view.edge_items), expected)
        self.assertIn(('count', 'count'), expected)
        self.assertNotIn('clk', self.view.node_items)
        self.assertIn('Clock: clk', self.view.node_items['count'].toolTip())
        self.assertIn('if', self.view.node_items['count'].toolTip())
        positions = _layers(['a', 'x', 'z', 'y'], [('a', 'x'), ('x', 'z'), ('z', 'x'), ('z', 'y')])
        self.assertEqual(positions['x'][0], positions['z'][0])
        self.assertLess(positions['a'][0], positions['x'][0])
        self.assertLess(positions['z'][0], positions['y'][0])

    def test_board_hood_has_four_dependency_columns_and_two_edge_delay(self):
        source = (ROOT / 'tests/fixtures/digital/de10_switch_led.ckt').read_text(encoding='utf-8')
        self.configure(source, 'de10-lite')
        self.view.toggle_hood()
        chain = ['SW0', 'ooc_sw0_meta', 'ooc_sw0_sync', 'LEDR0']
        positions = [self.view.node_items[name].pos().x() for name in chain]
        self.assertEqual(positions, sorted(set(positions)))
        for left, right in zip(chain, chain[1:]):
            self.assertIn((left, right), self.view.edge_items)
        self.sim.set_input('SW0', 1)
        self.sim.step()
        self.view.set_values(self.sim.values)
        self.assertEqual(self.view.output_items['LEDR0'].value, 0)
        self.sim.step()
        self.view.set_values(self.sim.values)
        self.assertEqual(self.view.output_items['LEDR0'].value, 1)
        self.assertGreaterEqual(self.view.transform().m11(), .8)

    def test_middle_button_pans_zoomed_graph(self):
        self.configure()
        self.view.zoom(3)
        before = (self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value())
        start = self.view.viewport().rect().center()
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.MiddleButton, pos=start)
        QTest.mouseMove(self.view.viewport(), start + QPoint(45, 35))
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.MiddleButton, pos=start + QPoint(45, 35))
        after = (self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value())
        self.assertNotEqual(before, after)

    def test_zoom_never_reverses_direction_outside_manual_limits(self):
        self.configure()
        self.view.resetTransform()
        self.view.scale(.04, .04)
        self.view.zoom(.8)
        self.assertLessEqual(self.view.transform().m11(), .04)
        self.view.zoom(1.2)
        self.assertGreater(self.view.transform().m11(), .04)
        self.view.resetTransform()
        self.view.scale(7, 7)
        self.view.zoom(1.2)
        self.assertGreaterEqual(self.view.transform().m11(), 7)
        self.view.zoom(.8)
        self.assertLess(self.view.transform().m11(), 7)

    def test_large_graph_has_explicit_limit_without_silent_truncation(self):
        signals = {'a': 1, 'y': 1, **{f'n{i}': 1 for i in range(130)}}
        sim = SimpleNamespace(inputs=('a',), outputs=('y',), values={}, signals=signals,
                              clocks=(), dependencies={}, name='Large')
        self.view.configure(sim)
        self.view.toggle_hood()
        self.assertFalse(self.view.expanded)
        self.assertIn('132 signals', self.view.notice)
        self.assertIn('view limit', self.view.notice)
        self.assertEqual(set(self.view.node_items), {'a', 'y'})

    def test_clearing_source_removes_all_stale_items_and_values(self):
        self.configure()
        self.view.toggle_hood()
        self.view.configure(None)
        self.assertFalse(self.view.expanded)
        self.assertEqual(self.view.values, {})
        self.assertEqual(self.view.inputs, ())
        self.assertEqual(self.view.outputs, ())
        self.assertEqual(self.view.node_items, {})
        self.assertEqual(self.view.edge_items, {})
        self.assertIsNone(self.view.chip_item)
        self.assertEqual(self.view.scene().items(), [])

    def drag(self, point, delta):
        start = self.view.mapFromScene(point)
        end = self.view.mapFromScene(point + delta)
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.view.viewport(), end)
        return end

    def release(self, point):
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

    def test_drag_moves_input_and_wire_live_without_toggling_or_refitting(self):
        self.configure(COUNTER)
        self.view.toggle_hood()
        for _ in range(3):
            self.sim.step()
        self.view.set_values(self.sim.values)
        changes = []
        self.view.placement_changed.connect(lambda: changes.append(1))
        item = self.view.input_items['a']
        before, transform = item.pos(), self.view.transform()
        endpoint = self.view.geometry.routes['a', 'count'][0]
        end = self.drag(item.sceneBoundingRect().center(), QPointF(53, 27))
        self.assertEqual(item.pos() - before, QPointF(50, 30))
        moved = self.view.geometry.routes['a', 'count'][0]
        self.assertEqual((moved[0] - endpoint[0], moved[1] - endpoint[1]), (50, 30))
        self.assertEqual(changes, [])
        self.release(end)
        self.assertEqual(changes, [1])
        self.assertEqual(self.toggles, [])
        self.assertEqual(self.sim.cycles, 3)
        self.assertEqual(self.view.transform(), transform)
        QTest.keyClick(self.view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(item.pos(), before)
        self.assertEqual(changes, [1, 1])

    def test_drag_collapsed_chip_keeps_all_port_connections(self):
        self.configure()
        before = self.view.chip_item.pos()
        original = dict(self.view.geometry.routes)
        end = self.drag(self.view.chip_item.sceneBoundingRect().center(), QPointF(40, 30))
        self.release(end)
        self.assertFalse(self.view.expanded)
        self.assertEqual(self.view.chip_item.pos() - before, QPointF(40, 30))
        for edge, points in self.view.geometry.routes.items():
            index = 0 if edge[0] == '__chip__' else -1
            self.assertEqual(points[index], (original[edge][index][0] + 40, original[edge][index][1] + 30))
            self.assertEqual(points[-1 if index == 0 else 0], original[edge][-1 if index == 0 else 0])

    def test_wire_drag_changes_route_preserving_endpoints_and_undo(self):
        self.configure()
        self.view.toggle_hood()
        edge = ('a', 'y')
        before = self.view.geometry.snapshot()
        points = self.view.geometry.routes[edge]
        a, b = points[0:2]
        midpoint = QPointF((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        end = self.drag(midpoint, QPointF(0, 40))
        self.assertNotEqual(self.view.geometry.routes[edge], points)
        self.release(end)
        moved = self.view.geometry.routes[edge]
        self.assertEqual((moved[0], moved[-1]), (points[0], points[-1]))
        self.assertEqual(self.probes, [])
        self.assertTrue(self.view.undo_placement())
        self.assertEqual(self.view.geometry.snapshot(), before)

    def test_cancel_and_return_to_origin_do_not_dirty_or_toggle(self):
        self.configure()
        item = self.view.input_items['a']
        point = item.sceneBoundingRect().center()
        before = self.view.geometry.snapshot()
        changes = []
        self.view.placement_changed.connect(lambda: changes.append(1))
        end = self.drag(point, QPointF(30, 30))
        QTest.keyClick(self.view, Qt.Key.Key_Escape)
        self.release(end)
        self.assertEqual(self.view.geometry.snapshot(), before)
        end = self.drag(point, QPointF(30, 30))
        origin = self.view.mapFromScene(point)
        QTest.mouseMove(self.view.viewport(), origin)
        self.release(origin)
        self.assertEqual(self.view.geometry.snapshot(), before)
        self.assertEqual(changes, [])
        self.assertEqual(self.toggles, [])

    def test_placements_survive_hood_changes_and_explicit_round_trip_only(self):
        self.configure()
        end = self.drag(self.view.input_items['a'].sceneBoundingRect().center(), QPointF(40, 20))
        self.release(end)
        ports = self.view.geometry.snapshot()
        self.view.toggle_hood()
        end = self.drag(self.view.output_items['y'].sceneBoundingRect().center(), QPointF(30, 30))
        self.release(end)
        internals = self.view.geometry.snapshot()
        record = self.view.placement()
        self.view.toggle_hood()
        self.assertEqual(self.view.geometry.snapshot(), ports)
        self.view.toggle_hood()
        self.assertEqual(self.view.geometry.snapshot(), internals)
        self.configure()
        self.assertNotEqual(self.view.geometry.snapshot(), ports)
        self.view.restore_placement(record)
        self.assertEqual(self.view.geometry.snapshot(), ports)
        self.view.toggle_hood()
        self.assertEqual(self.view.geometry.snapshot(), internals)


if __name__ == '__main__':
    unittest.main()
