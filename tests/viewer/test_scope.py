"""AC run/pause controls feed the existing oscilloscope with simulated samples."""

import math
import os
import unittest
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.viewer.scope import Oscilloscope


SOURCE = "battery test { Vrms=2; phaseV=30; f=1k; wire=vin; }\nmain { series { R1=1k; } }"


class OscilloscopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.scope = Oscilloscope()

    def tearDown(self):
        self.scope.shutdown()
        if self.scope.pending:
            self.scope.pending[1].result(timeout=5)
        self.scope.close()
        self.scope.deleteLater()
        self.app.processEvents()

    def collect_batch(self):
        self.scope.pending[1].result(timeout=5)
        self.scope.advance()

    def test_ac_battery_shows_scope_and_runs_pauses_resumes_resets(self):
        scope = self.scope
        scope.set_circuit(compile_circuit(SOURCE))
        self.assertFalse(scope.isHidden())
        self.assertEqual(scope.step.value(), 10)
        scope.run.click()
        self.assertEqual(scope.run.text(), "Pause")
        self.collect_batch()
        self.assertEqual(len(scope.waveform.samples), 12)
        for time, value in scope.waveform.samples:
            self.assertAlmostEqual(value, math.sqrt(8) * math.cos(math.tau * 1000 * time + math.pi / 6))
        scope.run.click()
        self.assertFalse(scope.timer.isActive())
        self.assertEqual(scope.run.text(), "Resume")
        last = scope.waveform.samples[-1][0]
        scope.run.click()
        self.collect_batch()
        self.collect_batch()
        self.assertGreater(scope.waveform.samples[-1][0], last)
        scope.reset.click()
        self.assertIsNone(scope.simulation)
        self.assertFalse(scope.waveform.samples)
        self.assertEqual(scope.run.text(), "Run")

    def test_missing_frequency_remains_visible_and_reports_run_error(self):
        scope = self.scope
        scope.set_circuit(compile_circuit(SOURCE.replace("f=1k;", "")))
        self.assertFalse(scope.isHidden())
        scope.run.click()
        self.assertIn("frequency", scope.message.text())
        self.assertFalse(scope.timer.isActive())
        self.assertIsNone(scope.simulation)

    def test_step_tracks_new_frequency_but_preserves_adjustment_on_value_edits(self):
        scope = self.scope
        scope.set_circuit(compile_circuit(SOURCE.replace("1k; wire", "100; wire")))
        self.assertAlmostEqual(scope.step.value(), 100)
        scope.step.setValue(20)
        scope.set_circuit(None)
        scope.set_circuit(compile_circuit(SOURCE.replace("1k; wire", "100; wire").replace("Vrms=2", "Vrms=3")))
        self.assertAlmostEqual(scope.step.value(), 20)
        scope.set_circuit(compile_circuit(SOURCE.replace("1k; wire", "1MHz; wire")))
        self.assertAlmostEqual(scope.step.value(), .01)

    def test_theoretical_impedance_reports_unsupported_without_fake_waveform(self):
        scope = self.scope
        scope.set_circuit(compile_circuit("f=1k;\nV1(2 Vrms) between(vin,0);\nZ1(2+j2) between(vin,0);"))
        scope.run.click()
        self.assertIn("no time-domain model", scope.message.text())
        self.assertFalse(scope.waveform.samples)
        self.assertFalse(scope.timer.isActive())

    def test_monitor_switch_reuses_history_and_keeps_running(self):
        scope = self.scope
        scope.set_circuit(compile_circuit(
            "f=1k;\nV1(1 Vrms) between(vin,0);\nR1(1k) between(vin,out);\nR2(1k) between(out,0);"))
        scope.node.setCurrentText("out")
        scope.run.click()
        self.collect_batch()
        simulation = scope.simulation
        before = tuple(scope.waveform.samples)
        elapsed = simulation.time
        scope.node.setCurrentText("vin")
        self.assertIs(scope.simulation, simulation)
        self.assertTrue(scope.timer.isActive())
        self.assertGreaterEqual(simulation.time, elapsed)
        self.assertEqual(tuple(scope.waveform.samples), tuple((time, value * 2) for time, value in before))
        self.collect_batch()
        for time, voltage in scope.waveform.samples:
            self.assertAlmostEqual(voltage, math.sqrt(2) * math.cos(math.tau * 1000 * time))
        scope.pause()
        scope.pending[1].result(timeout=5)
        scope.finish_batch()
        elapsed = simulation.time
        scope.node.setCurrentText("out")
        self.assertEqual(simulation.time, elapsed)
        self.assertFalse(scope.timer.isActive())
        self.assertTrue(scope.waveform.samples)

    def test_busy_worker_coalesces_value_edits_without_restarting(self):
        from circuitlab.viewer.scope import sample_batch
        scope = self.scope
        source = "V1(5) between(vin,0);\nP1(10k,25%) between(vin,out,0);\nC1(1u) between(out,0);"
        scope.set_circuit(compile_circuit(source))
        scope.run.click()
        self.collect_batch()
        scope.pause()
        scope.pending[1].result(timeout=5)
        scope.finish_batch()
        history = tuple(scope.waveform.samples)
        elapsed = scope.simulation.time
        memory = scope.simulation.previous_capacitor_voltage.copy()
        started, release = Event(), Event()

        def held_batch(simulation, stop):
            started.set()
            release.wait(5)
            return sample_batch(simulation, stop)

        try:
            with patch("circuitlab.viewer.scope.sample_batch", held_batch):
                scope.toggle()
                self.assertTrue(started.wait(2))
                original = scope.simulation
                scope.set_circuit(compile_circuit(source.replace("25%", "50%")), preserve_state=True)
                scope.set_circuit(compile_circuit(source.replace("25%", "75%")), preserve_state=True)
                self.assertIs(scope.simulation, original)
                scope.pause()
                release.set()
                scope.pending[1].result(timeout=5)
                self.assertTrue(scope.refresh_simulation())
            self.assertEqual(scope.simulation.time, elapsed)
            self.assertEqual(tuple(scope.waveform.samples), history)
            self.assertEqual(scope.simulation.previous_capacitor_voltage[-1], memory[-1])
            self.assertEqual(next(p.position for p in scope.simulation.circuit.components if p.kind == "P"), .75)
            scope.toggle()
            self.collect_batch()
            self.assertGreater(scope.waveform.samples[-1][0], elapsed)
            self.assertEqual(tuple(scope.waveform.samples)[:len(history)], history)
        finally:
            release.set()

    def test_time_step_and_initial_controls_do_not_reset_live_state(self):
        scope = self.scope
        scope.set_circuit(compile_circuit("V1(5) between(vin,0);\nR1(1k) between(vin,out);\nC1(1u) between(out,0);"))
        scope.run.click()
        self.collect_batch()
        scope.pause()
        scope.pending[1].result(timeout=5)
        scope.finish_batch()
        elapsed = scope.simulation.time
        charge = scope.simulation.previous_capacitor_voltage[-1]
        history = tuple(scope.waveform.samples)
        scope.step.setValue(20)
        scope.initial.setCurrentIndex(1)
        scope.voltage.setValue(2)
        self.assertEqual(scope.simulation.time, elapsed)
        self.assertEqual(scope.simulation.previous_capacitor_voltage[-1], charge)
        self.assertEqual(tuple(scope.waveform.samples), history)
        self.assertAlmostEqual(scope.simulation.step, 20e-6)
        scope.toggle()
        self.collect_batch()
        self.assertGreater(scope.waveform.samples[-1][0], elapsed)
        scope.reset.click()
        self.assertFalse(scope.history)
        self.assertIsNone(scope.simulation)


if __name__ == "__main__":
    unittest.main()
