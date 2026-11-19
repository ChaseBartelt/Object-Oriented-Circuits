"""Editor help and snippets follow manual-switch and supply syntax."""
# Help, snippets, and grammar should describe the same switch/supply syntax the compiler accepts.
# Similar names in digital declarations must not trigger unrelated electrical help.

import json
from pathlib import Path
import re
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.tooling.source_info import SourceInfo
from tests.tooling.test_ide_snippets import default_snippet


class SwitchSupplyEditorTest(unittest.TestCase):
    def test_switch_help_and_wire_hover_use_two_terminals(self):
        source = 'Sbutton(open) between(input, output);'
        info = SourceInfo(source)
        self.assertIn('Manual switch', info.hover_help(0, 1))
        self.assertIn('toggle', info.hover_help(0, 1))
        self.assertEqual(info.wire_at(0, source.index('input')), (1, 'Sbutton', 0, 'input'))
        self.assertEqual(info.wire_at(0, source.index('output')), (1, 'Sbutton', 1, 'output'))
        legacy = 'Sbutton input output closed'
        self.assertEqual(SourceInfo(legacy).wire_at(0, legacy.index('output')), (1, 'Sbutton', 1, 'output'))
        self.assertEqual(SourceInfo('S1 = input;').hover_help(0, 1), '')

    def test_negative_supply_help_is_scoped_to_battery_values(self):
        for source, line in (('battery Supply { Vneg = 5; wire = neg; }', 0),
                             ('(battery Supply)\nVneg = 5\n(end battery)', 1)):
            column = source.splitlines()[line].index('Vneg') + 1
            self.assertIn('-5 V', SourceInfo(source).hover_help(line, column))
        self.assertEqual(SourceInfo('Vneg = input;').hover_help(0, 1), '')
        self.assertEqual(SourceInfo('(battery B)\nV=5\n(end battery)\nVneg = input;').hover_help(3, 1), '')

    def test_opamp_help_and_wire_hover_preserve_three_or_five_pin_order(self):
        for source in ('op1(input, inv, out);', 'op1(input, inv, out, pos, neg);'):
            info = SourceInfo(source)
            self.assertIn('pins 4 and 5', info.hover_help(0, 1))
            self.assertIn('does not simulate rail clipping', info.hover_help(0, 1))
            for index, name in enumerate(('input', 'inv', 'out', 'pos', 'neg')):
                if name in source:
                    self.assertEqual(info.wire_at(0, source.index(name)), (1, 'op1', index, name))

    def test_new_snippet_defaults_compile(self):
        for prefix in ('switch', 'battery-negative', 'opamp-powered'):
            with self.subTest(prefix=prefix):
                source = default_snippet(prefix)
                circuit = compile_circuit(source, render_only=True)
                self.assertTrue(circuit.components)

    def test_grammar_recognizes_manual_switch_and_negative_supply(self):
        path = Path(__file__).resolve().parents[2] / 'ide/vscode/syntaxes/OOC.json'
        grammar = json.loads(path.read_text(encoding='utf-8'))['repository']
        component = grammar['components']['patterns'][0]['match']
        self.assertEqual(re.search(component, 'Sbutton(open) between(a,b);')[0], 'Sbutton')
        properties = grammar['properties']['patterns']
        self.assertTrue(any(re.search(pattern.get('match', '(?!)'), 'Vneg = 5;')
                            for pattern in properties))


if __name__ == '__main__':
    unittest.main()
