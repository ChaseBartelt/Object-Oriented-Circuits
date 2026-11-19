"""Exercise live behavioral chip diagrams through the real schematic viewer."""
# Drive the real schematic view with compiled behavior and inspect live signal identities.
# Opening a chip changes presentation only; input toggles, output probes, and private state stay
# bound.

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem

from circuitlab.layout.persistence import pack_layout
from circuitlab.math.digital import DigitalSimulation
from circuitlab.tooling.session import CircuitSession
from circuitlab.viewer.behavior_diagram import BehaviorDiagram, behavior_graph
from circuitlab.viewer.schematic import SchematicView
from circuitlab.viewer.window import ViewerWindow


REACTION = """chip Timer(clk, arm, react, reset, LEDR0, LEDR1, LEDR2, LEDR3, LEDR4, LEDR5, LEDR6, LEDR7, LEDR8, LEDR9) {
    clk, arm, react, reset = input;
    LEDR0, LEDR1, LEDR2, LEDR3, LEDR4, LEDR5, LEDR6, LEDR7, LEDR8, LEDR9 = output;
    const IDLE[2] = 0;
    const WAITING[2] = 1;
    const GO[2] = 2;
    fsm[2];
    wait_ticks[4];
    elapsed[8];
    clock(clk, 1kHz) {
        every(10ms) {
            if (reset) { fsm = IDLE; wait_ticks = 0; elapsed = 0; }
            else {
                switch (fsm) {
                    case IDLE: if (arm) { fsm = react ? GO : WAITING; } break;
                    case WAITING:
                        if (wait_ticks == 2) { fsm = GO; }
                        else { wait_ticks += 1; }
                        break;
                    case GO: if (!react) { elapsed += 1; } break;
                    default: break;
                }
            }
        }
    }
    LEDR[7:0] = elapsed[7:0];
    LEDR8 = fsm == WAITING;
    LEDR9 = fsm == GO;
}
"""

NESTED = """chip Count(clk, enable, out) {
    clk, enable = input;
    out[8] = output;
    value[8];
    clock(clk, 1kHz) { if (enable) { value += 1; } }
    out = value;
}
chip Wrapper(clk, enable, out) {
    clk, enable = input;
    out[8] = output;
    Count(clk, enable, out);
}
main Counters {
    clk, left, right = input;
    Count_2_value[8], second[8] = output;
    Wrapper(clk, left, Count_2_value);
    Wrapper(clk, right, second);
}
"""


class BehaviorSchematicTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / 'behavior.ckt'
        self.window = ViewerWindow()
        self.window.resize(1400, 900)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.source_tools.checkpoint()
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def open(self, source=REACTION):
        self.path.write_text(source, encoding='utf-8')
        self.assertTrue(self.window.open_path(self.path))
        self.assertFalse(self.window.session.issues)
        self.assertIsNotNone(self.window.digital.simulation)
        self.window.views.setCurrentIndex(0)
        self.app.processEvents()
        return self.window.schematic

    def expand(self, name='@method:0'):
        schematic = self.window.schematic
        schematic.toggle_package(name)
        self.app.processEvents()
        self.assertIn(name, schematic.behavior_diagrams, schematic.behavior_models)
        return schematic.behavior_diagrams[name]

    def click(self, diagram, block):
        schematic = self.window.schematic
        location = diagram.mapToScene(diagram.rects[block].center())
        schematic.ensureVisible(diagram.mapRectToScene(diagram.rects[block]))
        self.app.processEvents()
        point = schematic.mapFromScene(location)
        self.assertTrue(schematic.viewport().rect().contains(point))
        QTest.mouseClick(schematic.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

    def test_reaction_timer_groups_real_output_bits_and_keeps_control_dependencies(self):
        simulation = DigitalSimulation(REACTION)
        blocks, edges, notice = behavior_graph(simulation)
        self.assertFalse(notice)
        self.assertIn('LEDR[7:0]', blocks)
        self.assertEqual(blocks['LEDR[7:0]'].members, tuple(f'LEDR{i}' for i in range(8)))
        self.assertEqual(blocks['LEDR[7:0]'].caption, 'elapsed[7:0]')
        self.assertIn(('elapsed', 'LEDR[7:0]'), edges)
        self.assertIn(('fsm', 'LEDR9'), edges)
        self.assertIn(('arm', 'fsm'), edges)
        self.assertIn(('wait_ticks', 'fsm'), edges)
        self.assertNotIn('clk', blocks)
        self.assertNotIn('IDLE', blocks)
        self.assertEqual(blocks['LEDR[7:0]'].value({f'LEDR{i}': (165 >> i) & 1 for i in range(8)}), 165)

    def test_output_grouping_does_not_merge_different_drivers_or_missing_bits(self):
        source = """chip Bits(a,b,LEDR0,LEDR1,LEDR3,LEDR4) {
            a[4], b[4] = input;
            LEDR0,LEDR1,LEDR3,LEDR4 = output;
            LEDR0 = a[0]; LEDR1 = b[1]; LEDR3 = a[2]; LEDR4 = a[3];
        }"""
        blocks, edges, _ = behavior_graph(DigitalSimulation(source))
        self.assertIn('LEDR0', blocks)
        self.assertIn('LEDR1', blocks)
        self.assertNotIn('LEDR[4:0]', blocks)
        self.assertEqual(blocks['LEDR[4:3]'].caption, 'a[3:2]')
        self.assertIn(('a', 'LEDR[4:3]'), edges)

    def test_opened_chip_contains_functional_blocks_instead_of_source_listing(self):
        schematic = self.open()
        layout = pack_layout(schematic.layout)
        original = self.path.read_bytes()
        diagram = self.expand()
        self.assertIsInstance(diagram, BehaviorDiagram)
        self.assertTrue({'fsm', 'wait_ticks', 'elapsed', 'LEDR[7:0]'} <= diagram.blocks.keys())
        text = '\n'.join(item.text() for item in schematic.scene().items()
                         if isinstance(item, QGraphicsSimpleTextItem))
        self.assertNotIn('clock(clk', text)
        self.assertNotIn('switch (fsm)', text)
        self.assertEqual(pack_layout(schematic.layout), layout)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIn('@method:0', schematic.packages)

    def test_input_click_live_steps_and_probe_work_without_switching_tabs(self):
        schematic = self.open()
        diagram = self.expand()
        model = schematic.behavior_models['@method:0']
        simulation = self.window.digital.simulation
        self.click(diagram, 'arm')
        self.assertEqual(simulation.values['arm'], 1)
        self.assertEqual(diagram.values['arm'], 1)
        for _ in range(50):
            self.window.digital.step()
        self.assertEqual(diagram.values['fsm'], 2)
        self.assertEqual(diagram.values['elapsed'], 1)
        self.assertEqual(diagram.blocks['LEDR[7:0]'].value(diagram.values), 1)
        self.assertIs(schematic.behavior_diagrams['@method:0'], diagram)
        self.assertIs(schematic.behavior_models['@method:0'], model)
        self.click(diagram, 'elapsed')
        self.assertEqual(self.window.digital.probe.currentText(), 'elapsed')
        self.assertEqual(self.window.views.currentIndex(), 0)

    def test_grouped_output_click_probes_an_existing_port(self):
        self.open()
        diagram = self.expand()
        self.click(diagram, 'LEDR[7:0]')
        self.assertEqual(self.window.digital.probe.currentText(), 'LEDR0')
        self.assertIn('LEDR0', self.window.digital.waveform.traces)
        self.assertEqual(self.window.views.currentIndex(), 0)

    def test_dependency_wire_click_probes_source_without_toggling_input(self):
        schematic = self.open()
        diagram = self.expand()
        path = diagram.paths['arm', 'fsm']
        position = next(point for fraction in (.1, .2, .3, .4, .5, .6, .7, .8)
                        if (point := path.pointAtPercent(fraction))
                        and diagram._hit(point) == (diagram.blocks['arm'], True))
        location = diagram.mapToScene(position)
        schematic.centerOn(location)
        self.app.processEvents()
        QTest.mouseClick(schematic.viewport(), Qt.MouseButton.LeftButton,
                         pos=schematic.mapFromScene(location))
        self.assertEqual(self.window.digital.probe.currentText(), 'arm')
        self.assertEqual(self.window.digital.simulation.values['arm'], 0)
        self.assertEqual(self.window.views.currentIndex(), 0)

    def test_board_instance_inputs_show_actual_synchronized_values(self):
        source = """chip Lamp(a,y) { a=input; y=output; y=a; }
        main HYBRID01 {
            MAX10_CLK1_50, SW0 = input;
            LEDR0 = output;
            Lamp(SW0,LEDR0);
        }"""
        schematic = self.open(source)
        diagram = self.expand()
        digital = self.window.digital
        self.assertEqual(digital.simulation.profile, 'de10-lite')
        bindings = schematic.behavior_models['@method:0'][1]
        self.assertNotEqual(bindings['a'], 'SW0')
        digital.set_input('SW0', 1)
        self.assertEqual(diagram.values['a'], 0)
        digital.step()
        self.assertEqual(diagram.values['y'], 0)
        digital.step()
        self.assertEqual(diagram.values['a'], 1)
        self.assertEqual(diagram.values['y'], 1)
        self.click(diagram, 'a')
        self.assertEqual(digital.probe.currentText(), bindings['a'])
        self.assertEqual(digital.simulation.values['SW0'], 1)

    def test_nested_instances_bind_private_state_even_when_generated_names_collide(self):
        schematic = self.open(NESTED)
        self.expand('@method:0')
        first = self.expand('@method:1')
        self.expand('@method:2')
        second = self.expand('@method:3')
        # Redrawing after opening a sibling recreates scene items, not models.
        first = schematic.behavior_diagrams['@method:1']
        first_bindings = schematic.behavior_models['@method:1'][1]
        second_bindings = schematic.behavior_models['@method:3'][1]
        self.assertNotEqual(first_bindings['value'], 'Count_2_value')
        self.assertNotEqual(first_bindings['value'], second_bindings['value'])
        self.assertNotIn(second_bindings['value'], first_bindings.values())
        self.click(first, 'enable')
        self.assertEqual(self.window.digital.simulation.values['left'], 1)
        self.assertEqual(self.window.digital.simulation.values['right'], 0)
        self.window.digital.step()
        self.assertEqual(first.values['value'], 1)
        self.assertEqual(second.values['value'], 0)
        self.click(first, 'value')
        self.assertEqual(self.window.digital.probe.currentText(), first_bindings['value'])
        self.assertEqual(self.window.views.currentIndex(), 0)

    def test_nested_pin_banks_keep_scalar_ports_and_live_input_bindings(self):
        source = '''chip Bank(a[1:0], y[1:0]) {
            a[1:0] = input; y[1:0] = output;
            y0 = a0; y1 = a1;
        }
        main Pair {
            left, right = input; first, second = output;
            Bank(left, right, first, second);
        }'''
        schematic = self.open(source)
        diagram = self.expand()
        model, bindings = schematic.behavior_models['@method:0']
        self.assertEqual(model.inputs, ('a1', 'a0'))
        self.assertEqual(model.outputs, ('y1', 'y0'))
        self.assertEqual(set(diagram.blocks), {'a1', 'a0', 'y1', 'y0'})
        self.click(diagram, 'a0')
        self.assertEqual(self.window.digital.simulation.values['right'], 1)
        self.assertEqual(self.window.digital.simulation.values['left'], 0)
        self.assertEqual(diagram.values['y0'], 1)
        self.assertEqual(diagram.values['y1'], 0)
        self.assertEqual(bindings['a0'], 'right')

    def test_closing_and_reopening_hood_preserves_layout_source_and_simulation(self):
        schematic = self.open()
        source = self.window.session.source
        disk = self.path.read_bytes()
        layout = pack_layout(schematic.layout)
        placements = schematic.placements()
        self.expand()
        simulation = self.window.digital.simulation
        self.window.digital.set_input('arm', 1)
        self.window.digital.step()
        values = dict(simulation.values)
        schematic.toggle_package('@method:0')
        self.assertFalse(schematic.behavior_diagrams)
        self.assertNotIn('@method:0', schematic.expanded)
        diagram = self.expand()
        self.assertEqual(diagram.values, values)
        self.assertIs(self.window.digital.simulation, simulation)
        self.assertEqual(simulation.cycles, 1)
        self.assertEqual(pack_layout(schematic.layout), layout)
        self.assertEqual(schematic.placements(), placements)
        self.assertEqual(self.window.session.source, source)
        self.assertEqual(self.path.read_bytes(), disk)
        self.assertFalse(self.window.placement_dirty)

    def test_standalone_session_preview_does_not_invent_live_values(self):
        session = CircuitSession(source=REACTION)
        session.render()
        schematic = SchematicView()
        try:
            schematic.draw(session.drawing)
            schematic.toggle_package('@method:0')
            diagram = schematic.behavior_diagrams['@method:0']
            self.assertIn('elapsed', diagram.blocks)
            self.assertEqual(diagram.values, {})
            self.assertIsNone(diagram.blocks['elapsed'].value(diagram.values))
            self.assertEqual(session.source, REACTION)
        finally:
            schematic.close()
            schematic.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
