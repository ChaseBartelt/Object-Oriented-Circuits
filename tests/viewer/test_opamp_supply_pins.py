"""Optional op-amp power pins remain visible and probeable after rotation."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.geometry import Layout
from circuitlab.viewer.schematic import SchematicView, point


class OpAmpSupplyPinsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = SchematicView()
        self.view.resize(800, 500)
        self.view.show()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def draw_amp(self, *, powered, rotation=0):
        pins = "plus,minus,out,vplus,vminus" if powered else "plus,minus,out"
        circuit = compile_circuit(f"op1({pins});", render_only=True)
        amp = next(part for part in circuit.components if part.kind == "O")
        layout = Layout(symbols=[(amp, (0, 0), (4, 0), "+")], positions={amp.name: (0, 0)},
                        rotations={amp.name: rotation}, pin_grid=.25)
        self.view.draw(circuit, layout=layout)
        self.app.processEvents()
        return layout

    def power_captions(self):
        return {item.text(): item for item in self.view.scene().items()
                if isinstance(item, QGraphicsSimpleTextItem) and item.text() in {"+V", "-V"}}

    def test_three_pin_opamp_keeps_original_symbol(self):
        layout = self.draw_amp(powered=False)
        self.assertEqual({net for net, _ in layout.pins()}, {"plus", "minus", "out"})
        self.assertFalse(self.power_captions())
        self.assertNotIn("vplus", self.view.anchors)
        self.assertNotIn("vminus", self.view.anchors)

    def test_five_pin_opamp_rail_endpoints_and_labels_probe_their_nets(self):
        probed = []
        self.view.probed.connect(lambda kind, name: probed.append((kind, name)))
        layout = self.draw_amp(powered=True)
        pins = dict(layout.pins())
        self.assertEqual(set(pins), {"plus", "minus", "out", "vplus", "vminus"})
        self.assertEqual(pins["vplus"], (2, 1.25))
        self.assertEqual(pins["vminus"], (2, -1.25))
        captions = self.power_captions()
        self.assertEqual(set(captions), {"+V", "-V"})
        for text, net in (("+V", "vplus"), ("-V", "vminus")):
            with self.subTest(net=net):
                label = captions[text]
                self.assertEqual(label.data(1), net)
                QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton,
                                 pos=self.view.mapFromScene(label.sceneBoundingRect().center()))
                self.assertEqual(probed[-1], ("node", net))
                QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton,
                                 pos=self.view.mapFromScene(point(pins[net])))
                self.assertEqual(probed[-1], ("node", net))

    def test_rotated_power_pins_keep_their_probe_identity(self):
        probed = []
        self.view.probed.connect(lambda kind, name: probed.append((kind, name)))
        for rotation in (1, 2, 3):
            with self.subTest(rotation=rotation):
                layout = self.draw_amp(powered=True, rotation=rotation)
                self.assertEqual(set(self.power_captions()), {"+V", "-V"})
                pins = dict(layout.pins())
                for net in ("vplus", "vminus"):
                    QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton,
                                     pos=self.view.mapFromScene(point(pins[net])))
                    self.assertEqual(probed[-1], ("node", net))


if __name__ == "__main__":
    unittest.main()
