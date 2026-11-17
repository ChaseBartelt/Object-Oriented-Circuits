"""Exercise behavioral buses and selectors through the compiler and live viewer."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.math.digital import DigitalSimulation, _Parser
from circuitlab.model.graph import CircuitError
from circuitlab.viewer.digital import DigitalSimulator
from circuitlab.viewer.logic_scene import BUS


MUX = '''chip Selector(select, a, b, y) {
    select = input;
    a[8], b[8] = input;
    y[8] = output;
    if (select) { y = a; } else { y = b; }
}'''


class BehavioralRuntimeTest(unittest.TestCase):
    def test_combinational_bus_selector_updates_without_clock(self):
        sim = DigitalSimulation(MUX)
        self.assertFalse(sim.clocks)
        self.assertEqual(sim.signals['a'], 8)
        for a, b in ((0, 255), (128, 50), (255, 1)):
            sim.set_input('a', a)
            sim.set_input('b', b)
            for select in (0, 1):
                sim.set_input('select', select)
                self.assertEqual(sim.values['y'], a if select else b)
        self.assertEqual(set(sim.dependencies['y']), {'select', 'a', 'b'})
        self.assertIn('?', sim.signal_expressions['y'])
        self.assertEqual(sim.cycles, 0)

    def test_else_if_priority_and_default(self):
        sim = DigitalSimulation('''chip Priority(a, b, y) {
            a, b = input; y[8] = output;
            if(a) { y = 50; } else if(b) { y = 200; } else { y = 0; }
        }''')
        for a, b, y in ((0, 0, 0), (0, 1, 200), (1, 0, 50), (1, 1, 50)):
            sim.set_input('a', a); sim.set_input('b', b)
            self.assertEqual(sim.values['y'], y)

    def test_bus_arithmetic_wraps_at_expression_width(self):
        sim = DigitalSimulation('''chip Sum(a, b, y, wide) {
            a[8], b[8] = input; y[8], wide[16] = output;
            y = a + b; wide = a + b;
        }''')
        sim.set_input('a', 255); sim.set_input('b', 2)
        self.assertEqual(sim.values['y'], 1)
        self.assertEqual(sim.values['wide'], 1)

    def test_declared_wires_have_values_dependencies_and_probes(self):
        sim = DigitalSimulation('''chip Pipeline(a, y) {
            a[8] = input; y[8] = output; link[8] = wire;
            link = a + 1; y = link;
        }''')
        sim.set_input('a', 49)
        self.assertEqual(sim.values['link'], 50)
        self.assertEqual(sim.values['y'], 50)
        self.assertEqual(sim.signal_kinds['link'], 'wire')
        self.assertEqual(sim.dependencies['y'], ('link',))
        self.assertFalse(any(n.startswith('ooc_comb_') for n in sim.signals))

    def test_vector_input_range_validation_does_not_mutate_state(self):
        sim = DigitalSimulation(MUX)
        sim.set_input('a', 200)
        for bad in (-1, 256, 1.25, '50'):
            with self.subTest(value=bad), self.assertRaises(CircuitError):
                sim.set_input('a', bad)
            self.assertEqual(sim.values['a'], 200)

    def test_large_vector_input_is_not_limited_to_machine_int(self):
        sim = DigitalSimulation('chip Wide(a,y) { a[4096]=input; y[4096]=output; y=a; }')
        value = (1 << 4095) + 50
        sim.set_input('a', value)
        self.assertEqual(sim.values['y'], value)

    def test_clocked_bus_updates_keep_old_state_semantics(self):
        sim = DigitalSimulation('''chip Accumulate(clk, a, y, delayed) {
            clk=input; a[8]=input; y[8], delayed[8]=output;
            count[8]; prior[8];
            clock(clk, 1kHz) { count += a; prior = count; }
            y=count; delayed=prior;
        }''')
        sim.set_input('a', 50)
        sim.step()
        self.assertEqual((sim.values['y'], sim.values['delayed']), (50, 0))
        sim.step()
        self.assertEqual((sim.values['y'], sim.values['delayed']), (100, 50))
        sim.set_input('a', 200)
        sim.step()
        self.assertEqual((sim.values['y'], sim.values['delayed']), (44, 100))

    def test_accelerated_conditional_wires_match_each_reference_sample(self):
        source = '''chip Accumulate(clk, enable, y, pulse) {
            clk, enable=input; y[8], pulse=output;
            count[8]; increment[8]=wire;
            if(enable) {
                if(count < 80) { increment=1; } else { increment=3; }
            } else { increment=2; }
            clock(clk, 1kHz) { count += increment; }
            y=count;
            if(count < 128) { pulse=0; } else { pulse=1; }
        }'''
        fast, reference = DigitalSimulation(source), DigitalSimulation(source)
        for enable in (1, 0, 1):
            fast.set_input('enable', enable); reference.set_input('enable', enable)
            expected = {}
            for _ in range(1000):
                reference.step()
                expected[reference.cycles] = dict(reference.values)
            samples = []
            fast.advance(1000, traces=('y', 'pulse'), sample=lambda c,v: samples.append((c,dict(v))), sample_every=37)
            self.assertEqual(fast.values, reference.values)
            for cycle, values in samples:
                self.assertEqual(values, expected[cycle])
            edges = {cycle for cycle in expected if cycle-1 in expected
                     and expected[cycle]['pulse'] != expected[cycle-1]['pulse']}
            self.assertTrue(edges <= {cycle for cycle, _ in samples})

    def test_parser_accepts_internal_assign_and_rejects_undriven_wire(self):
        module = _Parser('module A (input wire [7:0] a, output wire [7:0] y); '
                         'wire [7:0] link; assign link = a; assign y = link; endmodule').parse()['A']
        self.assertEqual(module.widths['a'], 8)
        self.assertIn('link', module.drivers)
        with self.assertRaisesRegex(CircuitError, 'no driver'):
            _Parser('module A (input wire a, output wire y); wire link; assign y = a; endmodule').parse()

    def test_deep_wire_chain_falls_back_without_losing_state(self):
        rows = ['chip Chain(clk,a,y) { clk=input; a[8]=input; y[8]=output; count[8];']
        previous = 'a'
        for i in range(160):
            rows.append(f'link{i}[8]=wire; link{i}={previous}+1;')
            previous = f'link{i}'
        rows.append(f'clock(clk,1kHz) {{ count += {previous}; }} y=count; }}')
        sim = DigitalSimulation('\n'.join(rows))
        self.assertIsNone(sim._fast_plan)
        sim.set_input('a', 5)
        sim.advance(20)
        self.assertEqual(sim.values['y'], (20 * 165) & 255)


class BehavioralViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = DigitalSimulator()
        self.widget.set_source(MUX)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_bus_button_edits_number_and_scalar_still_toggles(self):
        self.assertFalse(self.widget.input_buttons['a'].isCheckable())
        with patch('circuitlab.viewer.digital.QInputDialog.getText', return_value=('0xC8', True)):
            self.widget.input_buttons['a'].click()
        self.widget.input_buttons['select'].click()
        self.assertEqual(self.widget.simulation.values['y'], 200)
        self.assertIn('0xC8 (200)', self.widget.output_labels['y'].text())
        self.assertEqual(self.widget.port_view.input_items['a'].width, 8)
        self.assertEqual(self.widget.port_view.edge_items['a','__chip__'].pen().color().name(), BUS)
        self.assertEqual(self.widget.source, MUX)

    def test_schematic_bus_click_uses_same_editor(self):
        with patch('circuitlab.viewer.digital.QInputDialog.getText', return_value=('0b110010', True)):
            self.widget.port_view.input_toggled.emit('b')
        self.assertEqual(self.widget.simulation.values['y'], 50)
        self.assertEqual(self.widget.waveform.history[-1][1]['y'], 50)

    def test_invalid_or_cancelled_bus_edit_keeps_simulation(self):
        original = self.widget.simulation
        for text, accepted in (('256', True), ('-1', True), ('oops', True), ('50', False)):
            with patch('circuitlab.viewer.digital.QInputDialog.getText', return_value=(text,accepted)), \
                 patch('circuitlab.viewer.digital.QMessageBox.warning'):
                self.widget.toggle_input('a')
            self.assertIs(self.widget.simulation, original)
            self.assertEqual(original.values['a'], 0)
        self.assertTrue(self.widget.status.isHidden())
