"""Analog declarations and private state expressions preserve electrical syntax."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.math_blocks import ir_nodes
from circuitlab.language.syntax import source_rows
from circuitlab.math.analysis import analyze
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError


class AnalogStateSyntaxTest(unittest.TestCase):
    def test_standalone_and_grouped_properties_share_inferred_units(self):
        source = """V1(2V) between(input,0);
            analog {
                property gain = 3;
                property offset: V = 2mV;
                properties {output.max=12V;}
                V(out)=min(gain*V(input)+offset,output.max);
            }
        """
        circuit = compile_circuit(source)
        self.assertEqual([(p.name,p.value,p.unit) for p in circuit.properties],
                         [("gain",3,""),("offset",.002,"V"),("output.max",12,"V")])
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")],6.002)

    def test_typed_integer_boolean_and_voltage_values_are_validated(self):
        circuit = compile_circuit("analog{property count:int=3; property active:bool=true; V(out)=count*active;}")
        self.assertEqual([(p.name,p.value) for p in circuit.properties],[("count",3),("active",True)])
        for declaration in ("property count:int=2.5", "property offset:V=2mA", "property name:object=3"):
            with self.subTest(declaration=declaration),self.assertRaises(CircuitError) as caught:
                compile_circuit("analog{\n"+declaration+";\nV(out)=1;}\n")
            self.assertEqual(caught.exception.line,2)

    def test_forward_internal_variables_are_algebraic_not_circuit_nodes(self):
        circuit = compile_circuit("""V1(2V) between(input,0);
            analog {
                V(out)=later;
                later=first+3V;
                let first=2*V(input);
            }
        """)
        self.assertNotIn("first",circuit.nodes)
        self.assertNotIn("later",circuit.nodes)
        self.assertEqual(len(circuit.behavior),1)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")],7)

    def test_direct_and_indirect_algebraic_cycles_are_source_errors(self):
        for body in ("first=first+1;", "first=second+1; second=first+1;"):
            with self.subTest(body=body),self.assertRaisesRegex(CircuitError,"Cyclic internal variable") as caught:
                compile_circuit("analog{\n"+body+"\nV(out)=first;}\n")
            self.assertEqual(caught.exception.line,2)

    def test_variable_names_are_local_to_each_chip_definition(self):
        source = """chip A(a,out){analog{property gain=2; local=gain*V(a); V(out)=local;}}
            chip B(a,out){analog{V(out)=local;}}
            main{B(input,out);}
        """
        with self.assertRaisesRegex(CircuitError,"Unknown math name.*local"):
            compile_circuit(source)

    def test_nested_properties_and_analog_locals_do_not_shadow_static_control(self):
        circuit = compile_circuit("""main {
            let gain=2;
            analog {
                properties {offset=1V;}
                let gain=3;
                V(out)=gain+offset;
            }
            for(let i=0;i<gain;i++){R${i}(1k) between(out,0);}
        }""")
        self.assertEqual(len(circuit.components), 2)
        network = analyze(circuit).network
        self.assertEqual(network.voltage[network.nodes.index("out")], 4)

    def test_property_patch_is_resolved_before_internal_variable_lowering(self):
        source = """chip Amplify(a,out){analog{property gain:int=2; local=gain*V(a); V(out)=local;}}
            @override
            chip Amplify {analog{property gain:int=4;}}
            V1(2V) between(input,0);
            main{Amplify(input,out);}
        """
        network = analyze(compile_circuit(source)).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")],8)
        with self.assertRaisesRegex(CircuitError,"integer"):
            compile_circuit(source.replace("gain:int=4","gain:int=4.5"))

    def test_multiline_rhs_and_trailing_operators_retain_the_voltage_equation(self):
        circuit = compile_circuit("""V1(2V) between(input,0);
            analog {
                property gain=3;
                V(out) =
                    gain *
                    V(input) +
                    2mV;
            }
        """)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")],6.002)
        self.assertEqual(circuit.behavior[0].line,4)

    def test_percentage_scalars_do_not_continue_into_the_next_statement(self):
        source = "eta = 80%\nspeedRatio = 0.9\n"
        self.assertEqual([(text, line) for text, line, _ in source_rows(source)],
                         [("eta = 80%", 1), ("speedRatio = 0.9", 2)])
        circuit = compile_circuit(source, "ac")
        self.assertEqual(circuit.known["eta"], .8)
        self.assertEqual(circuit.known["speedRatio"], .9)
        circuit = compile_circuit("analog{\nproperty ratio = 80 %\nV(out)=ratio;\n}")
        self.assertEqual(circuit.properties[0].value, .8)

    def test_state_calls_lower_to_shared_private_identifiers(self):
        circuit = compile_circuit("""analog{
            property frequency=2MHz;
            phase=integrate(frequency,time);
            V(a)=phase;
            V(b)=phase+1;
        }""")
        states = [[node for node in ir_nodes(eq.tree) if node[0]=="state"] for eq in circuit.behavior]
        self.assertEqual(states[0][0][1],"integrate")
        self.assertEqual(states[0][0][2],states[1][0][2])
        self.assertEqual(states[0][0][3:],(("c",2e6),("c",0)))
        self.assertNotIn("phase",circuit.nodes)

    def test_delay_and_transition_duration_rules_are_explicit(self):
        circuit = compile_circuit("analog{a=delay(V(input),1us); b=delay(V(input),2us,3us); V(out)=a+b;}")
        kinds = {node[1] for node in ir_nodes(circuit.behavior[0].tree) if node[0]=="state"}
        self.assertEqual(kinds,{"delay","transition"})
        for expression in ("delay(V(input),-1us)","delay(V(input),V(control))","integrate(1,V(input))"):
            with self.subTest(expression=expression),self.assertRaises(CircuitError):
                compile_circuit("analog V(out)="+expression+";\n")

    def test_repeated_and_linked_instances_do_not_share_state(self):
        chip = "chip Integrator(a,out){analog{value=integrate(V(a)); V(out)=value;}}"
        circuit = compile_circuit(chip+"\nmain{Integrator(a,x); Integrator(b,y);}")
        keys = [next(node[2] for node in ir_nodes(eq.tree) if node[0]=="state") for eq in circuit.behavior]
        self.assertEqual(len(set(keys)),2)
        root = 'include("child.ckt");\nanalog V(root)=integrate(1);\n'
        child = "analog V(child)=integrate(1);\n"
        circuit = compile_circuit(root,path=Path.cwd()/"root.ckt",read_source=lambda _:child)
        keys = [next(node[2] for node in ir_nodes(eq.tree) if node[0]=="state") for eq in circuit.behavior]
        self.assertEqual(len(set(keys)),2)

    def test_vco_source_uses_properties_locals_integration_and_square_without_new_symbols(self):
        circuit = compile_circuit('include("VCO");\nmain{VCO(control,clock,vcc,0);}\n')
        self.assertFalse(circuit.components)
        self.assertEqual(len(circuit.behavior),1)
        kinds = {(node[0],node[1]) for node in ir_nodes(circuit.behavior[0].tree) if node[0] in {"call","state"}}
        self.assertIn(("call","square"),kinds)
        self.assertIn(("state","integrate"),kinds)

    def test_digital_hdl_does_not_accidentally_accept_analog_state(self):
        for statement in ("property gain=2;", "out=delay(a,1us);"):
            with self.subTest(statement=statement),self.assertRaises(CircuitError):
                export_verilog("chip C(a,out){a=input; out=output; digital{"+statement+"}}")

    def test_hdl_chip_calls_and_signal_names_are_not_analog_state_keywords(self):
        for name in ("delay", "integrate", "transition"):
            with self.subTest(name=name):
                source = f"""chip {name}(a,y) {{
                    a=input; y=output; property=wire;
                    property=a; digital{{y=property;}}
                }}
                main{{a=input;y=output;{name}(a,y);}}
                """
                export_verilog(source)
                simulation = DigitalSimulation(source)
                simulation.set_input("a", 1)
                self.assertEqual(simulation.values["y"], 1)
