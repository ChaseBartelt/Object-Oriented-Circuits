"""Switch dispatch lowers to ordinary digital behavior without losing case order."""

import unittest

from circuitlab.language.digital_switch import expand_switches
from circuitlab.language.syntax import source_rows
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError


def lower(source):
    rows = list(source_rows("chip Wrapper(dummy) { " + source + "\n}"))
    return expand_switches([(text, line) for text, line, _ in rows[1:-1]])


def simulation(body, declarations="sel[3]=input; y[8]=output; state[8];", *, clocked=True):
    body = "switch (sel) { " + body + " }"
    if clocked:
        source = f"chip Test(clk,sel,y) {{ clk=input; {declarations} clock(clk,1kHz) {{ {body} }} y=state; }}"
    else:
        source = f"chip Test(sel,y) {{ {declarations} {body} }}"
    return DigitalSimulation(source)


class DigitalSwitchTest(unittest.TestCase):
    def test_clocked_cases_default_and_stacked_labels(self):
        sim = simulation("case 0: case 1: state=12; break; case 2: state=34; break; default: state=56;")
        for selector, value in ((0, 12), (1, 12), (2, 34), (7, 56)):
            sim.set_input("sel", selector)
            sim.step()
            self.assertEqual(sim.values["y"], value)

    def test_fallthrough_retains_source_order_and_default_position(self):
        sim = simulation("case 0: state=1; default: state=2; case 1: state=3; break; case 2: state=4;")
        for selector, value in ((0, 3), (7, 3), (1, 3), (2, 4)):
            sim.set_input("sel", selector)
            sim.step()
            self.assertEqual(sim.values["y"], value)

    def test_without_default_clocked_state_holds(self):
        sim = simulation("case 0: state=11; break; case 1: state=22; break;")
        for selector, value in ((0, 11), (7, 11), (1, 22), (6, 22)):
            sim.set_input("sel", selector)
            sim.step()
            self.assertEqual(sim.values["y"], value)

    def test_nested_switch_breaks_exit_only_the_inner_switch(self):
        rows = lower("""switch (sel) {
            case 0: switch (state) { case 0: state=5; break; default: state=6; }
                    state=state+1; break;
            default: state=9;
        }""")
        body = "\n".join(row for row, _ in rows)
        sim = DigitalSimulation(f"chip Test(clk,sel,y) {{ clk,sel=input; y[8]=output; state[8]; clock(clk,1kHz) {{ {body} }} y=state; }}")
        sim.step()
        self.assertEqual(sim.values["y"], 1)  # Last NBA uses the old state, not the inner switch's scheduled 5.
        sim.step()
        self.assertEqual(sim.values["y"], 2)
        sim.set_input("sel", 1)
        sim.step()
        self.assertEqual(sim.values["y"], 9)

    def test_combinational_switch_requires_total_assignment(self):
        declarations = "sel[3]=input; y[8]=output;"
        sim = simulation("case 0: case 1: y=8; break; default: y=16;", declarations, clocked=False)
        for selector, value in ((0, 8), (1, 8), (5, 16)):
            sim.set_input("sel", selector)
            self.assertEqual(sim.values["y"], value)
        with self.assertRaises(CircuitError):
            simulation("case 0: y=1; break;", declarations, clocked=False)

    def test_literal_and_declared_constant_labels_keep_source_lines(self):
        rows = lower("const IDLE[3]=1;\nswitch (sel) {\ncase IDLE: state=4; break;\ndefault: state=8;\n}")
        self.assertIn(("if ((sel) == 1) {", 3), rows)
        self.assertIn(("state=4", 3), rows)
        self.assertIn(("else {", 4), rows)
        self.assertNotIn("break", [text for text, _ in rows])

    def test_bad_labels_and_conditional_break_have_explicit_diagnostics(self):
        cases = (
            ("case unknown: state=1;", "case label"),
            ("case 0: break; case 0: break;", "Duplicate case"),
            ("default: break; default: break;", "one default"),
            ("case 0: if(sel) { break; }", "Conditional break"),
            ("state=1; case 0: break;", "Start the switch"),
        )
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(CircuitError, message):
                lower("switch(sel) { " + body + " }")
        with self.assertRaisesRegex(CircuitError, "Duplicate case"):
            lower("const SAME[3]=0; switch(sel) { case 0: break; case SAME: break; }")

    def test_case_ternary_statement_is_not_split_on_its_colon(self):
        rows = lower("switch(sel) { case 0: state=flag ? 1 : 2; break; default: state=3; }")
        self.assertIn(("state=flag ? 1 : 2", 1), rows)

    def test_assembled_switch_instances_keep_independent_state(self):
        sim = DigitalSimulation('''
            chip Count(clk,sel,y) { clk,sel=input; y[3]=output; state[3];
                const INC=1;
                clock(clk) { switch(sel) { case INC: state+=1; break; default: state=0; } }
                y=state;
            }
            main {clk,a,b=input; x[3],y[3]=output; Count(clk,a,x); Count(clk,b,y);}
        ''')
        sim.set_input("a", 1)
        sim.advance(3)
        self.assertEqual((sim.values["x"], sim.values["y"]), (3, 0))
        sim.set_input("a", 0)
        sim.set_input("b", 1)
        sim.step()
        self.assertEqual((sim.values["x"], sim.values["y"]), (0, 1))

    def test_static_loops_inside_cases_and_case_labels_inside_loops(self):
        sim = DigitalSimulation('''chip Select(sel,a,y) { sel=input; a[4]=input; y[4]=output;
            switch(sel) {case 0: for(i=0;i<4;i++){y[i]=a[i];} break; default:y=0;}
        }''')
        sim.set_input("a", 13)
        self.assertEqual(sim.values["y"], 13)
        sim.set_input("sel", 1)
        self.assertEqual(sim.values["y"], 0)
        sim = DigitalSimulation('''chip Select(sel,y) { sel[2]=input; y[4]=output;
            for(i=0;i<4;i++){switch(sel){case i:y[i]=1;break;default:y[i]=0;}}
        }''')
        for selector in range(4):
            sim.set_input("sel", selector)
            self.assertEqual(sim.values["y"], 1 << selector)


if __name__ == "__main__":
    unittest.main()
