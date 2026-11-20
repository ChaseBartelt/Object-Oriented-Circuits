"""The eight-argument amplifier exposes gain/bypass contacts in the viewer."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.geometry import Layout
from circuitlab.viewer.schematic import SchematicView, point


class AudioSymbolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_contacts_and_labels_remain_probeable_after_rotation(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,gnd,g1,g8,bypass);", render_only=True)
        part, = circuit.components
        view = SchematicView()
        view.resize(900, 700)
        view.show()
        probed = []
        view.probed.connect(lambda kind, name: probed.append((kind, name)))
        try:
            for turns in range(4):
                layout = Layout(symbols=[(part, (0, 0), (4.3, 0), "+")],
                                rotations={part.name: turns}, pin_grid=.25)
                view.draw(circuit, layout=layout)
                self.app.processEvents()
                captions = {item.text() for item in view.scene().items() if isinstance(item, QGraphicsSimpleTextItem)}
                self.assertTrue({"GAIN1", "GAIN8", "BYPASS", "V+", "V-"} <= captions)
                self.assertNotIn("gain_default", captions)
                for net, pin in layout.pins():
                    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                                     pos=view.mapFromScene(point(pin)))
                    self.assertEqual(probed[-1], ("node", net), (turns, net))
        finally:
            view.close()
            view.deleteLater()
            self.app.processEvents()
