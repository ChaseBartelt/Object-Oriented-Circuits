"""Potentiometer glyphs expose fixed terminals and an adjustable wiper arrow."""

from dataclasses import replace
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QGraphicsRectItem

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.geometry import Layout, rotate_point, symbol_pins
from circuitlab.layout.persistence import pack_layout
from circuitlab.math.analysis import solve_input
from circuitlab.viewer.schematic import SchematicView, point
from circuitlab.viewer.widgets import probe_text


class PotentiometerViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit('P1(200,25%) between(high,tap,low);', render_only=True)
        self.part = self.circuit.components[0]
        self.view = SchematicView()
        self.addCleanup(self.view.deleteLater)

    def test_arrow_tracks_position_and_all_external_leads_stay_fixed(self):
        a, b = (0, 0), (4, 0)
        self.view.pin_grid = .25
        for position in (0, .25, .5, 1):
            with self.subTest(position=position):
                part = replace(self.part, position=position)
                with patch.object(self.view, 'lines') as lines:
                    self.view.symbol(part, a, b, '+')
                paths = [call.args[0] for call in lines.call_args_list]
                pins = dict(symbol_pins(part, a, b, '+', grid=.25))
                self.assertEqual(paths[0][0], pins['high'])
                self.assertEqual(paths[2][-1], pins['low'])
                self.assertEqual(paths[3][0], pins['tap'])
                tip = paths[3][-1]
                self.assertAlmostEqual(tip[1], -.6 + 1.2 * position)
                self.assertEqual(paths[4][1], tip)
                self.assertLess(tip[0], paths[3][-2][0])

    def test_native_symbol_and_hit_area_rotate_with_all_three_terminals(self):
        for turns in range(4):
            with self.subTest(turns=turns):
                a, b = (0, 0), (4, 0)
                layout = Layout(symbols=[(self.part, a, b, '+')], positions={'P1': a},
                                rotations={'P1': turns}, pin_grid=.25)
                before = pack_layout(layout)
                self.view.draw(self.circuit, layout=layout)
                self.assertIs(self.view.layout, layout)
                self.assertEqual(pack_layout(layout), before)
                handles = [item for item in self.view.scene().items()
                           if isinstance(item, QGraphicsRectItem)
                           and item.data(0) == 'component' and item.data(1) == 'P1']
                self.assertEqual(len(handles), 1)
                for _, pin in symbol_pins(self.part, a, b, '+', grid=.25):
                    rotated = point(rotate_point(pin, a, b, turns))
                    self.assertTrue(handles[0].contains(handles[0].mapFromScene(rotated)))
                self.assertFalse(any(item.data(0) == 'hood' for item in self.view.scene().items()))

    def test_loaded_potentiometer_probe_names_each_terminal_current(self):
        result = solve_input('V1(7) between(high,0);\nP1(200,25%) between(high,tap,0);\n'
                             'Rload(50) between(tap,0);')
        # The loaded lower leg is 25 ohms, giving 40mA into high and 20mA out of each return.
        for actual, expected in zip(result.network.terminal_current['P1'], (.04, -.02, -.02)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(probe_text(result, 'component', 'P1'),
                         'P1 high-low voltage = 7 V   |   current into high = 0.04 A   |   '
                         'current into wiper = -0.02 A   |   current into low = -0.02 A')
        self.assertEqual(probe_text(result, 'component', 'Rload'),
                         'Rload voltage = 1 V   |   current = 0.02 A')


if __name__ == '__main__':
    unittest.main()
