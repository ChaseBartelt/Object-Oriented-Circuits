"""Width-bearing expression declarations infer wires without redefining state."""

import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


class WireInitializerTest(unittest.TestCase):
    def test_clockless_board_bank_expressions_are_visible_live_wires(self):
        source = """chip HYBRID01(SW[7:0],LEDR[7:0]) {
            SW[7:0]=input;LEDR[7:0]=output;
            requests[8]=SW[7:0]; inverted[8]=requests ^ 255;
            LEDR[7:0]=inverted;
        }"""
        self.assertNotRegex(export_verilog(source), r"\b(?:reg|always)\b")
        for profile in ("ooc", "de10-lite"):
            with self.subTest(profile=profile):
                sim = DigitalSimulation(source, profile)
                self.assertEqual(sim.clocks, ())
                self.assertIsNone(sim.frequency)
                self.assertEqual(sim.signal_kinds["requests"], "wire")
                self.assertEqual(sim.signal_kinds["inverted"], "wire")
                for value in range(256):
                    for bit in range(8):
                        sim.set_input(f"SW{bit}", (value >> bit) & 1)
                    self.assertEqual(sim.values["requests"], value)
                    self.assertEqual(sim.values["inverted"], value ^ 255)
                    self.assertEqual(sum(sim.values[f"LEDR{bit}"] << bit for bit in range(8)), value ^ 255)
                self.assertEqual(sim.cycles, 0)

    def test_combinational_arithmetic_ternary_and_forward_reference(self):
        sim = DigitalSimulation("""chip Calculate(a,b,s,y) {
            a[8],b[8]=input;s=input;y[8]=output;
            selected[8]=s ? mixed : a;
            mixed[8]=a ^ b;
            result[8]=selected + 1;
            y=result;
        }""")
        for a, b, select in ((0, 0, 0), (5, 12, 1), (255, 1, 0), (255, 128, 1)):
            sim.set_input("a", a)
            sim.set_input("b", b)
            sim.set_input("s", select)
            self.assertEqual(sim.values["y"], ((a ^ b if select else a) + 1) & 255)
        for name in ("selected", "mixed", "result"):
            self.assertEqual(sim.signal_kinds[name], "wire")
        self.assertNotIn("register", sim.signal_kinds.values())

    def test_literal_initializers_and_bare_vectors_remain_stored_state(self):
        source = """chip Stored(clk,a,y) {
            clk=input;a[8]=input;y[8]=output;
            phase[32]=0; count[8]=1_0; spare[8]; const STEP[8]=1;
            next[8]=a + count;
            clock(clk,10Hz){phase+=STEP;count=next;spare=a;}
            y=count;
        }"""
        sim = DigitalSimulation(source)
        self.assertEqual(sim.values["count"], 10)
        self.assertEqual(sim.values["spare"], 0)
        for name in ("phase", "count", "spare"):
            self.assertEqual(sim.signal_kinds[name], "register")
        self.assertEqual(sim.signal_kinds["next"], "wire")
        self.assertEqual(sim.signal_kinds["STEP"], "constant")
        sim.set_input("a", 7)
        self.assertEqual(sim.values["y"], 10)
        sim.step()
        self.assertEqual((sim.values["phase"], sim.values["count"], sim.values["spare"]), (1, 17, 7))

    def test_existing_vector_index_assignments_do_not_become_declarations(self):
        sim = DigitalSimulation("""chip Forward(a,y) {
            a[4]=input;y[4]=output;
            selected[4]=wire;
            selected[3]=a[3];selected[2:0]=a[2:0];
            y[3]=selected[3];y[2:0]=selected[2:0];
        }""")
        for value in range(16):
            sim.set_input("a", value)
            self.assertEqual(sim.values["y"], value)
        self.assertEqual(sim.signals["selected"], 4)
        self.assertEqual(sim.signals["y"], 4)
        literal = DigitalSimulation("chip SetTop(a,y){a[4]=input;y[4]=output;y[3]=1;y[2:0]=a[2:0];}")
        literal.set_input("a", 5)
        self.assertEqual(literal.values["y"], 13)

    def test_constant_expression_and_single_bit_vector_infer_wires(self):
        sim = DigitalSimulation("""chip Calculate(a,y,z) {
            a=input;y[4]=output;z=output;
            value[4]=2 + 4; flag[1]=!a;
            y=value;z=flag[0];
        }""")
        self.assertEqual(sim.values["y"], 6)
        self.assertEqual(sim.signal_kinds["value"], "wire")
        self.assertEqual(sim.signal_kinds["flag"], "wire")
        self.assertEqual(sim.values["z"], 1)
        sim.set_input("a", 1)
        self.assertEqual(sim.values["z"], 0)

    def test_nested_assembly_keeps_inferred_wire_bindings_and_independent_values(self):
        source = """chip Flip(a,y) {
            a[4]=input;y[4]=output; inverted[4]=a ^ 15;y=inverted;
        }
        chip Twice(a,y) {
            a[4]=input;y[4]=output;mid[4]=wire;
            forward[4]=a;
            Flip(forward,mid);Flip(mid,y);
        }
        main {a[4],b[4]=input;x[4],y[4]=output;Twice(a,x);Twice(b,y);}
        """
        session = CircuitSession(source=source)
        session.compile()
        self.assertFalse(session.issues)
        self.assertEqual([instance.parent for instance in session.drawing.instances], [None, 0, 0, None, 3, 3])
        sim = DigitalSimulation(source)
        sim.set_input("a", 10)
        sim.set_input("b", 3)
        self.assertEqual((sim.values["x"], sim.values["y"]), (10, 3))
        for instance in session.drawing.instances:
            names = dict(instance.signal_bindings)
            local = "forward" if instance.name == "Twice" else "inverted"
            self.assertEqual(sim.signal_kinds[names[local]], "wire")

    def test_self_feedback_conflicts_bad_widths_and_out_of_range_indices_are_rejected(self):
        bodies = (
            "next[4]=next;y=next;",
            "next[4]=other;other[4]=next;y=next;",
            "next[4]=a;next[4]=wire;y=next;",
            "next[0]=a;y=next;",
            "next[4097]=a;y=next;",
            "y[4]=a[0];y[3:0]=a;",
            "next[4]=a[99:96];y=next;",
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CircuitError):
                export_verilog("chip Bad(a,y){a[4]=input;y[4]=output;" + body + "}")

    def test_undeclared_value_inside_clock_is_not_an_implicit_declaration(self):
        with self.assertRaises(CircuitError):
            export_verilog("chip Bad(clk,a,y){clk,a=input;y=output;clock(clk){next[1]=a;}y=a;}")

    def test_inferred_expression_diagnostics_keep_the_original_source_line(self):
        source = "chip Bad(a,y){\n a[4]=input;y[4]=output;\n next[4]=missing;\n y=next;\n}"
        with self.assertRaises(CircuitError) as error:
            export_verilog(source)
        self.assertEqual(error.exception.line, 3)


if __name__ == "__main__":
    unittest.main()
