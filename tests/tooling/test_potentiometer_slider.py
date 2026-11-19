"""Potentiometer gestures preview locally and commit one checked source edit."""
# Preview changes stay local until the gesture commits one acknowledged source transaction.
# Debouncing, stale worker results, Undo, and Discard must preserve the correct source revision.
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from circuitlab.layout.persistence import pack_layout
from circuitlab.math.analysis import analyze
from circuitlab.viewer.schematic import point
from circuitlab.viewer.window import ViewerWindow


SOURCE = 'V1(8) between(high,0);\nP1(10k,25%) between(high,out,0); # keep\n'


class PotentiometerSliderTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / 'slider.ckt'
        self.path.write_text(SOURCE, encoding='utf-8')
        self.window = ViewerWindow()
        self.window.resize(1200, 800)
        self.window.show()
        self.window.open_path(self.path)
        self.app.processEvents()
        self.tools = self.window.source_tools
        self.view = self.window.schematic

    def tearDown(self):
        self.tools.cancel_position()
        self.tools.checkpoint()
        self.window.placement_dirty = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for(self, predicate):
        deadline = monotonic() + 4
        while not predicate() and monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def voltage(self):
        result = self.window.solution
        return None if result is None else result.network.voltage[result.network.nodes.index('out')].real

    def test_click_drag_preview_commits_one_edit_and_undo_restores_gesture(self):
        layout = pack_layout(self.view.layout)
        _, a, b, _ = next(row for row in self.view.layout.symbols if row[0].name == 'P1')
        center = point(tuple((x + y) / 2 for x, y in zip(a, b)))
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=self.view.mapFromScene(center))
        self.assertFalse(self.view.position_panel.isHidden())
        slider = self.view.position_slider
        self.assertEqual(slider.value(), 25)
        slider.setSliderDown(True)
        for position in (0, 40, 100, 75):
            slider.setValue(position)
        self.wait_for(lambda: self.voltage() == 6)
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertEqual(self.window.session.source.replace('\r\n', '\n'), SOURCE)
        self.assertFalse(self.tools.history)
        slider.setSliderDown(False)
        self.assertEqual(self.path.read_text(), SOURCE.replace('10k,25%', '10k,75%'))
        self.assertEqual(len(self.tools.history), 1)
        self.assertEqual(pack_layout(self.view.layout), layout)
        self.tools.undo()
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertEqual(self.voltage(), 2)
        self.assertEqual(pack_layout(self.view.layout), layout)

    def test_keyboard_adjustments_debounce_one_bridge_request(self):
        requests = []
        self.tools.sender = requests.append
        self.tools.open_potentiometer('P1')
        slider = self.view.position_slider
        slider.setFocus()
        for _ in range(3):
            QTest.keyClick(slider, Qt.Key.Key_Right)
        self.assertFalse(requests)
        self.wait_for(lambda: len(requests) == 1)
        self.assertIn('P1(10k,28%)', requests[0]['after'])
        self.assertEqual(self.path.read_text(), SOURCE)
        self.assertFalse(self.tools.history)
        self.path.write_text(requests[0]['after'], encoding='utf-8')
        self.tools.result({'id': requests[0]['id'], 'ok': True})
        self.assertEqual(len(self.tools.history), 1)
        QTest.qWait(350)
        self.assertEqual(len(requests), 1)

    def test_external_reload_discards_a_slow_stale_preview(self):
        entered, release = Event(), Event()

        def slow(circuit):
            entered.set()
            release.wait(3)
            return analyze(circuit)

        self.tools.open_potentiometer('P1')
        with patch('circuitlab.viewer.source_editing.analyze', side_effect=slow):
            try:
                self.tools.adjust_position('P1', 75)
                self.wait_for(entered.is_set)
                self.path.write_text(SOURCE.replace('25%', '50%'), encoding='utf-8')
                self.window.session.load(self.path)
                self.window.compile_source()
            finally:
                release.set()
            self.wait_for(lambda: self.tools.position_future[1].done())
            QTest.qWait(150)
        self.assertEqual(self.voltage(), 4)
        self.assertEqual(next(part.position for part in self.view.circuit.components if part.kind == 'P'), .5)
        self.assertTrue(self.view.position_panel.isHidden())
        self.assertFalse(self.tools.history)

    def test_placeholder_position_and_discard_preserve_unspecified_resistance(self):
        source = SOURCE.replace('P1(10k,25%)', 'P1')
        self.path.write_text(source, encoding='utf-8')
        self.window.open_path(self.path)
        self.tools.open_potentiometer('P1')
        self.tools.adjust_position('P1', 75, final=True)
        self.assertEqual(self.path.read_text(), source.replace('P1 between', 'P1(,75%) between'))
        self.assertIsNone(self.window.solution)
        self.assertTrue(self.tools.discard_values())
        self.assertEqual(self.path.read_text(), source)

    def test_preview_does_not_change_a_different_part_on_the_same_line(self):
        source = ('V1(8) between(high,0);\nmain {\n'
                  'P1(10k,25%) between(high,out,0); P2(10k,50%) between(high,second,0);\n}\n')
        self.path.write_text(source, encoding='utf-8')
        self.window.open_path(self.path)
        self.tools.open_potentiometer('P1')
        self.tools.adjust_position('P1', 75)
        self.wait_for(lambda: self.voltage() == 6)
        positions = {part.name: part.position for part in self.view.circuit.components if part.kind == 'P'}
        self.assertEqual(positions, {'P1': .75, 'P2': .5})
        self.tools.cancel_position()
        self.assertEqual(self.voltage(), 2)
        self.assertEqual(self.path.read_text(), source)


if __name__ == '__main__':
    unittest.main()
