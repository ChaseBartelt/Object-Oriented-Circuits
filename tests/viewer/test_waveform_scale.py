"""The voltage gutter controls a persistent display scale without editing state."""

import math
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.viewer.scope import Oscilloscope, Waveform


class WaveformScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.plot = Waveform()
        self.plot.resize(520, 350)
        self.plot.samples.extend((time * 1e-3, -2.0) for time in range(20))
        self.plot.show()
        self.app.processEvents()

    def tearDown(self):
        self.plot.close()
        self.plot.deleteLater()
        self.app.processEvents()

    def drag(self, distance, x=30):
        start = QPoint(x, round(self.plot.plot_area().center().y()))
        end = start + QPoint(0, distance)
        QTest.mousePress(self.plot, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.plot, end)
        QTest.mouseRelease(self.plot, Qt.MouseButton.LeftButton, pos=end)
        self.app.processEvents()

    def test_drag_up_and_down_change_divisions_around_the_current_negative_dc_level(self):
        low, high = self.plot.voltage_bounds()
        samples = tuple(self.plot.samples)
        self.drag(-60)
        zoomed = self.plot.voltage_bounds()
        self.assertFalse(self.plot.auto_voltage)
        self.assertLess(zoomed[1] - zoomed[0], high - low)
        self.assertAlmostEqual(sum(zoomed) / 2, -2)
        self.drag(90)
        wide = self.plot.voltage_bounds()
        self.assertGreater(wide[1] - wide[0], zoomed[1] - zoomed[0])
        self.assertAlmostEqual(sum(wide) / 2, -2)
        self.assertEqual(tuple(self.plot.samples), samples)

    def test_manual_range_survives_samples_node_changes_clear_and_time_unit_changes(self):
        self.drag(-40)
        bounds = self.plot.voltage_bounds()
        self.plot.samples.clear()
        self.plot.samples.extend(((0, 20.0), (.001, 100.0)))
        self.plot.set_time_unit("ms", .001)
        self.assertEqual(self.plot.voltage_bounds(), bounds)
        QTest.mouseDClick(self.plot, Qt.MouseButton.LeftButton, pos=QPoint(30, 100))
        self.assertTrue(self.plot.auto_voltage)
        low, high = self.plot.voltage_bounds()
        self.assertLessEqual(low, 20)
        self.assertGreaterEqual(high, 100)

    def test_dragging_the_trace_does_not_change_the_voltage_scale(self):
        bounds = self.plot.voltage_bounds()
        self.drag(-40, x=200)
        self.assertTrue(self.plot.auto_voltage)
        self.assertEqual(self.plot.voltage_bounds(), bounds)

    def test_empty_constant_and_extreme_finite_samples_render_on_neutral_grey(self):
        for samples in ((), ((0, -2),), ((0, 1e20), (.001, 1e20)),
                        ((0, -1e308), (.001, 1e308)), ((0, -2), (0, -2))):
            with self.subTest(samples=samples):
                self.plot.samples.clear()
                self.plot.samples.extend(samples)
                low, high = self.plot.voltage_bounds()
                self.assertTrue(math.isfinite(low) and math.isfinite(high))
                self.assertLess(low, high)
                bitmap = self.plot.grab().toImage()
                background = bitmap.pixelColor(1, 1)
                self.assertEqual((background.red(), background.green(), background.blue()), (51, 51, 51))


class ScopeManualScaleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_auto_control_and_drag_change_only_the_viewport(self):
        scope = Oscilloscope(compact=True)
        try:
            scope.resize(420, 520)
            scope.set_circuit(compile_circuit("analog V(out)=integrate(1);"))
            scope.toggle()
            scope.pending[1].result(timeout=5)
            scope.pause()
            scope.finish_batch()
            self.app.processEvents()
            simulation, history = scope.simulation, tuple(scope.history)
            memories = dict(simulation.behavior_state.memory)
            with patch.object(simulation, "reconfigure", side_effect=AssertionError("display-only edit")):
                scope.auto_voltage.click()
                self.assertFalse(scope.waveform.auto_voltage)
                self.assertFalse(scope.auto_voltage.isChecked())
                scope.time_unit.setCurrentText("ms")
                scope.auto_voltage.click()
                self.assertTrue(scope.waveform.auto_voltage)
                self.assertTrue(scope.auto_voltage.isChecked())
                QTest.mousePress(scope.waveform, Qt.MouseButton.LeftButton, pos=QPoint(25, 80))
                QTest.mouseMove(scope.waveform, QPoint(25, 50))
                QTest.mouseRelease(scope.waveform, Qt.MouseButton.LeftButton, pos=QPoint(25, 50))
                self.assertFalse(scope.auto_voltage.isChecked())
                QTest.mouseDClick(scope.waveform, Qt.MouseButton.LeftButton, pos=QPoint(25, 80))
                self.assertTrue(scope.auto_voltage.isChecked())
            self.assertIs(scope.simulation, simulation)
            self.assertEqual(tuple(scope.history), history)
            self.assertEqual(simulation.behavior_state.memory, memories)
        finally:
            scope.shutdown()
            scope.close()
            scope.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
