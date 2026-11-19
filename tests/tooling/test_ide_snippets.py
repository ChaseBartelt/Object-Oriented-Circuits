"""The default text inserted by digital editor snippets must compile."""
# Expand snippet defaults as the editor would, then compile the inserted source.
# Escaped interpolation and generated names must survive the snippet layer literally.

import json
from pathlib import Path
import re
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation


SNIPPETS = json.loads((Path(__file__).resolve().parents[2] /
                      'ide/vscode/snippets/ooc.code-snippets').read_text(encoding='utf-8'))


def default_snippet(prefix):
    """Expand the simple tabstops/defaults/choices used by these snippets."""
    body = next(item['body'] for item in SNIPPETS.values() if item['prefix'] == prefix)
    source = '\n'.join(body) if isinstance(body, list) else body
    source = source.replace(r'\$', '\x01').replace(r'\}', '}')
    values = {}

    def placeholder(match):
        value = match[2] if match[2] is not None else match[3].split(',')[0]
        values[match[1]] = value
        return value

    source = re.sub(r'\$\{(\d+)(?::([^}]*?)|\|([^}]*?)\|)\}', placeholder, source)
    source = re.sub(r'\$(\d+)', lambda match: values.get(match[1], ''), source)
    return source.replace('\x01', '$')


class DigitalEditorSnippetTest(unittest.TestCase):
    def test_complete_digital_examples_compile_and_generated_names_survive_snippet_escaping(self):
        for prefix in ('ooc-chip', 'ooc-main', 'ooc-bank'):
            with self.subTest(prefix=prefix):
                source = default_snippet(prefix)
                self.assertIn('module ', export_verilog(source))
        source = default_snippet('ooc-bank')
        self.assertIn('Follow(in${i}, out${i});', source)
        sim = DigitalSimulation(source)
        sim.set_input('in3', 1)
        self.assertEqual(sim.values['out3'], 1)
        self.assertEqual(sim.values['out0'], 0)

    def test_bus_wire_and_combinational_branch_snippets_work_inside_a_chip(self):
        source = 'chip Selector(choose, data, b, y) {\n'
        source += default_snippet('ooc-bus') + '\n'
        source += default_snippet('ooc-wire') + '\n'
        source += 'choose = input; b[8] = input; y[8] = output; a[8] = wire;\n'
        source += 'link = data; a = link;\n' + default_snippet('ooc-if') + '\n}'
        sim = DigitalSimulation(source)
        sim.set_input('data', 50)
        sim.set_input('b', 25)
        self.assertEqual(sim.values['y'], 25)
        sim.set_input('choose', 1)
        self.assertEqual(sim.values['y'], 50)

    def test_clock_and_every_snippets_toggle_at_the_declared_interval(self):
        clock = default_snippet('ooc-clock').replace('\n\t\n', '\n' + default_snippet('ooc-every') + '\n')
        source = 'chip Blink(clk, y) { clk = input; y = output; state;\n' + clock + '\ny = state; }'
        sim = DigitalSimulation(source)
        sim.advance(24_999_999)
        self.assertEqual(sim.values['y'], 0)
        sim.step()
        self.assertEqual(sim.values['y'], 1)


if __name__ == '__main__':
    unittest.main()
