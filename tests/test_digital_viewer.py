"""Interact with digital inputs, clocks, profiles and bounded trace history."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.model.graph import CircuitError
from circuitlab.viewer.digital import DigitalSimulator, DigitalWaveform


ROOT = Path(__file__).resolve().parents[1]
COMBO = 'chip Logic(a,b,y) { a,b=input; y=output; y=a & !b; }'
COUNTER = '''chip Counter(clk,a,y) {
    clk,a=input; y=output; count[3];
    clock(clk,8Hz) { if(a) { count+=1; } }
    y=count[2];
}'''


class DigitalViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = DigitalSimulator()
        self.events = []
        self.widget.values_changed.connect(lambda values: self.events.append(values))

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def show_source(self, source):
        self.widget.set_source(source)
        self.widget.resize(1150, 950)
        self.widget.show()
        self.app.processEvents()

    def click_diagram_item(self, item):
        view = self.widget.port_view
        position = view.mapFromScene(item.sceneBoundingRect().center())
        self.assertTrue(view.viewport().rect().contains(position))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=position)
        self.app.processEvents()

    def test_combinational_clicks_update_led_and_signal_colors(self):
        self.widget.set_source(COMBO)
        self.assertFalse(self.widget.run_button.isEnabled())
        QTest.mouseClick(self.widget.input_buttons['a'], Qt.MouseButton.LeftButton)
        self.assertEqual(self.widget.simulation.values['y'], 1)
        self.assertIn('y  1', self.widget.output_labels['y'].text())
        self.assertEqual(self.widget.port_view.values['y'], 1)
        QTest.mouseClick(self.widget.input_buttons['b'], Qt.MouseButton.LeftButton)
        self.assertEqual(self.widget.simulation.values['y'], 0)
        self.assertEqual(self.events[-1]['y'], 0)
        self.assertEqual(self.widget.source, COMBO)
        self.assertEqual(self.widget.waveform.history[-1][1]['b'], 1)
        self.assertEqual(self.widget.probe.currentText(), 'y')
        self.assertEqual(self.widget.waveform.traces[0], 'y')
        self.assertFalse(self.widget.waveform.clocked)
        self.assertEqual([point[0] for point in self.widget.waveform.history], [0, 1, 2])
        self.assertTrue(self.widget.status.isHidden())

    def test_diagram_inputs_toggle_logic_and_outputs_only_probe(self):
        self.show_source(COMBO)
        view = self.widget.port_view
        self.click_diagram_item(view.input_items['a'])
        self.assertEqual(self.widget.simulation.values['a'], 1)
        self.assertEqual(self.widget.simulation.values['y'], 1)
        self.assertTrue(self.widget.input_buttons['a'].isChecked())
        self.assertEqual(view.values['y'], 1)
        self.click_diagram_item(view.output_items['y'])
        self.assertEqual(self.widget.probe.currentText(), 'y')
        self.assertEqual(self.widget.waveform.traces[0], 'y')
        self.assertEqual(self.widget.simulation.values['y'], 1)
        self.click_diagram_item(view.input_items['b'])
        self.assertEqual(self.widget.simulation.values['y'], 0)
        self.click_diagram_item(view.input_items['a'])
        self.assertEqual(self.widget.simulation.values['a'], 0)
        self.assertFalse(self.widget.input_buttons['a'].isChecked())
        self.assertEqual(self.widget.source, COMBO)

    def test_diagram_switch_respects_synchronizer_while_paused(self):
        source = (ROOT / 'examples/digital/de10_switch_led.ckt').read_text()
        self.show_source(source)
        self.click_diagram_item(self.widget.port_view.input_items['SW5'])
        self.assertEqual(self.widget.simulation.values['SW5'], 1)
        self.assertEqual(self.widget.simulation.values['LEDR5'], 0)
        self.assertFalse(self.widget.timer.isActive())
        self.assertEqual(self.widget.simulation.cycles, 0)
        self.widget.step_button.click()
        self.assertEqual(self.widget.simulation.values['LEDR5'], 0)
        self.widget.step_button.click()
        self.assertEqual(self.widget.simulation.values['LEDR5'], 1)
        self.assertEqual(self.widget.port_view.values['LEDR5'], 1)
        self.assertEqual(self.widget.source, source)

    def test_zoom_and_hood_controls_preserve_view_during_state_changes(self):
        self.show_source(COUNTER)
        view = self.widget.port_view
        self.assertFalse(view.expanded)
        self.click_diagram_item(view.chip_item)
        self.assertTrue(view.expanded)
        self.assertIn('count', view.node_items)
        before = view.transform().m11()
        view.zoom(1.25)
        zoomed = view.transform().m11()
        self.assertGreater(zoomed, before)
        self.widget.set_input('a', 1)
        self.widget.step_button.click()
        self.assertEqual(self.widget.simulation.values['count'], 1)
        self.assertTrue(view.expanded)
        self.assertAlmostEqual(view.transform().m11(), zoomed)
        self.widget.reset_button.click()
        self.assertEqual(self.widget.simulation.values['count'], 0)
        self.assertTrue(view.expanded)
        self.assertAlmostEqual(view.transform().m11(), zoomed)
        view.zoom(1 / 1.25)
        self.assertLess(view.transform().m11(), zoomed)
        view.fit()
        self.assertGreater(view.transform().m11(), 0)
        self.click_diagram_item(view.chip_item)
        self.assertFalse(view.expanded)
        self.assertEqual(self.widget.source, COUNTER)

    def test_input_and_output_panels_collapse_independently(self):
        self.show_source(COMBO)
        self.assertTrue(self.widget.input_group.isVisible())
        self.assertTrue(self.widget.output_group.isVisible())
        self.widget.input_toggle.click()
        self.assertTrue(self.widget.input_group.isHidden())
        self.assertTrue(self.widget.input_toggle.isVisible())
        self.assertTrue(self.widget.output_group.isVisible())
        self.widget.output_toggle.click()
        self.assertTrue(self.widget.output_group.isHidden())
        self.assertTrue(self.widget.output_toggle.isVisible())
        self.app.processEvents()
        self.click_diagram_item(self.widget.port_view.input_items['a'])
        self.assertEqual(self.widget.simulation.values['y'], 1)
        self.widget.output_toggle.click()
        self.assertTrue(self.widget.output_group.isVisible())
        self.assertIn('y  1', self.widget.output_labels['y'].text())
        self.assertTrue(self.widget.input_group.isHidden())
        self.widget.input_toggle.click()
        self.assertTrue(self.widget.input_group.isVisible())
        self.assertTrue(self.widget.input_buttons['a'].isChecked())

    def test_waveform_keeps_plateau_endpoints_and_each_transition(self):
        wave = DigitalWaveform()
        wave.configure(('out',), {'out': 1})
        for cycle in range(1000):
            wave.sample(cycle, {'out': 0})
        self.assertEqual([point[0] for point in wave.history], [0, 999])
        wave.sample(1000, {'out': 1})
        for cycle in range(1001, 2001):
            wave.sample(cycle, {'out': 1})
        self.assertEqual([point[0] for point in wave.history], [0, 999, 1000, 2000])
        self.assertEqual([point[1]['out'] for point in wave.history], [0, 0, 1, 1])
        wave.close()

    def test_board_profile_exposes_all_ten_switches_with_two_edge_latency(self):
        source = (ROOT / 'examples/digital/de10_switch_led.ckt').read_text()
        self.widget.set_source(source)
        self.assertEqual(self.widget.profile.currentData(), 'de10-lite')
        self.assertEqual(set(self.widget.input_buttons), {f'SW{i}' for i in range(10)})
        self.assertEqual(set(self.widget.output_labels), {f'LEDR{i}' for i in range(10)})
        self.assertEqual(self.widget.waveform.traces, ('LEDR0', 'ooc_sw0_sync', 'ooc_sw0_meta', 'SW0'))
        self.widget.probe.setCurrentText('LEDR5')
        QTest.mouseClick(self.widget.input_buttons['SW5'], Qt.MouseButton.LeftButton)
        self.assertEqual(self.widget.simulation.values['LEDR5'], 0)
        QTest.mouseClick(self.widget.step_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.widget.simulation.values['LEDR5'], 0)
        QTest.mouseClick(self.widget.step_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.widget.simulation.values['LEDR5'], 1)
        self.assertEqual(self.widget.simulation.cycles, 2)
        self.assertEqual([sample[1]['LEDR5'] for sample in self.widget.waveform.history], [0, 0, 0, 1])
        self.assertEqual(self.widget.simulation.frequency, 50_000_000)
        self.assertAlmostEqual(self.widget.simulation.cycles / self.widget.simulation.frequency, 40e-9)
        self.assertTrue(self.widget.status.isHidden())
        self.assertNotIn('MAX10_CLK1_50', [self.widget.probe.itemText(i) for i in range(self.widget.probe.count())])
        self.widget.reset()
        self.assertEqual(self.widget.simulation.cycles, 0)
        self.assertTrue(all(self.widget.simulation.values[f'SW{i}'] == 0 for i in range(10)))
        self.assertTrue(all(self.widget.simulation.values[f'LEDR{i}'] == 0 for i in range(10)))
        self.assertEqual(len(self.widget.waveform.history), 1)

    def test_all_ports_is_optional_and_dependencies_are_cycle_safe(self):
        self.widget.set_source((ROOT / 'examples/digital/de10_switch_led.ckt').read_text())
        self.assertFalse(self.widget.all_ports.isChecked())
        self.assertNotIn('SW9', self.widget.waveform.traces)
        self.widget.all_ports.setChecked(True)
        self.assertIn('SW9', self.widget.waveform.traces)
        self.assertIn('LEDR9', self.widget.waveform.traces)
        self.widget.all_ports.setChecked(False)
        self.assertNotIn('SW9', self.widget.waveform.traces)
        self.widget.set_source(COUNTER)
        self.widget.probe.setCurrentText('count')
        self.assertEqual(self.widget.waveform.traces, ('count', 'a'))

    def test_profile_switch_resets_and_literal_ooc_has_no_hidden_sync(self):
        source = (ROOT / 'examples/digital/de10_switch_led.ckt').read_text()
        self.widget.set_source(source)
        self.widget.set_input('SW0', 1)
        self.widget.profile.setCurrentIndex(0)
        self.assertEqual(self.widget.simulation.values['SW0'], 0)
        self.widget.set_input('SW0', 1)
        self.assertEqual(self.widget.simulation.values['LEDR0'], 1)
        self.widget.set_source(source)
        self.assertEqual(self.widget.profile.currentData(), 'ooc')
        self.assertEqual(self.widget.simulation.values['SW0'], 0)

    def test_run_pause_and_step_are_bounded_and_retain_simulated_timing(self):
        self.widget.set_source(COUNTER)
        self.widget.set_input('a', 1)
        self.widget.toggle()
        self.assertTrue(self.widget.timer.isActive())
        self.assertEqual(self.widget.run_button.text(), 'Pause')
        self.widget.advance()
        cycles = self.widget.simulation.cycles
        self.assertGreater(cycles, 0)
        self.assertLessEqual(cycles, 128)
        self.assertEqual(self.widget.simulation.frequency, 8)
        self.assertTrue(self.widget.status.isHidden())
        self.widget.toggle()
        self.assertFalse(self.widget.timer.isActive())
        self.widget.advance()
        self.assertEqual(self.widget.simulation.cycles, cycles)
        self.widget.step()
        self.assertEqual(self.widget.simulation.cycles, cycles + 1)
        self.assertFalse(self.widget.timer.isActive())
        self.assertTrue(self.widget.status.isHidden())

    def test_probe_history_limit_and_paint(self):
        self.widget.set_source(COUNTER)
        self.widget.probe.setCurrentText('count')
        self.widget.set_input('a', 1)
        for _ in range(520):
            self.widget.simulation.step()
            self.widget.waveform.sample(self.widget.simulation.cycles, self.widget.simulation.values)
        self.widget._refresh()
        self.assertEqual(len(self.widget.waveform.history), 512)
        self.assertEqual(self.widget.waveform.history[0][0], 9)
        self.assertEqual(self.widget.waveform.history[-1][0], 520)
        self.assertIn('count', self.widget.waveform.traces)
        self.assertEqual(self.widget.waveform.traces[0], 'count')
        self.assertNotIn('clk', self.widget.waveform.traces)
        self.widget.resize(900, 720)
        self.widget.show()
        self.app.processEvents()
        self.assertFalse(self.widget.grab().isNull())
        self.assertEqual(self.widget.source, COUNTER)

    def test_invalid_and_empty_sources_clear_running_values(self):
        self.show_source(COUNTER)
        self.widget.toggle()
        self.widget.set_source('chip broken')
        self.assertIsNone(self.widget.simulation)
        self.assertFalse(self.widget.timer.isActive())
        self.assertFalse(self.widget.input_buttons)
        self.assertFalse(self.widget.output_labels)
        self.assertFalse(self.widget.port_view.values)
        self.assertFalse(self.widget.waveform.history)
        self.assertFalse(self.widget.run_button.isEnabled())
        self.assertEqual(self.events[-1], {})
        self.assertTrue(self.widget.status.isVisible())
        self.assertTrue(self.widget.status.text())
        self.widget.set_source('')
        self.assertEqual(self.widget.status.text(), '')
        self.assertTrue(self.widget.status.isHidden())

    def test_source_reload_and_runtime_failure_clear_old_values(self):
        self.show_source(COUNTER)
        self.widget.set_input('a', 1)
        self.widget.step()
        self.widget.set_source(COUNTER)
        self.assertEqual(self.widget.simulation.cycles, 0)
        self.assertEqual(self.widget.simulation.values['count'], 0)
        with patch.object(self.widget.simulation, 'step', side_effect=CircuitError('Stopped test simulation')):
            self.widget.step()
        self.assertIsNone(self.widget.simulation)
        self.assertFalse(self.widget.waveform.history)
        self.assertEqual(self.events[-1], {})
        self.assertIn('Stopped test simulation', self.widget.status.text())
        self.assertTrue(self.widget.status.isVisible())

    def test_shutdown_stops_pending_run_timer(self):
        self.widget.set_source(COUNTER)
        self.widget.toggle()
        self.widget.shutdown()
        QTest.qWait(50)
        self.assertEqual(self.widget.simulation.cycles, 0)
        self.assertFalse(self.widget.timer.isActive())
        self.assertEqual(self.widget.run_button.text(), 'Run')
