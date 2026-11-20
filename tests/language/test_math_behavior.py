"""Mathematical behavior is additive to existing electrical and digital syntax."""

from pathlib import Path
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.digital import DigitalSimulation
from circuitlab.exporters.verilog import export_verilog
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.routing import schematic_layout
from circuitlab.layout.checks import preserves_nets
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError


def voltage(network, node):
    return network.voltage[network.nodes.index(node)]


class MathBehaviorTest(unittest.TestCase):
    def test_inline_equation_drives_voltage_without_a_visible_source(self):
        circuit = compile_circuit("V1(2) between(input,0);\nanalog:math V(out)=V(input)*10;\nR1(1k) between(out,0);")
        result = analyze(circuit).network
        self.assertAlmostEqual(voltage(result, "out"), 20)
        self.assertEqual([p.kind for p in circuit.components], ["V", "R"])
        self.assertEqual(result.names, ("V1", "R1"))

    def test_multiple_equations_and_si_literals(self):
        circuit = compile_circuit("""V1(2) between(input,0);
            analog:math { V(a)=V(input)*2; V(b)=V(a)+5V; V(out)=V(b)/3; }
        """)
        result = analyze(circuit).network
        self.assertAlmostEqual(voltage(result, "a"), 4)
        self.assertAlmostEqual(voltage(result, "b"), 9)
        self.assertAlmostEqual(voltage(result, "out"), 3)

    def test_math_only_constant_circuit_and_nonlinear_functions(self):
        circuit = compile_circuit("analog:math { V(a)=4V; V(out)=sqrt(V(a))+abs(-3V)+sin(pi/2); }")
        result = analyze(circuit).network
        self.assertAlmostEqual(voltage(result, "out"), 6)
        self.assertEqual(result.names, ())

    def test_comparator_example_uses_electrical_voltage_queries(self):
        definition = "chip Comparator(plus,minus,out){ digital { out=V(plus)>V(minus); } }"
        for a, b, expected in ((2, 1, 1), (1, 2, 0), (1, 1, 0)):
            with self.subTest(a=a, b=b):
                source = definition + f"\nVp({a}) between(a,0);\nVm({b}) between(b,0);\nmain{{Comparator(a,b,out);}}"
                circuit = compile_circuit(source)
                self.assertAlmostEqual(voltage(analyze(circuit).network, "out"), expected)
                top = next(hood_levels(circuit, set()))[1]
                self.assertEqual([part.kind for part in top.components].count("H"), 1)
                self.assertFalse(top.behavior)
                drawing = schematic_layout(top, component_labels(top), "labeled")
                self.assertTrue(preserves_nets(top, drawing))
                opened = list(hood_levels(circuit, {"@method:0"}))[-1][1]
                self.assertFalse(opened.components)

    def test_analog_opamp_model_alias_is_scoped_and_has_no_extra_symbol(self):
        source = """chip Amplifier(plus,minus,out,vp,vn) {
            op1(plus,minus,out,vp,vn);
            analog:math op1 { gain=100; bandwidth=1MHz; slew=2V/us; offset=1mV; }
        }
        main{Amplifier(a,b,x,pos,neg);Amplifier(c,d,y,pos,neg);}
        """
        circuit = compile_circuit(source)
        self.assertEqual(len(circuit.components), 2)
        self.assertTrue(all(p.kind == "O" and p.opamp.gain == 100 for p in circuit.components))

    def test_nested_circuit_include_main_and_private_math_nodes(self):
        source = 'include("math.ckt");\nV1(2) between(input,0);\nmain{Stage(input,x);Stage(input,y);}'
        definition = """circuit Double(a,b){analog:math V(b)=V(a)*2;}
            chip Stage(a,b){analog:math V(mid)=V(a)+1V;Double(mid,b);}
        """
        circuit = compile_circuit(source, path=Path.cwd() / "main.ckt", read_source=lambda _: definition)
        result = analyze(circuit).network
        self.assertAlmostEqual(voltage(result, "x"), 6)
        self.assertAlmostEqual(voltage(result, "y"), 6)
        self.assertEqual(len([n for n in result.nodes if n.endswith(".mid")]), 2)
        self.assertTrue(all(eq.path.name == "math.ckt" for eq in circuit.behavior))

    def test_time_math_and_comparison_follow_the_sample_time(self):
        circuit = compile_circuit("analog:math V(clock)=sin(2*pi*1Hz*t);\ndigital:math { out=V(clock)>0; }")
        sim = TimeDomain(circuit, .25)
        first = sim.advance()
        self.assertAlmostEqual(first[sim.nodes.index("clock")], 1)
        self.assertEqual(first[sim.nodes.index("out")], 1)
        sim.advance()
        third = sim.advance()
        self.assertAlmostEqual(third[sim.nodes.index("clock")], -1)
        self.assertEqual(third[sim.nodes.index("out")], 0)

    def test_digital_math_wrapper_preserves_existing_verilog_and_runtime(self):
        plain = "chip Logic(a,b,c,d,out){a,b,c,d=input;out=output;out=(a&b)|(c^d);}"
        wrapped = plain.replace("out=(a&b)|(c^d);", "digital:math {out=(a&b)|(c^d);}")
        self.assertEqual(export_verilog(plain), export_verilog(wrapped))
        sim = DigitalSimulation(wrapped)
        sim.set_input("a", 1)
        sim.set_input("b", 1)
        self.assertEqual(sim.values["out"], 1)

    def test_math_errors_are_source_diagnostics_and_unsafe_python_is_rejected(self):
        for expression in ("__import__('os')", "V(a).__class__", "[1,2]", "lambda:0", "2**1000000"):
            with self.subTest(expression=expression), self.assertRaises(CircuitError):
                circuit = compile_circuit(f"analog:math V(out)={expression};")
                analyze(circuit)
        with self.assertRaises(CircuitError) as error:
            compile_circuit("analog:math {\n V(out)=bad(1);\n}")
        self.assertEqual(error.exception.line, 2)

    def test_duplicate_drivers_are_rejected(self):
        with self.assertRaisesRegex(CircuitError, "Multiple mathematical drivers"):
            compile_circuit("analog:math {V(out)=1V;V(out)=2V;}")

    def test_electrical_bitwise_or_does_not_select_mixed_analysis(self):
        source = """Va(1) between(a,0);
            Vb(1) between(b,0);
            Vc(0) between(c,0);
            Vd(1) between(d,0);
            digital:math {out=(a&b)|(c^d);}
        """
        circuit = compile_circuit(source)
        self.assertEqual(circuit.mode, "dc")
        self.assertEqual(voltage(analyze(circuit).network, "out"), 1)

    def test_assembled_digital_math_preserves_main_and_nested_chip_calls(self):
        source = """chip Gate(a,b,out){a,b=input;out=output;digital:math{out=a^b;}}
            chip Nested(a,b,out){a,b=input;out=output;Gate(a,b,out);}
            main Board{x,y=input;z=output;Nested(x,y,z);}
        """
        sim = DigitalSimulation(source)
        sim.set_input("x", 1)
        self.assertEqual(sim.values["z"], 1)

    def test_affine_ac_preserves_phasors_without_adding_a_dc_constant(self):
        circuit = compile_circuit("V1(2Vrms) between(input,0);\nf=1k;\nanalog:math V(out)=V(input)*3+5V;")
        self.assertAlmostEqual(voltage(analyze(circuit).network, "out"), 6)

    def test_linked_executable_math_keeps_shared_connectors_and_local_nodes(self):
        source = ('include("behavior.ckt");\nconnector(input,out);\n'
                  'V1(2) between(input,0);\nR1(1k) between(out,0);')
        child = 'connector(input,out);\nanalog:math{V(mid)=V(input)+1V;V(out)=V(mid)*2;}'
        circuit = compile_circuit(source, path=Path.cwd() / "main.ckt", read_source=lambda _: child)
        result = analyze(circuit).network
        self.assertAlmostEqual(voltage(result, "out"), 6)
        self.assertIn("@file1.mid", result.nodes)

    def test_opamp_config_and_math_fields_do_not_mutate_static_variables(self):
        circuit = compile_circuit("""main {
            let gain=2;
            analog:math op1 { gain=100; }
            analog:math { V(given)=3V; }
            for(let i=0;i<gain;i++){R${i}(1k) between(a${i},0);}
            op1(given,out,out);
        }""")
        self.assertEqual(len([p for p in circuit.components if p.kind == "R"]), 2)
        self.assertEqual(next(p for p in circuit.components if p.kind == "O").opamp.gain, 100)
