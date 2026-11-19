"""Persist editable digital diagrams without changing executable OOC or runtime state."""
# Persist digital geometry as metadata while comparing executable source and runtime state.
# Profiles, opened chips, and output modes keep separate placements and coherent undo histories.

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.layout.persistence import embed_placement, layout_payload, strip_placement
from circuitlab.tooling.source_edits import edit_value
from circuitlab.viewer.window import ViewerWindow


COUNTER = '''chip Counter(clk, enable, y) {
    clk, enable = input;
    y[8] = output;
    count[8];
    clock(clk, 100Hz) { if (enable) { count += 1; } }
    y = count;
}
'''
BOARD = '''chip HYBRID01(MAX10_CLK1_50, SW0, LEDR0) {
    MAX10_CLK1_50, SW0 = input;
    LEDR0 = output;
    LEDR0 = SW0;
}
'''
DIVIDER = 'V1(5) between(a,0);\nR1(1k) between(a,b);\nR2(1k) between(b,0);\n'


def serialized(value):
    """JSON turns tuples into arrays; compare the stored representation."""
    return json.loads(json.dumps(value))


class LogicLayoutSaveTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / 'counter.ckt'
        self.path.write_text(COUNTER, encoding='utf-8')
        self.window = ViewerWindow()
        self.window.resize(1400, 900)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.source_tools.sender = None
        self.window.source_tools.checkpoint()
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def open(self, path=None):
        self.assertTrue(self.window.open_path(path or self.path))
        self.assertFalse(self.window.session.issues)
        self.app.processEvents()

    def logic(self, *, expanded=False):
        self.window.views.setCurrentWidget(self.window.digital)
        view = self.window.digital.port_view
        if view.expanded != expanded:
            view.toggle_hood()
        self.app.processEvents()
        return view

    def diagram(self):
        self.window.views.setCurrentIndex(0)
        view = self.window.schematic
        if '@method:0' not in view.expanded:
            view.toggle_package('@method:0')
        self.app.processEvents()
        return view, view.behavior_diagrams['@method:0']

    def drag(self, view, location, delta=QPoint(70, 40)):
        view.resetTransform()
        view.centerOn(location)
        self.app.processEvents()
        start = view.mapFromScene(location)
        self.assertTrue(view.viewport().rect().contains(start))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), start + delta, delay=1)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start + delta)
        self.app.processEvents()

    def drag_logic_node(self, name='count', *, expanded=True, delta=QPoint(70, 40)):
        view = self.logic(expanded=expanded)
        item = view.node_items[name]
        before = item.pos()
        self.drag(view, item.sceneBoundingRect().center(), delta)
        self.assertNotEqual(item.pos(), before)
        self.assertTrue(self.window.placement_dirty)
        return view

    def drag_inline_node(self, name='count', delta=QPoint(70, 40)):
        view, diagram = self.diagram()
        before = diagram.rects[name].topLeft()
        self.drag(view, diagram.mapToScene(diagram.rects[name].center()), delta)
        self.assertNotEqual(diagram.rects[name].topLeft(), before)
        self.assertTrue(self.window.placement_dirty)
        return diagram

    def test_inline_and_logic_tab_layouts_save_in_comments_and_reopen_independently(self):
        self.open()
        self.drag_logic_node()
        logic_record = serialized(self.window.digital.port_view.placement())
        diagram = self.drag_inline_node(delta=QPoint(-60, 80))
        inline_record = serialized(diagram.placement())
        self.assertEqual(self.path.read_text(), COUNTER)
        self.assertTrue(self.window.save())
        saved = layout_payload(self.path.read_text())
        self.assertTrue(saved['logic']['schematic'])
        self.assertTrue(saved['logic']['simulation']['ooc'])
        self.assertEqual(strip_placement(self.path.read_text()), COUNTER)
        self.assertFalse(self.path.with_name(self.path.name + '.layout.json').exists())
        self.open()
        self.assertEqual(serialized(self.logic(expanded=True).placement()), logic_record)
        _, reopened = self.diagram()
        self.assertEqual(serialized(reopened.placement()), inline_record)
        self.assertFalse(self.window.placement_dirty)

    def test_run_state_survives_drag_and_metadata_only_save(self):
        self.open()
        digital = self.window.digital
        digital.set_input('enable', 1)
        digital.step()
        simulation = digital.simulation
        with patch('circuitlab.viewer.digital.perf_counter', return_value=100.0):
            digital.toggle()
            self.assertTrue(digital.timer.isActive())
            self.drag_logic_node()
            self.drag_inline_node()
            self.assertTrue(self.window.save())
            self.assertIs(digital.simulation, simulation)
            self.assertTrue(digital.timer.isActive())
            self.assertEqual(simulation.cycles, 1)
            self.assertEqual(simulation.values['count'], 1)
            digital.toggle()
        digital.step()
        self.assertEqual(simulation.values['count'], 2)
        self.assertEqual(strip_placement(self.path.read_text()), COUNTER)

    def test_wire_bends_in_both_views_save_without_moving_connected_endpoints(self):
        self.open()
        edge = 'count', 'y'
        view = self.logic(expanded=True)
        original = view.geometry.routes[edge]
        self.drag(view, view.edge_items[edge].path().pointAtPercent(.5), QPoint(0, 70))
        routed = view.geometry.routes[edge]
        self.assertNotEqual(routed, original)
        self.assertEqual((routed[0], routed[-1]), (original[0], original[-1]))
        schematic, diagram = self.diagram()
        original_inline = diagram.geometry.routes[edge]
        self.drag(schematic, diagram.mapToScene(diagram.paths[edge].pointAtPercent(.5)), QPoint(0, 90))
        routed_inline = diagram.geometry.routes[edge]
        self.assertNotEqual(routed_inline, original_inline)
        self.assertEqual((routed_inline[0], routed_inline[-1]), (original_inline[0], original_inline[-1]))
        self.assertTrue(self.window.save())
        self.open()
        self.assertEqual(self.logic(expanded=True).geometry.routes[edge], routed)
        _, diagram = self.diagram()
        self.assertEqual(diagram.geometry.routes[edge], routed_inline)
        self.assertEqual(strip_placement(self.path.read_text()), COUNTER)

    def test_simulation_port_and_internal_modes_keep_separate_positions(self):
        self.open()
        view = self.logic()
        ports_before = serialized(view.placement())
        self.drag_logic_node('enable', expanded=False)
        ports_after = serialized(view.placement())
        self.assertNotEqual(ports_after, ports_before)
        self.drag_logic_node()
        self.assertTrue(self.window.save())
        records = serialized(view.placement())
        self.open()
        view = self.logic()
        self.assertEqual(serialized(view.placement()), records)
        view.toggle_hood()
        view.toggle_hood()
        self.assertEqual(serialized(view.placement()), records)

    def test_undo_reverts_logic_drag_without_changing_source_or_simulation(self):
        self.open()
        view = self.logic(expanded=True)
        initial = serialized(view.geometry.snapshot())
        self.drag_logic_node()
        self.window.digital.step()
        sim = self.window.digital.simulation
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertEqual(serialized(view.geometry.snapshot()), initial)
        self.assertEqual(sim.cycles, 1)
        self.assertEqual(self.path.read_text(), COUNTER)
        schematic, diagram = self.diagram()
        initial = serialized(diagram.placement())
        self.drag_inline_node()
        schematic.setFocus()
        QTest.keyClick(schematic, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        diagram = schematic.behavior_diagrams['@method:0']
        self.assertEqual(serialized(diagram.placement()), initial)
        self.assertIs(self.window.digital.simulation, sim)

    def test_switching_files_does_not_reuse_unsaved_diagram_positions(self):
        other = self.path.with_name('other.ckt')
        other.write_text(COUNTER)
        self.open()
        view = self.logic(expanded=True)
        initial = serialized(view.geometry.snapshot())
        self.drag_logic_node()
        moved = serialized(view.geometry.snapshot())
        self.assertTrue(self.window.save())
        self.open(other)
        self.assertEqual(serialized(self.logic(expanded=True).geometry.snapshot()), initial)
        self.open()
        self.assertEqual(serialized(self.logic(expanded=True).geometry.snapshot()), moved)

    def test_profiles_save_and_restore_distinct_layouts(self):
        self.path.write_text(BOARD)
        self.open()
        digital = self.window.digital
        self.assertEqual(digital.profile.currentData(), 'de10-lite')
        view = self.logic()
        digital.profile.setCurrentIndex(digital.profile.findData('ooc'))
        self.app.processEvents()
        raw_initial = serialized(self.logic().geometry.snapshot())
        digital.profile.setCurrentIndex(digital.profile.findData('de10-lite'))
        self.app.processEvents()
        self.drag_logic_node('SW0', expanded=False)
        board_record = serialized(view.geometry.snapshot())
        digital.profile.setCurrentIndex(digital.profile.findData('ooc'))
        self.app.processEvents()
        self.assertEqual(serialized(self.logic().geometry.snapshot()), raw_initial)
        self.drag_logic_node('SW0', expanded=False, delta=QPoint(-40, 90))
        raw_record = serialized(view.geometry.snapshot())
        self.assertTrue(self.window.save())
        self.open()
        digital.profile.setCurrentIndex(digital.profile.findData('de10-lite'))
        self.app.processEvents()
        self.assertEqual(serialized(self.logic().geometry.snapshot()), board_record)
        digital.profile.setCurrentIndex(digital.profile.findData('ooc'))
        self.app.processEvents()
        self.assertEqual(serialized(self.logic().geometry.snapshot()), raw_record)

    def test_saved_logic_metadata_survives_value_undo_and_discard(self):
        self.open()
        self.drag_logic_node()
        self.assertTrue(self.window.save())
        logic = layout_payload(self.path.read_text())['logic']
        self.path.write_text(embed_placement(DIVIDER, 'auto', {}, logic_layouts=logic))
        self.open()
        plan = edit_value(self.window.schematic.circuit, 'R1', '2k', self.path,
                          self.window.session.source, lambda path: path.read_text())
        self.window.source_tools.apply(plan)
        self.assertTrue(self.window.save())
        self.window.schematic.undo()
        self.assertEqual(layout_payload(self.path.read_text())['logic'], logic)
        self.assertEqual(strip_placement(self.path.read_text()), DIVIDER)
        self.assertTrue(self.window.source_tools.discard_values())
        self.assertEqual(layout_payload(self.path.read_text())['logic'], logic)
        self.assertEqual(strip_placement(self.path.read_text()), DIVIDER.replace('R1(1k)', 'R1(2k)'))

    def test_layout_save_uses_acknowledged_ide_edit(self):
        self.open()
        self.drag_inline_node()
        requests = []

        def accept(request):
            requests.append(request)
            def acknowledge():
                self.path.write_text(request['after'], encoding='utf-8', newline='')
                self.window.source_tools.result({'id': request['id'], 'ok': True})
            QTimer.singleShot(0, acknowledge)

        self.window.source_tools.sender = accept
        self.assertTrue(self.window.save())
        self.assertEqual(len(requests), 1)
        self.assertEqual(strip_placement(requests[0]['after']).replace('\r\n', '\n'), COUNTER)
        self.assertTrue(layout_payload(requests[0]['after'])['logic']['schematic'])
        self.assertFalse(self.window.placement_dirty)

    def test_old_or_invalid_optional_logic_records_fall_back_without_rewriting_source(self):
        records = (
            None,
            'invalid optional data',
            {'schematic': {'missing-scope': {'version': 99}},
             'simulation': {'ooc': {'internals': {
                 'version': 1, 'nodes': {'count': ['bad', 40]}, 'wires': []}}}},
        )
        for logic in records:
            with self.subTest(logic=logic):
                payload = {'version': 1, 'mode': 'auto', 'layouts': []}
                if logic is not None:
                    payload['logic'] = logic
                source = COUNTER + '\n# @ooc-layout-begin\n# ' + json.dumps(payload) + '\n# @ooc-layout-end\n'
                self.path.write_text(source)
                self.open()
                self.assertIn('count', self.logic(expanded=True).node_items)
                _, diagram = self.diagram()
                self.assertIn('count', diagram.blocks)
                self.assertEqual(self.path.read_text(), source)
                self.assertFalse(self.window.placement_dirty)


if __name__ == '__main__':
    unittest.main()
