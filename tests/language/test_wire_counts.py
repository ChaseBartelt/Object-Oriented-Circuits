"""Main wire counters allocate concrete nets before graph and layout construction."""

from pathlib import Path
import unittest

from circuitlab.exporters.spice import export_spice
from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout
from circuitlab.math.analysis import analyze
from circuitlab.math.digital import DigitalSimulation
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.source_info import SourceInfo
from circuitlab.tooling.trace import wire_trace


def terminals(circuit, name):
    return next(part.terminal_nets for part in circuit.components if part.name == name)


class WireCountTest(unittest.TestCase):
    def test_named_suffixes_read_zero_and_advance_the_declared_counter(self):
        circuit = compile_circuit("""main {
            wi = count;
            aux = count;
            R0(1k) between(wi^, 0);
            R1(1k) between(input, wi*);
            R2(1k) between(wi^, wi*);
            R3(1k) between(wi^, aux*);
            R4(1k) between(aux^, 0);
        }""")
        self.assertEqual([part.terminal_nets for part in circuit.components], [
            ("wi0", "0"), ("input", "wi1"), ("wi1", "wi2"), ("wi2", "aux1"), ("aux1", "0"),
        ])

    def test_inserting_a_stage_preserves_previous_connections_and_advances_later_wires(self):
        before = """main {
            i = count;
            R1(1k) between(input, w*i);
            op1(w^i, 0, w*i);
        }"""
        after = before.replace("op1(w^i", "R2(1k) between(w^i, w*i);\n            op1(w^i")
        original, inserted = (compile_circuit(text) for text in (before, after))
        self.assertEqual(terminals(original,"R1"),("input","w1"))
        self.assertEqual(terminals(inserted,"R1"),("input","w1"))
        self.assertEqual(terminals(inserted,"R2"),("w1","w2"))
        for circuit, expected in ((original,("w1","0","w2")),(inserted,("w2","0","w3"))):
            amp = next(part for part in circuit.components if part.kind == "O")
            self.assertEqual((amp.in_plus,amp.in_minus,amp.p),expected)
        self.assertFalse(any(char in node for char in "*^$" for node in inserted.nodes))

    def test_current_starts_at_zero_and_each_advance_consumes_one_number_left_to_right(self):
        circuit = compile_circuit("""main {
            i=count;
            Rzero(1k) between(w^i,0);
            R1(1k) between(w*i,w*i);
            R2(1k) between(w^i,0);
            R3(1k) between(w^i,0);
            R4(1k) between(w*i,w*i);
        }""")
        self.assertEqual(terminals(circuit,"Rzero"),("w0","0"))
        self.assertEqual(terminals(circuit,"R1"),("w1","w2"))
        self.assertEqual(terminals(circuit,"R2"),("w2","0"))
        self.assertEqual(terminals(circuit,"R3"),("w2","0"))
        self.assertEqual(terminals(circuit,"R4"),("w3","w4"))

    def test_counters_are_independent_and_counter_values_are_not_per_prefix(self):
        circuit = compile_circuit("""main {
            i=count; j=count;
            R1(1k) between(input,a*i);
            R2(1k) between(a^i,b*j);
            R3(1k) between(b^j,c*i);
            R4(1k) between(c^i,b*j);
            R5(1k) between(b^j,0);
        }""")
        self.assertEqual([terminals(circuit,name) for name in ("R1","R2","R3","R4","R5")],
                         [("input","a1"),("a1","b1"),("b1","c2"),("c2","b2"),("b2","0")])

    def test_user_shorthand_aliases_and_prefixes_reuse_the_right_counter(self):
        circuit = compile_circuit("""main {
            i=count; j=count;
            R1(1k) between(input,w*i);
            R2(1k) between(w$i,w*i);
            op1(w^,0,w*i);
            R3(1k) between(aux*j,0);
            R4(1k) between(w^,aux^);
            R5(1k) between(w^i,aux$j);
        }""")
        self.assertEqual(terminals(circuit,"R2"),("w1","w2"))
        amp = next(part for part in circuit.components if part.kind == "O")
        self.assertEqual((amp.in_plus,amp.in_minus,amp.p),("w2","0","w3"))
        self.assertEqual(terminals(circuit,"R4"),("w3","aux1"))
        self.assertEqual(terminals(circuit,"R5"),("w3","aux1"))

    def test_explicit_current_read_selects_the_prefix_counter_without_incrementing_it(self):
        circuit = compile_circuit("""main {
            i=count; j=count;
            R1(1k) between(input,w*i);
            R2(1k) between(w*j,0);
            R3(1k) between(w*i,0);
            R4(1k) between(w^j,0);
            R5(1k) between(w^,0);
        }""")
        self.assertEqual(terminals(circuit,"R3"),("w2","0"))
        self.assertEqual(terminals(circuit,"R4"),("w1","0"))
        self.assertEqual(terminals(circuit,"R5"),("w1","0"))

    def test_only_executed_loop_and_branch_occurrences_advance_the_counter(self):
        circuit = compile_circuit("""main {
            i=count;
            if(false) {Rskip(1k) between(w*missing,0);}
            for(int n=0;n<3;n++) {
                if(n==1) {continue;}
                R${n}(1k) between(w^i,w*i);
            }
            Rfinal(1k) between(w^i,0);
        }""")
        self.assertEqual([part.name for part in circuit.components],["R0","R2","Rfinal"])
        self.assertEqual([part.terminal_nets for part in circuit.components],
                         [("w0","w1"),("w1","w2"),("w2","0")])

    def test_readonly_expressions_can_observe_counter_values(self):
        circuit = compile_circuit("""main {
            i=count;
            Rstart(1k) between(input,w*i);
            let seen=i;
            if(i==1 && seen==1) {Rcurrent${i}(1k) between(w^i,0);}
            Rnext(1k) between(w*i,0);
        }""")
        self.assertEqual(terminals(circuit,"Rcurrent1"),("w1","0"))
        self.assertEqual(terminals(circuit,"Rnext"),("w2","0"))

    def test_static_interpolation_and_wire_markers_share_textual_execution_order(self):
        circuit = compile_circuit("""main {
            i=count;
            Rseed(1k) between(input,w*i);
            R${i}(1k) between(w^i,w*i);
            R${i}(1k) between(w^i,w*i);
        }""")
        self.assertEqual([part.name for part in circuit.components],["Rseed","R1","R2"])
        self.assertEqual(terminals(circuit,"R1"),("w1","w2"))
        self.assertEqual(terminals(circuit,"R2"),("w2","w3"))

    def test_generated_chain_solves_exports_and_restores_the_same_schematic_nets(self):
        source = """V1(9V) between(input,0);
        main {
            i=count;
            R1(1k) between(input,w*i);
            R2(1k) between(w^i,w*i);
            R3(1k) between(w^i,0);
            measure=V w^i 0;
        }"""
        circuit = compile_circuit(source)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("w1")],6)
        self.assertAlmostEqual(network.voltage[network.nodes.index("w2")],3)
        self.assertEqual(circuit.probes[0].targets,("w2","0"))
        deck = export_spice(source)
        self.assertIn("R1 input w1 1000",deck)
        self.assertIn("R2 w1 w2 1000",deck)
        self.assertNotIn("count",deck)
        view = next(hood_levels(circuit,set()))[1]
        drawing = fast_layout(view,component_labels(view))
        self.assertTrue(preserves_nets(view,drawing))
        restored = restore_layout(pack_layout(drawing),view,())
        self.assertIsNotNone(restored)
        self.assertTrue(preserves_nets(view,restored))

    def test_method_arguments_resolve_in_main_before_binding_and_retain_hover_trace(self):
        source = """circuit Stage(a,b) {Rinside(1k) between(a,b);}
        main {
            i=count;
            Stage(input,w*i);
            Stage(w^i,w*i);
        }"""
        circuit = compile_circuit(source)
        self.assertEqual([part.terminal_nets for part in circuit.components],
                         [("input","w1"),("w1","w2")])
        line = source.splitlines()[3]
        location = SourceInfo(source).wire_at(3,line.index("w*i")+1)
        self.assertIsNotNone(location)
        parts,nets = wire_trace(circuit,location)
        self.assertTrue(parts)
        self.assertEqual(nets,{"w1"})

    def test_included_executable_mains_have_fresh_counters_and_private_wires(self):
        source = 'include("child.ckt");\nmain {i=count; R1(1k) between(input,w*i); R2(1k) between(w^i,w*i);}'
        child = 'main {i=count; R1(1k) between(input,w*i); R2(1k) between(w^i,0);}'
        circuit = compile_circuit(source,path=Path.cwd()/"main.ckt",read_source=lambda _:child)
        self.assertEqual(terminals(circuit,"R1"),("input","w1"))
        self.assertEqual(terminals(circuit,"R2"),("w1","w2"))
        self.assertEqual(terminals(circuit,"@file1.R1"),("@file1.input","@file1.w1"))
        self.assertEqual(terminals(circuit,"@file1.R2"),("@file1.w1","0"))

    def test_analog_arithmetic_and_digital_xor_do_not_consume_main_counts(self):
        circuit = compile_circuit("""main {
            i=count;
            R1(1k) between(input,w*i);
            analog {property w=2; let i=3; V(analog_out)=w*i;}
            digital {let a=1; out=a^i;}
            R2(1k) between(w^i,0);
        }""")
        self.assertEqual(terminals(circuit,"R2"),("w1","0"))
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("analog_out")],6)
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")],2)
        self.assertNotIn("a1",circuit.nodes)
        self.assertIn("module Logic",export_verilog("chip Logic(a,b,y){a,b=input;y=output;y=a^b;}"))

    def test_counter_errors_retain_the_original_statement_line(self):
        cases = (
            ("main {\nR1(1k) between(input,w*i);\n}",2),
            ("main {\ni=count;\nR1(1k) between(input,w^);\n}",3),
            ("main {\ni=count;\ni++;\nR1(1k) between(input,w*i);\n}",3),
            ("main {\ni=count;\ni=3;\nR1(1k) between(input,w*i);\n}",3),
            ("main {\ni=count;\ni=count;\nR1(1k) between(input,w*i);\n}",3),
            ("main {\nlet i=0;\nR1(1k) between(input,w*i);\n}",3),
        )
        for source,line in cases:
            with self.subTest(source=source),self.assertRaises(CircuitError) as caught:
                compile_circuit(source)
            self.assertEqual(caught.exception.line,line)

    def test_count_declarations_are_main_only_and_not_inherited_by_called_methods(self):
        sources = (
            "i=count;\nR1(1k) between(input,0);",
            "circuit Stage(a,b){i=count;R1(1k) between(a,b);}\nmain{Stage(input,0);}",
            "circuit Stage(a,b){R1(1k) between(a,w*i);}\nmain{i=count;Stage(input,0);}",
        )
        for source in sources:
            with self.subTest(source=source),self.assertRaises(CircuitError):
                compile_circuit(source)

    def test_included_counter_error_keeps_its_file_and_line(self):
        path = Path.cwd()/"main.ckt"
        source = 'include("child.ckt");\nRmain(1k) between(input,0);'
        child = "main {\ni=count;\nRbad(1k) between(input,w*missing);\n}"
        with self.assertRaises(CircuitError) as caught:
            compile_circuit(source,path=path,read_source=lambda _:child)
        self.assertEqual(caught.exception.path,path.with_name("child.ckt"))
        self.assertEqual(caught.exception.line,3)

    def test_flat_component_rows_and_source_shorthand_use_the_same_counter_rules(self):
        circuit = compile_circuit("""main {
            i=count;
            V1=9V : wire=w*i;
            R1 w^i w*i 1k;
            R2 w^i 0 2k;
        }""")
        self.assertEqual(terminals(circuit,"V1"),("w1","0"))
        self.assertEqual(terminals(circuit,"R1"),("w1","w2"))
        self.assertEqual(terminals(circuit,"R2"),("w2","0"))
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("w2")],6)

    def test_battery_and_transistor_connection_fields_advance_in_source_order(self):
        circuit = compile_circuit("""main {
            i=count;
            battery Vpower {V=5;wire=w*i;wire=0;}
            transistor Q1 {
                base=b*i;
                collector=c*i;
                emitter=e*i;
                type=npn;
            }
            R1(1k) between(e^,0);
        }""")
        supply = next(part for part in circuit.components if part.kind == "V")
        transistor = next(part for part in circuit.components if part.kind == "Q")
        self.assertEqual(supply.terminal_nets,("w1","0"))
        self.assertEqual((transistor.base,transistor.p,transistor.n),("b2","c3","e4"))
        self.assertEqual(terminals(circuit,"R1"),("e4","0"))

    def test_anonymous_structural_group_endpoints_consume_counts(self):
        circuit = compile_circuit("""V1(9V) between(input,0);
        main {
            i=count;
            series(input,w*i) {R1=1k;}
            R2(2k) between(w^,0);
        }""",render_only=True)
        self.assertEqual(terminals(circuit,"R1"),("input","w1"))
        self.assertEqual(terminals(circuit,"R2"),("w1","0"))

    def test_generated_prefixes_and_values_follow_textual_interpolation_order(self):
        circuit = compile_circuit("""main {
            i=count;
            for(int n=0;n<2;n++) {
                R${n}(${i+1}k) between(bank${n}*i,0);
            }
        }""")
        self.assertEqual([part.terminal_nets for part in circuit.components],[("bank01","0"),("bank12","0")])
        self.assertEqual([part.value for part in circuit.components],[1000,2000])

    def test_lexical_counter_shadowing_restores_the_visible_prefix_association(self):
        circuit = compile_circuit("""main {
            i=count;
            Router(1k) between(w*i,0);
            if(true) {
                i=count;
                Rinner1(1k) between(w*i,0);
                Rinner2(1k) between(w*i,0);
                RinnerNow(1k) between(w^,0);
            }
            Rafter(1k) between(w^,0);
            Rnext(1k) between(w*i,0);
        }""")
        self.assertEqual(terminals(circuit,"RinnerNow"),("w2","0"))
        self.assertEqual(terminals(circuit,"Rafter"),("w1","0"))
        self.assertEqual(terminals(circuit,"Rnext"),("w2","0"))
        with self.assertRaises(CircuitError):
            compile_circuit("main{i=count;if(true){j=count;R1(1k) between(x*j,0);}R2(1k) between(x^,0);}")

    def test_legacy_static_variable_named_count_keeps_ordinary_expression_semantics(self):
        circuit = compile_circuit("""main {
            int count=2;
            for(int n=0;n<count;n++) {R${n}(1k) between(input,node${n});}
            let twice=count*2;
            if(twice==4) {Rtail(1k) between(node1,0);}
            let i=0;
            i=count;
            Rassigned${i}(1k) between(input,0);
        }""")
        self.assertEqual([part.name for part in circuit.components],["R0","R1","Rtail","Rassigned2"])
        self.assertEqual(terminals(circuit,"R1"),("input","node1"))
        called = compile_circuit("""circuit Stage(a,b) {
            let count=2; let i=0; i=count;
            R${i}(1k) between(a,b);
        }
        main {Stage(input,0);}
        """)
        self.assertEqual(len(called.components),1)
        self.assertTrue(called.components[0].name.startswith("R2_"))
        self.assertEqual(called.components[0].terminal_nets,("input","0"))

    def test_behavioral_digital_main_counts_wires_before_chip_assembly(self):
        source = """chip Follow(a,y) {a=input;y=output;y=a;}
        main Counted {
            i=count; j=count;
            a=input; y=output;
            w*i=wire;
            z*j=wire;
            Follow(a,w^i);
            Follow(w^i,z^j);
            Follow(z^j,y);
        }"""
        sim = DigitalSimulation(source)
        self.assertIn("w1",sim.signals)
        self.assertIn("z1",sim.signals)
        for value in (0,1,0):
            sim.set_input("a",value)
            self.assertEqual(sim.values["y"],value)
        deck = export_verilog(source)
        self.assertIn("module Counted",deck)
        self.assertNotIn("w*i",deck)
        self.assertNotIn("count;",deck)


if __name__ == "__main__":
    unittest.main()
