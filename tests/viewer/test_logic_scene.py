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
from circuitlab.viewer.behavior_diagram import BehaviorDiagram
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

    def test_octant_opens_cpu_modules_and_steps_the_same_live_state(self):
        source = (ROOT / 'examples/advanced/octant/octant_cpu.ckt').read_text(encoding='utf-8')
        self.configure(source)
        self.assertTrue(self.view.expanded)
        self.assertTrue(any('BUS_ADDR' in signals for signals in self.view.edge_signals.values()))
        self.assertTrue(any('BUS_RDATA' in signals for signals in self.view.edge_signals.values()))

        def open_chip(name):
            item = next(item for item in self.view.node_items.values()
                        if item.module and self.sim.instances[item.module.instance].name == name)
            self.view.centerOn(item)
            self.click(item)
            self.assertEqual(self.sim.instances[self.view.scope].name, name)

        def back():
            self.view.centerOn(self.view.chip_item)
            self.click(self.view.chip_item)

        def step(count):
            before, transform = dict(self.view.node_items), self.view.transform()
            for _ in range(count):
                self.sim.step()
                self.view.set_values(self.sim.values)
            self.assertEqual(self.view.node_items, before)
            self.assertEqual(self.view.transform(), transform)

        for channel in (2, 5):
            self.sim.set_input(f'TRIP{channel}', 1)
        self.view.set_values(self.sim.values)
        open_chip('OctantCpuCore')
        open_chip('OctantCpuROM')
        first_word = self.view.node_items['word'].value
        step(1)
        word = self.view.node_items['word']
        self.assertNotEqual(word.value, first_word)
        self.assertEqual(word.value, self.sim.values['IR'])
        self.view.centerOn(word)
        self.click(word)
        self.assertEqual(self.probes, ['IR'])

        back()
        open_chip('OctantCpuALU')
        self.assertEqual(self.view.node_items['address'].value, self.sim.values['BUS_ADDR'])
        step(1)
        self.assertEqual(self.view.node_items['pc_next'].value, 12)
        back()
        open_chip('OctantCpuRegisterFile')
        step(5)
        self.assertEqual(self.view.node_items['r3'].value, 0x24)

        back()
        back()
        open_chip('OctantCpuMemory')
        open_chip('OctantCpuRAM')
        step(6)
        self.assertEqual(self.view.node_items['word0'].value, 0x24)
        self.assertEqual(self.view.node_items['word1'].value, 2)

        back()
        back()
        open_chip('OctantAlerts')
        open_chip('OctantCapture')
        self.assertEqual(self.view.node_items['pending'].value, 0x24)
        step(2)
        self.assertEqual(self.view.node_items['pending'].value, 0)
        self.assertEqual(self.sim.values['LAST_ACK'], 0x24)
        self.assertEqual(self.sim.cycles, 15)

    def test_generated_memory_windows_open_individually_with_live_words(self):
        from tests.exporters.test_stm32h750_memory_bank import WINDOWS, bank_simulation

        self.sim = bank_simulation(WINDOWS, {}, True)
        self.sim.set_input('READY', 1)
        self.sim.set_input('SIZE', 2)
        for index, (base, _) in enumerate(WINDOWS):
            self.sim.set_input('ADDRESS', base)
            self.sim.set_input('WDATA', 0x12340000 + index)
            self.sim.set_input('WRITE', 1)
            self.sim.step()
        self.sim.set_input('WRITE', 0)
        self.view.configure(self.sim)
        self.app.processEvents()

        bank = next(item for item in self.view.node_items.values() if item.module)
        self.view.centerOn(bank)
        self.click(bank)
        windows = [item.module.instance for item in self.view.node_items.values()
                   if item.module and self.sim.instances[item.module.instance].name
                   == 'M7FirmwareMemoryWindow']
        self.assertEqual(len(windows), len(WINDOWS))
        for index, instance in enumerate(windows):
            item = next(item for item in self.view.node_items.values()
                        if item.module and item.module.instance == instance)
            ports = {port.label: port.live for port in item.module.ports}
            self.assertEqual(self.sim.values[ports['base']], WINDOWS[index][0])
            self.view.centerOn(item)
            self.click(item)
            self.assertEqual(self.view.scope, instance)
            ram = next(item for item in self.view.node_items.values() if item.module)
            words = next(port.live for port in ram.module.ports if port.label == 'first_words')
            self.assertEqual(self.sim.values[words] & 0xFFFFFFFF,
                             0x12340000 + index)
            self.view.centerOn(self.view.chip_item)
            self.click(self.view.chip_item)

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

    def test_large_dependency_graph_remains_interactive_in_both_views(self):
        names = [f'n{i}' for i in range(451)]
        dependencies = {
            name: [names[(index - offset) % len(names)] for offset in range(1, 6)]
            for index, name in enumerate(names)
        }
        dependencies[names[0]].extend(names[1:3])
        signals = dict.fromkeys(names, 1)
        values = dict.fromkeys(names, 0)
        sim = SimpleNamespace(inputs=(), outputs=(names[-1],), values=values,
                              signals=signals, clocks=(), dependencies=dependencies, name='Large')
        self.view.configure(sim)
        self.view.toggle_hood()
        expected = {(source, target) for target, sources in dependencies.items() for source in sources}
        self.assertEqual(len(expected), 2257)
        self.assertTrue(self.view.expanded)
        self.assertEqual(set(self.view.node_items), set(names))
        self.assertEqual(set(self.view.edge_items), expected)
        self.view.fit()
        self.view.zoom(1.15)
        self.assertGreater(self.view.transform().m11(), 0)
        self.view.resetTransform()
        output = self.view.node_items[names[-1]]
        self.view.ensureVisible(output)
        self.click(output)
        self.assertEqual(self.probes, [names[-1]])

        values[names[-1]] = 1
        self.view.set_values(values)
        self.assertEqual(output.value, 1)
        inline = BehaviorDiagram(sim)
        self.assertEqual(set(inline.rects), set(names))
        self.assertEqual(set(inline.paths), expected)
        self.assertTrue(inline.geometry.move_block(names[-1], 20, 40))
        placement = inline.placement()
        self.assertTrue(inline.geometry.reset())
        inline.restore_placement(placement)
        self.assertEqual(inline.placement(), placement)

    def test_layering_accepts_a_deep_chain_without_recursion(self):
        names = [str(i) for i in range(1600)]
        edges = tuple(zip(names, names[1:]))
        self.assertEqual(_layers(names, edges)[names[-1]], (len(names) - 1, 0))

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
