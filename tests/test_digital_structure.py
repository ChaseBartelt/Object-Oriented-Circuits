"""Static assembly creates scoped behavior without changing electrical syntax."""

import unittest

from circuitlab.exporters.verilog import compile_chips, export_verilog
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.digital_structure import assemble_digital
from circuitlab.language.syntax import has_chip
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


INVERT = "chip Invert(a,y) { a=input; y=output; y=!a; }"


class DigitalStructureTest(unittest.TestCase):
    def test_main_generates_three_calls_and_wires_with_original_line_numbers(self):
        source = INVERT + """
main {
    a=input;
    for (int i=0; i<3; i++) {
        y${i}=output;
        Invert(a,y${i});
    }
}
"""
        instances = []
        top, pins, _, _ = assemble_digital(source, instances=instances)
        self.assertEqual(top, "main")
        self.assertEqual(pins, ("a", "y0", "y1", "y2"))
        self.assertEqual(len(instances), 3)
        self.assertEqual({i.line for i in instances}, {6})
        sim = DigitalSimulation(source)
        self.assertEqual([sim.values[n] for n in sim.outputs], [1, 1, 1])
        sim.set_input("a", 1)
        self.assertEqual([sim.values[n] for n in sim.outputs], [0, 0, 0])
        self.assertEqual(export_verilog(source).count("assign y"), 3)

    def test_static_if_while_do_select_actual_hardware(self):
        source = INVERT + """ main Generated {
            a=input;
            int i=0;
            while (i<2) {
                y${i}=output; Invert(a,y${i}); i++;
            }
            do { y${i}=output; Invert(a,y${i}); i++; } while (i<3);
            if (i==3) { selected=output; Invert(a,selected); }
            else { Missing(a,undefined); }
        }"""
        sim = DigitalSimulation(source)
        self.assertEqual(sim.outputs, ("y0", "y1", "y2", "selected"))
        self.assertTrue(all(sim.values[n] == 1 for n in sim.outputs))
        self.assertNotIn("Missing", export_verilog(source))

    def test_nested_chips_have_scoped_wires_and_metadata(self):
        source = INVERT + """
        chip Buffer(a,y) {
            a=input; y=output; mid=wire;
            Invert(a,mid); Invert(mid,y);
        }
        main { a,b=input; first,second=output; Buffer(a,first); Buffer(b,second); }
        """
        session = CircuitSession(source=source)
        session.compile()
        self.assertFalse(session.issues)
        self.assertEqual([x.parent for x in session.drawing.instances], [None, 0, 0, None, 3, 3])
        sim = DigitalSimulation(source)
        sim.set_input("a", 1)
        self.assertEqual((sim.values["first"], sim.values["second"]), (1, 0))
        self.assertEqual(len([n for n in sim.signals if n.endswith("_mid")]), 2)

    def test_keyword_like_state_names_and_spaced_units_remain_valid(self):
        source = """chip T(clk,y) {
            clk=input; y=output; clock; every; const;
            const Hz[4]=3;
            phase[16];
            clock(clk, 1kHz) {
                phase += 10 Hz;
                every(10 ms) { clock=!clock; every=!every; const=!const; }
            }
            y=clock ^ every ^ const ^ phase[15] ^ Hz[0];
        }
        main Timed { clk=input; y=output; T(clk,y); }
        """
        sim = DigitalSimulation(source)
        sim.advance(10)
        self.assertEqual(sim.values["T_1_clock"], 1)
        self.assertEqual(sim.values["T_1_every"], 1)
        self.assertEqual(sim.values["T_1_const"], 1)
        self.assertEqual(sim.values["T_1_phase"], 6550)

    def test_local_names_cannot_capture_main_signals_or_drive_formal_inputs(self):
        bodies = (
            "y=secret;",
            "held; clock(secret,1Hz){held=!held;} y=held;",
            "a=1; y=a;",
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CircuitError):
                compile_chips("chip Bad(a,y){a=input;y=output;" + body + "} "
                              "main { secret=input; n=wire; y=output; Bad(n,y); }")

    def test_nested_chip_cannot_drive_parents_input_even_when_bound_to_wire(self):
        source = INVERT + """ chip Bad(a,y) {
            a=input; y=output; Invert(a,a); y=a;
        }
        main { x=input; n=wire; y=output; Invert(x,n); Bad(n,y); }
        """
        with self.assertRaises(CircuitError):
            compile_chips(source)

    def test_internal_names_do_not_collide_with_explicit_root_names(self):
        source = """chip Counter(clk,y){clk=input;y=output;c[8];clock(clk,1Hz){c+=1;}y=c[0];}
        main { clk, Counter_1_c=input; y=output; Counter(clk,y); }
        """
        sim = DigitalSimulation(source)
        self.assertIn("ooc_Counter_1_c", sim.signals)
        sim.step()
        self.assertEqual(sim.values["y"], 1)
        self.assertEqual(sim.values["Counter_1_c"], 0)

    def test_digital_ports_are_detected_with_only_vector_declarations(self):
        source = "chip Copy(a,y){a[8]=input;y[8]=output;y=a;} main{a[8]=input;y[8]=output;Copy(a,y);}"
        self.assertTrue(has_chip(source))
        sim = DigitalSimulation(source)
        sim.set_input("a", 255)
        self.assertEqual(sim.values["y"], 255)

    def test_rejects_bad_connections_and_multiple_drivers(self):
        for assembly in (
            "a=input; y=output; Invert(a);",
            "a=input; y=output; Invert(missing,y);",
            "a[8]=input; y=output; Invert(a,y);",
            "a=input; y=output; Invert(a,a);",
            "a=input; y=output; Invert(a,y); Invert(a,y);",
            "a=input; y=output; Unknown(a,y);",
        ):
            with self.subTest(assembly=assembly), self.assertRaises(CircuitError):
                compile_chips(INVERT + "main {" + assembly + "}")

    def test_rejects_recursion_and_cross_instance_combinational_feedback(self):
        recursive = "chip Recursive(a,y){a=input;y=output;Recursive(a,y);} main{a=input;y=output;Recursive(a,y);}"
        cycle = INVERT + "main { y=output; n=wire; Invert(y,n); Invert(n,y); }"
        for source in (recursive, cycle):
            with self.assertRaises(CircuitError):
                compile_chips(source)

    def test_main_runtime_behavior_and_nonterminating_generation_are_rejected(self):
        bodies = ("a=input; y=output; if(a){Invert(a,y);}",
                  "a=input;y=output;y=a;", "a=input;y=output;clock(a,1Hz){y=1;}",
                  "a=input;y=output;while(true){}")
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CircuitError):
                compile_chips(INVERT + "main {" + body + "}")

    def test_existing_electrical_main_and_chip_meanings_are_unchanged(self):
        source = """chip Load(p,n){R1(1k) between(p,n);}
        V1(5) between(vcc,0);
        main {for(int i=0;i<3;i++){Load(vcc,0);}}"""
        self.assertFalse(has_chip(source))
        circuit = compile_circuit(source)
        self.assertEqual(len(circuit.instances), 3)
        self.assertEqual(len(circuit.components), 4)
        self.assertTrue(all(p.kind in {"V", "R"} for p in circuit.components))

    def test_existing_chip_only_source_has_no_assembly(self):
        self.assertIsNone(assemble_digital(INVERT))
        self.assertEqual(compile_chips(INVERT)[0][0], "Invert")

    def test_main_alias_forms_and_top_name_collision(self):
        for header in ("main", "main()", "main Demo", "main Demo()"):
            self.assertEqual(compile_chips(INVERT + header + " {a=input;y=output;Invert(a,y);}")[0][0],
                             "Demo" if "Demo" in header else "main")
        with self.assertRaises(CircuitError):
            compile_chips(INVERT + "main Invert {a=input;y=output;Invert(a,y);}")
