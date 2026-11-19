"""Check physical potentiometer equivalents and exported pin connectivity."""
# Validate both resistor legs and exact endpoint shorts, then check all three exported
# terminal contacts independently of the viewer glyph.
import unittest

from circuitlab.exporters.ltspice import export_hierarchy, export_schematic
from circuitlab.exporters.spice import spice_circuit, spice_netlist
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.geometry import Layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.routing import fast_layout
from circuitlab.math.report import component_labels
from tests.exporters.test_ltspice import read_connections


class PotentiometerExportTest(unittest.TestCase):
    def circuit(self, position):
        return compile_circuit(
            f'V1(8) between(high, 0);\nP1(200, {position}) between(high, wiper, 0);\n'
            'Rload(50) between(wiper, 0);'
        )

    def test_netlist_has_both_legs_and_exact_endpoint_shorts(self):
        for position, kinds, values in (
            ('25%', ('R', 'R'), (150, 50)),
            ('0%', ('R', 'V'), (200, 0)),
            ('100%', ('V', 'R'), (0, 200)),
        ):
            with self.subTest(position=position):
                circuit = self.circuit(position)
                spice = spice_circuit(circuit)
                rows = {row.split()[0]: row.split()[1:] for row in spice_netlist(circuit).splitlines() if row}
                for leg, kind, value, nets in zip(spice.potentiometers['P1'], kinds, values,
                                                  (('high', 'wiper'), ('wiper', '0'))):
                    model = spice.parts[leg.name]
                    self.assertEqual(model.kind, kind)
                    self.assertEqual(float(model.value), value)
                    self.assertEqual(tuple(rows[model.name][:2]), nets)
                    if kind == 'V':
                        self.assertEqual(rows[model.name][2:], ['DC', '0'])

    def test_ltspice_preserves_all_three_pins_at_every_rotation_and_endpoint(self):
        for position in ('0%', '25%', '100%'):
            circuit = self.circuit(position)
            pot = next(part for part in circuit.components if part.kind == 'P')
            spice = spice_circuit(circuit)
            for turns in range(4):
                with self.subTest(position=position, turns=turns):
                    # Isolate the symbol geometry; flags connect this drawing to its circuit nets.
                    layout = Layout(symbols=[(pot, (0, 0), (4, 0), '+')],
                                    rotations={pot.name: turns}, pin_grid=.5)
                    sheet, _ = export_schematic(circuit, layout)
                    connections = read_connections(sheet)
                    for leg, nets in zip(spice.potentiometers[pot.name], (('high', 'wiper'), ('wiper', '0'))):
                        model = spice.parts[leg.name]
                        self.assertEqual(connections[model.name][0], nets)

    def test_rheostat_tied_wiper_exports_without_self_connected_source(self):
        circuit = compile_circuit('V1(5) between(high, 0);\nP1(1k, 0%) between(high, 0, 0);')
        spice = spice_circuit(circuit)
        self.assertIsNone(spice.potentiometers['P1'][1])
        self.assertNotIn('V_P1_low', spice_netlist(circuit))
        sheet, _ = export_schematic(circuit, fast_layout(circuit, component_labels(circuit)))
        self.assertEqual(read_connections(sheet)[spice.parts['R_P1_high'].name][0], ('high', '0'))

    def test_names_and_nested_chip_export_do_not_collide(self):
        circuit = compile_circuit('''
            chip Level(high, wiper, low) {
                P1(200,25%) between(high,wiper,low);
                R_P1_high(50) between(wiper,low);
            }
            V1(8) between(high,0);
            main { Level(high,wiper,0); }
        ''')
        spice = spice_circuit(circuit)
        self.assertEqual(len({model.name.casefold() for model in spice.parts.values()}), len(spice.parts))
        top = next(hood_levels(circuit, set()))[1]
        sheet, files, _ = export_hierarchy(circuit, fast_layout(top, component_labels(top)))
        self.assertIn('SYMBOL', sheet)
        details = [text for name, text in files.items() if name.endswith('.asc')]
        self.assertEqual(len(details), 1)
        connections = read_connections(details[0])
        pot_legs = next(iter(spice.potentiometers.values()))
        for leg, nets in zip(pot_legs, (('high', 'wiper'), ('wiper', 'low'))):
            self.assertEqual(connections[spice.parts[leg.name].name][0], nets)


if __name__ == '__main__':
    unittest.main()
