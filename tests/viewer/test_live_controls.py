"""Potentiometer source edits and the running AC scope share the same circuit."""
# Source-backed value edits and the running scope must share one accepted circuit revision.
# Preview, acknowledgement, rejection, Undo, and pause/resume must retain stored physical history.

import os
from math import cos, pi, sqrt
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.layout.persistence import pack_layout
from circuitlab.tooling.source_edits import edit_value
from circuitlab.viewer.window import ViewerWindow


SOURCE = "f = 1k\nVac(1 Vrms) between(high, 0);\nP1(10k, 25%) between(high, out, 0);\n"


class LiveControlsTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "volume.ckt"
        self.path.write_text(SOURCE, encoding="utf-8")
        self.window = ViewerWindow()
        self.window.show()
        self.window.open_path(self.path)
        self.app.processEvents()

    def tearDown(self):
        self.window.source_tools.checkpoint()
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for(self, predicate, timeout=3):
        until = monotonic() + timeout
        while not predicate() and monotonic() < until:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def run_scope(self):
        scope = self.window.scope
        self.assertFalse(scope.isHidden())
        scope.node.setCurrentText("out")
        scope.step.setValue(5)
        scope.toggle()
        self.wait_for(lambda: len(scope.waveform.samples) >= 12)
        self.assertTrue(scope.timer.isActive())
        return scope

    @staticmethod
    def position(circuit):
        return next(part.position for part in circuit.components if part.kind == "P")

    def wait_for_position_samples(self, scope, position):
        self.wait_for(lambda: scope.simulation is not None
                      and self.position(scope.simulation.circuit) == position)
        changed_at = scope.simulation.time
        self.wait_for(lambda: len(scope.waveform.samples) >= 12
                      and scope.waveform.samples[-12][0] > changed_at)
        return tuple(scope.waveform.samples)[-12:]

    def assert_continuous(self, scope, previous):
        samples = tuple(scope.waveform.samples)
        self.assertIn(previous[-1], samples)
        self.assertTrue(all(a[0] < b[0] for a, b in zip(samples, samples[1:])))
        self.assertGreaterEqual(scope.simulation.time, previous[-1][0])

    def test_running_ac_scope_uses_committed_position_and_undo_restores_it(self):
        scope = self.run_scope()
        tools = self.window.source_tools
        layout = pack_layout(self.window.schematic.layout)
        before = tuple(scope.waveform.samples)
        tools.open_potentiometer("P1")
        tools.adjust_position("P1", 75, final=True)
        self.wait_for(lambda: self.position(scope.circuit) == .75 and scope.timer.isActive())
        self.assertIn("10k", self.path.read_text())
        self.assertEqual(scope.node.currentText(), "out")
        self.assertEqual(scope.step.value(), 5)
        self.wait_for(lambda: scope.simulation is not None and self.position(scope.simulation.circuit) == .75)
        self.assert_continuous(scope, before)
        net = self.window.solution.network
        self.assertAlmostEqual(net.voltage[net.nodes.index("out")], .75)
        self.assertEqual(pack_layout(self.window.schematic.layout), layout)
        self.assertEqual(len(tools.history), 1)
        before_undo = tuple(scope.waveform.samples)
        tools.undo()
        self.wait_for(lambda: self.position(scope.simulation.circuit) == .25)
        self.assert_continuous(scope, before_undo)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertTrue(scope.timer.isActive())
        self.assertEqual(pack_layout(self.window.schematic.layout), layout)

    def test_held_slider_previews_waveform_without_writing_source(self):
        scope = self.run_scope()
        tools = self.window.source_tools
        before = tuple(scope.waveform.samples)
        tools.open_potentiometer("P1")
        slider = self.window.schematic.position_slider
        slider.setSliderDown(True)
        slider.setValue(75)
        preview_samples = self.wait_for_position_samples(scope, .75)
        self.assertEqual(scope.node.currentText(), "out")
        self.assertEqual(scope.step.value(), 5)
        self.assertTrue(scope.timer.isActive())
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertFalse(tools.history)
        self.assert_continuous(scope, before)
        for time, value in preview_samples:
            self.assertAlmostEqual(value, .75 * sqrt(2) * cos(2 * pi * 1000 * time), places=7)
        before_cancel = tuple(scope.waveform.samples)
        tools.cancel_position()
        restored_samples = self.wait_for_position_samples(scope, .25)
        self.assert_continuous(scope, before_cancel)
        for time, value in restored_samples:
            self.assertAlmostEqual(value, .25 * sqrt(2) * cos(2 * pi * 1000 * time), places=7)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertTrue(scope.timer.isActive())

    def test_cancel_preview_respects_pause_and_keeps_scope_settings(self):
        scope = self.run_scope()
        tools = self.window.source_tools
        tools.open_potentiometer("P1")
        tools.adjust_position("P1", 75)
        self.wait_for(lambda: self.position(scope.circuit) == .75)
        scope.pause()
        tools.cancel_position()
        self.assertEqual(self.position(scope.circuit), .25)
        self.assertFalse(scope.timer.isActive())
        self.assertEqual(scope.node.currentText(), "out")
        self.assertEqual(scope.step.value(), 5)
        self.assertEqual(self.path.read_text(), SOURCE)

    def test_ide_preview_does_not_write_until_release_and_ack(self):
        scope = self.run_scope()
        before = tuple(scope.waveform.samples)
        tools = self.window.source_tools
        requests = []
        tools.sender = requests.append
        tools.open_potentiometer("P1")
        tools.adjust_position("P1", 75)
        self.wait_for(lambda: self.position(self.window.solution.circuit) == .75)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertEqual(self.window.session.source.replace('\r\n', '\n'), SOURCE)
        self.assertFalse(requests)
        tools.adjust_position("P1", 75, final=True)
        self.assertEqual(len(requests), 1)
        self.assertTrue(tools.pending)
        request = requests[0]
        self.path.write_bytes(request["after"].encode("utf-8"))
        tools.result({"id": request["id"], "ok": True})
        self.assertFalse(tools.pending)
        self.assertEqual(len(tools.history), 1)
        self.assertEqual(self.position(self.window.scope.circuit), .75)
        self.assertTrue(self.window.scope.timer.isActive())
        self.wait_for(lambda: self.position(scope.simulation.circuit) == .75)
        self.assert_continuous(scope, before)

    def test_rejected_ide_edit_restores_preview_without_changing_running_source(self):
        scope = self.run_scope()
        before = tuple(scope.waveform.samples)
        tools = self.window.source_tools
        requests = []
        tools.sender = requests.append
        tools.open_potentiometer("P1")
        tools.adjust_position("P1", 75)
        self.wait_for(lambda: self.position(self.window.solution.circuit) == .75)
        tools.adjust_position("P1", 75, final=True)
        with patch("circuitlab.viewer.source_editing.QMessageBox.warning"):
            tools.result({"id": requests[0]["id"], "ok": False, "error": "IDE changed meanwhile"})
        self.app.processEvents()
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertEqual(self.position(self.window.solution.circuit), .25)
        self.assertEqual(self.position(self.window.schematic.circuit), .25)
        self.assertEqual(self.position(self.window.scope.circuit), .25)
        self.wait_for(lambda: self.window.scope.simulation is not None
                      and self.position(self.window.scope.simulation.circuit) == .25)
        self.assertFalse(tools.history)
        self.assertTrue(self.window.scope.timer.isActive())
        self.assert_continuous(scope, before)

    def test_resistor_edit_and_undo_preserve_paused_capacitor_voltage(self):
        source = 'V1(5) between(high,0);\nR1(1k) between(high,out);\nC1(1u) between(out,0);\n'
        self.path.write_text(source, encoding='utf-8')
        self.window.open_path(self.path)
        scope = self.run_scope()
        scope.pause()
        if scope.pending:
            scope.pending[1].result(timeout=5)
        simulation = scope.simulation
        previous_time = simulation.time
        capacitor = next(i for i in simulation.capacitors if simulation.parts[i].name == 'C1')
        previous_voltage = simulation.previous_capacitor_voltage[capacitor]
        self.assertGreater(previous_voltage, 0)
        tools = self.window.source_tools
        plan = edit_value(self.window.schematic.circuit, 'R1', '2k', self.path,
                          self.window.session.source, self.window.read_linked_source)
        tools.apply(plan)
        self.wait_for(lambda: scope.simulation is not simulation)
        self.assertFalse(scope.timer.isActive())
        self.assertEqual(scope.simulation.time, previous_time)
        self.assertAlmostEqual(scope.simulation.previous_capacitor_voltage[capacitor], previous_voltage)
        changed = scope.simulation
        tools.undo()
        self.wait_for(lambda: scope.simulation is not changed)
        self.assertFalse(scope.timer.isActive())
        self.assertEqual(scope.simulation.time, previous_time)
        self.assertAlmostEqual(scope.simulation.previous_capacitor_voltage[capacitor], previous_voltage)
        self.assertEqual(self.path.read_text(), source)

    def test_legacy_editor_value_write_keeps_running_scope(self):
        from circuitlab.viewer.legacy import MainWindow

        window = MainWindow()
        try:
            window.open_path(self.path)
            window.compile_source()
            scope = window.scope
            scope.node.setCurrentText('out')
            scope.toggle()
            self.wait_for(lambda: len(scope.waveform.samples) >= 12)
            before = tuple(scope.waveform.samples)
            window.source_tools.open_potentiometer('P1')
            window.source_tools.adjust_position('P1', 75, final=True)
            self.wait_for(lambda: self.position(scope.simulation.circuit) == .75)
            self.assertTrue(scope.timer.isActive())
            self.assert_continuous(scope, before)
            self.assertIn('75%', window.editor.toPlainText())
            self.assertEqual(self.path.read_text(), SOURCE)
        finally:
            window.placement_dirty = False
            for editor, state in window.documents.items():
                state.placement_dirty = False
                editor.document().setModified(False)
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_background_ide_value_save_keeps_running_time_and_history(self):
        scope = self.run_scope()
        before = tuple(scope.waveform.samples)
        self.path.write_text(SOURCE.replace('25%', '75%'), encoding='utf-8')
        self.assertTrue(self.window.refresh_from_editor(self.path))
        self.wait_for(lambda: self.position(scope.simulation.circuit) == .75)
        self.assertTrue(scope.timer.isActive())
        self.assert_continuous(scope, before)
        self.assertEqual(scope.node.currentText(), 'out')
        self.assertEqual(scope.step.value(), 5)


if __name__ == "__main__":
    unittest.main()
