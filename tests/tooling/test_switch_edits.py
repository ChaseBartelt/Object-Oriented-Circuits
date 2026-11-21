"""Schematic switch toggles edit the selected source statement atomically."""
# Toggling must change one state span rather than rewrite the component's connections.
# Grouped and included definitions retain shared scope, surrounding whitespace, and comments.

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.tooling.source_edits import edit_switch, edit_value


class SwitchEditsTest(unittest.TestCase):
    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'switch.ckt'
        self.reader = lambda path: Path(path).read_text(encoding='utf-8')

    def test_toggles_explicit_and_default_states_without_touching_other_statements(self):
        for declaration in ('S1(open)', 'S1(off)', 'S1(0)', 'S1'):
            source = f'{declaration} between(v,out); # keep comment\nR1(1k) between(out,0);'
            circuit = compile_circuit(source, path=self.path, render_only=True)
            plan = edit_switch(circuit, 'S1', True, self.path, source, self.reader)
            self.assertEqual(plan.after, source.replace(declaration, 'S1(closed)', 1))
            changed = compile_circuit(plan.after, path=self.path, render_only=True)
            self.assertTrue(next(part.closed for part in changed.components if part.name == 'S1'))
            self.assertEqual([(part.name, part.terminal_nets) for part in circuit.components],
                             [(part.name, part.terminal_nets) for part in changed.components])
            reopened = edit_switch(changed, 'S1', False, self.path, plan.after, self.reader)
            self.assertIn('S1(open) between(v,out); # keep comment', reopened.after)
            self.assertFalse(self.path.exists())

    def test_group_and_included_definition_changes_preserve_shared_scope(self):
        grouped = 'series button(a,b) {\n  S1 = open; # shared button\n  R1 = 1k;\n}\nmain { button(v,a); }'
        circuit = compile_circuit(grouped, path=self.path, render_only=True)
        part = next(part for part in circuit.components if part.kind == 'K')
        plan = edit_switch(circuit, part.name, True, self.path, grouped, self.reader)
        self.assertEqual(plan.after, grouped.replace('S1 = open', 'S1 = closed'))

        definition = 'circuit button(a,b) {\n  S1(open) between(a,b); # shared button\n}\n'
        library = self.path.with_name('button.ckt')
        library.write_text(definition, encoding='utf-8')
        source = 'include("button.ckt");\nmain { button(v,a); button(v,b); }'
        circuit = compile_circuit(source, path=self.path, render_only=True)
        part = next(part for part in circuit.components if part.kind == 'K')
        plan = edit_switch(circuit, part.name, True, self.path, source, self.reader)
        self.assertEqual(plan.path, library.resolve())
        self.assertEqual(plan.after, definition.replace('S1(open)', 'S1(closed)'))
        changed = compile_circuit(source, path=self.path, render_only=True,
                                  read_source=lambda path: plan.after if path == library else self.reader(path))
        self.assertEqual([part.closed for part in changed.components if part.kind == 'K'], [True, True])
        self.assertEqual(self.reader(library), definition)

    def test_rejects_non_switch_and_non_boolean_state(self):
        source = 'R1(1k) between(v,0);\nS1(open) between(v,out);'
        circuit = compile_circuit(source, path=self.path, render_only=True)
        for name, state in (('R1', True), ('S1', 'closed')):
            with self.subTest(name=name, state=state), self.assertRaises(ValueError):
                edit_switch(circuit, name, state, self.path, source, self.reader)

    def test_edits_compact_group_entries_without_touching_neighbors_or_wires(self):
        source = ('/* Start with a commented-out button.\n'
                  '   Sbutton(off); */ main { series(v,out) { Sbutton ( closed ); '
                  'R2(10k /* keep note */); } } // keep comment\n')
        circuit = compile_circuit(source, path=self.path, render_only=True)
        opened = edit_switch(circuit, 'Sbutton', False, self.path, source, self.reader)
        self.assertEqual(opened.after, source.replace('( closed )', '( open )'))
        cleared = edit_value(circuit, 'R2', '', self.path, source, self.reader)
        self.assertEqual(cleared.after, source.replace('R2(10k ', 'R2( '))
        original_nets = [(part.name, part.terminal_nets) for part in circuit.components]
        for plan in (opened, cleared):
            edited = compile_circuit(plan.after, path=self.path, render_only=True)
            self.assertEqual([(part.name, part.terminal_nets) for part in edited.components], original_nets)
        self.assertFalse(self.path.exists())

    def test_legacy_switch_rows_can_be_toggled(self):
        for source in ('S1 input output closed # keep legacy comment\nR1 output 0 1k\n',
                       '(series) S1(closed) (end series) // keep comment\n'):
            with self.subTest(source=source):
                circuit = compile_circuit(source, path=self.path, render_only=True)
                plan = edit_switch(circuit, 'S1', False, self.path, source, self.reader)
                self.assertEqual(plan.after, source.replace('closed', 'open'))


if __name__ == '__main__':
    unittest.main()
