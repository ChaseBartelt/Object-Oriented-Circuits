"""Time-unit display changes preserve the physical sampling interval and state."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.viewer.scope import Oscilloscope


class ScopeTimeUnitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.scope = Oscilloscope(compact=True)

    def tearDown(self):
        self.scope.shutdown()
        if self.scope.pending:
            self.scope.pending[1].result(timeout=5)
        self.scope.close()
        self.scope.deleteLater()
        self.app.processEvents()

    def unit(self, label):
        self.scope.time_unit.setCurrentText(label)

    def start_and_pause(self):
        self.scope.set_circuit(compile_circuit("analog V(out)=integrate(1);"))
        self.scope.toggle()
        self.scope.pending[1].result(timeout=5)
        self.scope.pause()
        self.scope.finish_batch()

    def test_default_step_and_all_display_conversions_keep_canonical_seconds(self):
        scope = self.scope
        self.assertEqual([scope.time_unit.itemText(i) for i in range(scope.time_unit.count())],
                         ["ns", "µs", "ms", "s"])
        self.assertEqual(scope.time_unit.currentText(), "µs")
        self.assertEqual(scope.step.value(), 10)
        seconds = scope.step_seconds()
        for label, number in (("ns",10000), ("ms",.01), ("s",.00001), ("µs",10)):
            with self.subTest(unit=label):
                self.unit(label)
                self.assertAlmostEqual(scope.step.value(), number)
                self.assertEqual(scope.step_seconds(), seconds)
                self.assertEqual(scope.step.suffix(), "")

    def test_unit_only_change_keeps_the_running_solver_and_history_exactly(self):
        self.start_and_pause()
        scope = self.scope
        simulation, seconds = scope.simulation, scope.step_seconds()
        elapsed, history = simulation.time, tuple(scope.waveform.samples)
        memories = dict(simulation.behavior_state.memory)
        with patch.object(simulation,"reconfigure",side_effect=AssertionError("unit-only restamp")):
            for label in ("ns", "ms", "s", "µs"):
                self.unit(label)
                self.assertTrue(scope.refresh_simulation())
                self.assertIs(scope.simulation, simulation)
                self.assertEqual(scope.step_seconds(), seconds)
                self.assertEqual(simulation.time, elapsed)
                self.assertEqual(simulation.behavior_state.memory, memories)
                self.assertEqual(tuple(scope.waveform.samples), history)

    def test_editing_selected_unit_changes_interval_without_resetting_integrator(self):
        self.start_and_pause()
        scope = self.scope
        elapsed, history = scope.simulation.time, tuple(scope.waveform.samples)
        self.unit("ms")
        scope.step.setValue(.5)
        self.assertAlmostEqual(scope.simulation.step,.0005)
        self.assertAlmostEqual(scope.step_seconds(),.0005)
        self.assertEqual(scope.simulation.time,elapsed)
        self.assertEqual(tuple(scope.waveform.samples),history)
        scope.toggle()
        scope.pending[1].result(timeout=5)
        scope.pause()
        scope.finish_batch()
        self.assertGreater(scope.simulation.time,elapsed)
        for time, voltage in scope.waveform.samples:
            self.assertAlmostEqual(voltage,time)

    def test_seconds_are_usable_and_picosecond_minimum_survives_every_unit(self):
        scope = self.scope
        self.unit("s")
        scope.step.setValue(1)
        self.assertEqual(scope.step_seconds(),1)
        self.unit("ns")
        self.assertEqual(scope.step.value(),1e9)
        scope.step.setValue(.001)
        seconds = scope.step_seconds()
        self.assertAlmostEqual(seconds,1e-12,places=20)
        for label in ("µs", "ms", "s", "ns"):
            self.unit(label)
            self.assertEqual(scope.step_seconds(),seconds)
            self.assertGreater(scope.step.value(),0)

    def test_frequency_auto_step_uses_seconds_in_the_selected_unit(self):
        scope = self.scope
        self.unit("ms")
        source = "f=1k;\nV1(1 Vrms) between(vin,0);\nR1(1k) between(vin,0);"
        scope.set_circuit(compile_circuit(source))
        self.assertEqual(scope.time_unit.currentText(),"ms")
        self.assertAlmostEqual(scope.step.value(),.01)
        self.assertAlmostEqual(scope.step_seconds(),1e-5)
        self.unit("ns")
        scope.set_circuit(compile_circuit(source.replace("f=1k", "f=1MHz")))
        self.assertAlmostEqual(scope.step.value(),10)
        self.assertAlmostEqual(scope.step_seconds(),1e-8)

    def test_time_axis_is_notified_of_selected_unit_without_touching_samples(self):
        self.start_and_pause()
        waveform = self.scope.waveform
        history = tuple(waveform.samples)
        with patch.object(waveform,"set_time_unit",wraps=waveform.set_time_unit) as update:
            self.unit("ms")
            update.assert_called_once_with("ms",.001)
        self.assertEqual(tuple(waveform.samples),history)

    def test_step_editor_has_usable_width_in_both_scope_layouts(self):
        for compact in (False,True):
            scope = Oscilloscope(compact=compact)
            try:
                scope.resize(800 if not compact else 350,400)
                scope.set_circuit(compile_circuit("analog V(out)=integrate(1);"))
                self.app.processEvents()
                for label in ("ns", "s"):
                    scope.time_unit.setCurrentText(label)
                    self.app.processEvents()
                    self.assertGreaterEqual(scope.step.width(),60)
                    self.assertEqual(scope.time_unit.width(),58)
            finally:
                scope.shutdown()
                scope.close()
                scope.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
