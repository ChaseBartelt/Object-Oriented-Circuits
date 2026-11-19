"""Check physical segment order, active-low values and live simulator updates."""
# Check physical segment order, active-low color, and exact signal probing together.
# Stepping, reset, collapsed outputs, and cleared sources must not leave stale lit segments.

import os
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.viewer.digital import DigitalSimulator
from circuitlab.viewer.behavior_diagram import behavior_graph
from circuitlab.viewer.seven_segment import LIT, UNLIT, segment_paths


COMBINATIONAL = '''chip HYBRID01(SW0, HEX00, HEX07, LEDR0) {
    SW0=input; HEX00,HEX07,LEDR0=output;
    HEX00=SW0; HEX07=!SW0; LEDR0=SW0;
}'''
CLOCKED = '''chip Display(clk, HEX00) {
    clk=input; HEX00=output; state;
    clock(clk,8Hz) { state=!state; }
    HEX00=state;
}'''


class SevenSegmentTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = DigitalSimulator()
        self.widget.resize(1100, 800)
        self.widget.show()
        self.display = self.widget.hex_display
        self.app.processEvents()

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_all_physical_digits_use_bit_order_and_active_low_polarity(self):
        outputs = tuple(f'HEX{digit}{bit}' for digit in range(6) for bit in range(8))
        self.display.configure(SimpleNamespace(outputs=outputs, signals=dict.fromkeys(outputs, 1),
                                               values=dict.fromkeys(outputs, 1)))
        self.assertEqual(len(self.display.pins), 48)
        self.assertLess(self.display.digit_rect(5).left(), self.display.digit_rect(0).left())
        for digit in range(6):
            for bit in range(8):
                values = dict.fromkeys(outputs, 1)
                values[f'HEX{digit}{bit}'] = 0
                self.display.set_values(values)
                self.assertEqual(self.display.lit_segments(digit), {bit})
                for other in set(range(6)) - {digit}:
                    self.assertFalse(self.display.lit_segments(other))
        self.display.set_values({})
        self.assertTrue(all(value is None for value in self.display.digit_values(0)))
        self.assertFalse(self.display.lit_segments(0))

    def test_logic_graph_groups_each_hex_bank_without_combining_digits(self):
        banks = ', '.join(f'HEX{digit}[7:0]' for digit in range(6))
        source = f'chip HYBRID01(SW[9:0], LEDR[9:0], {banks}) {{ SW[9:0]=input; LEDR[9:0],{banks}=output; '
        source += 'requests[10]=SW[9:0]; LEDR[9:0]=requests; '
        source += ' '.join(f'HEX{digit}[7:0]=sevenseg(requests >> {digit});' for digit in range(6))
        self.widget.set_source(source + '}')
        self.assertIsNotNone(self.widget.simulation, self.widget.status.text())
        blocks, _, notice = behavior_graph(self.widget.simulation)
        self.assertEqual(notice, '')
        for digit in range(6):
            block = blocks[f'HEX{digit}[7:0]']
            self.assertEqual(block.members, tuple(f'HEX{digit}{bit}' for bit in range(8)))
            self.assertEqual(block.width, 8)
        self.assertEqual(blocks['LEDR[9:0]'].members, tuple(f'LEDR{i}' for i in range(10)))
        self.assertTrue(all(f'SW{i}' in blocks for i in range(10)))

    def test_combinational_switch_display_is_visible_with_outputs_collapsed(self):
        self.widget.set_source(COMBINATIONAL)
        self.widget.output_toggle.setChecked(False)
        self.app.processEvents()
        self.assertEqual(self.widget.profile.currentData(), 'de10-lite')
        self.assertTrue(self.display.isVisible())
        self.assertTrue(self.widget.output_group.isHidden())
        self.assertEqual(set(self.widget.output_labels), {'LEDR0'})
        self.assertEqual(self.display.lit_segments(0), {0})
        self.assertTrue(all(value is None for value in self.display.digit_values(5)))
        QTest.mouseClick(self.widget.input_buttons['SW0'], Qt.MouseButton.LeftButton)
        self.assertEqual(self.display.lit_segments(0), {7})
        self.assertEqual(self.widget.simulation.cycles, 0)
        # Switching to the native profile rebuilds with the same port identities.
        self.widget.profile.setCurrentIndex(0)
        self.assertEqual(self.display.lit_segments(0), {0})
        self.widget.set_input('SW0', 1)
        self.assertEqual(self.display.lit_segments(0), {7})

    def test_segment_click_selects_exact_probe_without_toggling_inputs(self):
        self.widget.set_source(COMBINATIONAL)
        self.app.processEvents()
        point = segment_paths(self.display.digit_rect(0))[7].boundingRect().center().toPoint()
        before = dict(self.widget.simulation.values)
        QTest.mouseClick(self.display, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.widget.probe.currentText(), 'HEX07')
        self.assertEqual(self.widget.simulation.values, before)

    def test_step_run_and_reset_repaint_clocked_display(self):
        self.widget.set_source(CLOCKED)
        self.assertEqual(self.display.lit_segments(0), {0})
        self.widget.step()
        self.assertFalse(self.display.lit_segments(0))
        self.widget.reset()
        self.assertEqual(self.display.lit_segments(0), {0})
        base = perf_counter() + 60
        with patch('circuitlab.viewer.digital.perf_counter', return_value=base) as clock:
            self.widget.toggle()
            clock.return_value = base + .125
            self.widget.advance()
            self.assertEqual(self.widget.simulation.cycles, 1)
            self.assertFalse(self.display.lit_segments(0))
            self.assertTrue(self.widget.timer.isActive())
        self.widget.reset()
        self.assertEqual(self.display.lit_segments(0), {0})

    def test_rendered_segment_color_and_clearing_do_not_leave_stale_digits(self):
        self.widget.set_source(COMBINATIONAL)
        self.app.processEvents()
        paths = segment_paths(self.display.digit_rect(0))
        image = self.display.grab().toImage()
        self.assertEqual(image.pixelColor(paths[0].boundingRect().center().toPoint()).name(), LIT)
        self.assertEqual(image.pixelColor(paths[7].boundingRect().center().toPoint()).name(), UNLIT)
        self.widget.set_source('chip Logic(a,y) { a=input; y=output; y=a; }')
        self.assertTrue(self.display.isHidden())
        self.assertEqual(self.display.values, {})
        self.assertEqual(self.display.pins, {})
        self.widget.set_source(COMBINATIONAL)
        self.assertFalse(self.display.isHidden())
        self.widget.set_source('')
        self.assertTrue(self.display.isHidden())


if __name__ == '__main__':
    unittest.main()
