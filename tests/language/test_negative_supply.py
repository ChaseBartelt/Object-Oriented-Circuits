"""Vneg names a negative battery voltage without reversing its terminal names."""

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.model.graph import CircuitError


class NegativeSupplyTest(unittest.TestCase):
    def test_magnitude_and_signed_value_produce_the_same_negative_rail(self):
        for value in ('5', '-5', '5 V', '0', '2.5'):
            with self.subTest(value=value):
                source = f'battery NegSupply {{ Vneg = {value}; wire = neg; }}\nRload(1k) between(neg,0);'
                circuit = compile_circuit(source)
                source_part = next(part for part in circuit.components if part.kind == 'V')
                self.assertEqual((source_part.p, source_part.n), ('neg', '0'))
                self.assertEqual(source_part.value, -abs(float(value.split()[0])))
                result = analyze(circuit)
                voltage = result.network.voltage[result.network.nodes.index('neg')]
                self.assertAlmostEqual(voltage, source_part.value)

    def test_custom_ground_and_legacy_battery_markers_are_preserved(self):
        source = '(battery Negative)\nVneg = 5\nwire = neg\nground = reference\n(end battery)\nR1(1k) between(neg,reference);'
        circuit = compile_circuit(source)
        part = next(part for part in circuit.components if part.kind == 'V')
        self.assertEqual((part.p, part.n, part.value), ('neg', 'reference', -5))

    def test_negative_supply_supports_method_scope_and_case_insensitive_spelling(self):
        source = 'circuit rail(out) { battery supply { vNeG = 5; wire = out; } }\nmain { rail(neg); R1(1k) between(neg,0); }'
        circuit = compile_circuit(source)
        self.assertEqual(next(part.value for part in circuit.components if part.kind == 'V'), -5)

    def test_rejects_conflicting_polarities_and_complex_voltage(self):
        for fields in ('V = 5; Vneg = 5;', 'Vneg = 5; V = 5;',
                       'Vneg = 5; Vneg = 5;', 'Vneg = 2+j3;', 'Vneg = nan;'):
            with self.subTest(fields=fields), self.assertRaises(CircuitError):
                compile_circuit(f'battery supply {{ {fields} wire = neg; }}\nR1(1k) between(neg,0);')


if __name__ == '__main__':
    unittest.main()
