"""Native switch declarations preserve state and identity through expansion."""
# Manual two-contact switches have state aliases and scoped names distinct from the
# existing four-terminal voltage-controlled switch; both forms must keep their identities.

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError


class SchematicSwitchSyntaxTest(unittest.TestCase):
    def test_optional_state_aliases_and_two_terminal_model(self):
        for raw, closed in (("", False), ("()", False), ("(open)", False),
                            ("(off)", False), ("(0)", False), ("(closed)", True),
                            ("(on)", True), ("(1)", True), ("(CLOSED)", True)):
            with self.subTest(raw=raw):
                circuit = compile_circuit(f"Sbutton{raw} between(signal, out);")
                part, = circuit.components
                self.assertEqual((part.name, part.kind, part.closed), ("Sbutton", "K", closed))
                self.assertEqual(part.terminal_nets, ("signal", "out"))
                self.assertEqual(part.value, .001 if closed else 1e12)
                self.assertEqual(component_labels(circuit)[part.name][-1], "Closed" if closed else "Open")
                self.assertNotIn("", circuit.nodes)

    def test_invalid_states_duplicates_and_terminals_keep_source_line(self):
        for row in ("S1(2) between(a,b);", "S1(10k) between(a,b);",
                    "S1(open,closed) between(a,b);", "S1(closed) between(a,b,c);"):
            with self.subTest(row=row), self.assertRaises(CircuitError) as error:
                compile_circuit("# contact\n" + row)
            self.assertEqual(error.exception.line, 2)
        with self.assertRaisesRegex(CircuitError, "Duplicate component"):
            compile_circuit("S1(open) between(a,b);\ns1(closed) between(c,d);")

    def test_named_series_button_and_scoped_component_arguments(self):
        source = """
        series button { S1(closed); R2(10k); }
        circuit contact(a, b, part) { part between(a, b); }
        main {
            button(pos, first);
            button(pos, second);
            contact(first, out, Scontact(open));
        }
        """
        circuit = compile_circuit(source)
        switches = [part for part in circuit.components if part.kind == "K"]
        self.assertEqual([p.name for p in switches], ["Scontact_contact_1", "S1_button_1", "S1_button_2"])
        self.assertEqual([p.closed for p in switches], [False, True, True])
        self.assertEqual(switches[0].terminal_nets, ("first", "out"))
        self.assertEqual(switches[1].p, "pos")
        self.assertEqual(switches[2].p, "pos")
        self.assertNotEqual(switches[1].n, switches[2].n)

    def test_inline_group_button_and_resistor_share_the_generated_junction(self):
        circuit = compile_circuit("""
            main { series(input, out) { Sbutton(closed); R2(10k); } }
        """)
        button, resistor = circuit.components
        self.assertEqual((button.name, button.kind, button.closed), ("Sbutton", "K", True))
        self.assertEqual((button.p, resistor.n, resistor.value), ("input", "out", 10000))
        self.assertEqual(button.n, resistor.p)

    def test_generated_switches_and_state_parameters(self):
        source = """
        battery Supply { V = 2; wire = pos; }
        series button(state) { state = S1; R2 = 10k; }
        main {
            button(closed);
            for (int i = 0; i < 2; i++) { S${i}(on) between(a${i}, b${i}); }
        }
        """
        circuit = compile_circuit(source, render_only=True)
        switches = [part for part in circuit.components if part.kind == "K"]
        self.assertEqual({p.name for p in switches}, {"S1_button_1", "S0", "S1"})
        self.assertTrue(all(p.closed for p in switches))

    def test_voltage_controlled_switch_stays_four_terminal(self):
        circuit = compile_circuit("switch1(control,0,a,b,1,1,1G);\nS1(closed) between(b,c);")
        controlled, manual = circuit.components
        self.assertEqual(controlled.kind, "S")
        self.assertEqual(controlled.terminal_nets, ("control", "0", "a", "b"))
        self.assertEqual(manual.kind, "K")
        self.assertEqual(manual.terminal_nets, ("b", "c"))


if __name__ == "__main__":
    unittest.main()
