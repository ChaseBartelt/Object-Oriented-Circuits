"""Unimplemented model math cannot hide valid topology or become fake numbers."""

from pathlib import Path
import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.devices import device_preview, library_devices
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout
from circuitlab.math.analysis import analyze
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError


class AnalogPreviewDeferTest(unittest.TestCase):
    def test_valid_math_is_unchanged_by_topology_preview(self):
        source = """V1(2V) between(input,0);
            analog {property gain=3; property offset:V=2mV;
                internal=gain*V(input)+offset; V(out)=internal;}
        """
        ordinary = compile_circuit(source)
        preview = compile_circuit(source,render_only=True)
        self.assertFalse(preview.model_issues)
        self.assertEqual(ordinary.properties,preview.properties)
        self.assertEqual(ordinary.behavior,preview.behavior)
        network = analyze(preview).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")],6.002)

    def test_bad_property_is_omitted_and_never_reinterpreted_as_a_digital_node(self):
        source = """V1(1V) between(input,0);
            analog {property memory.capacity=44kbit; V(good)=V(input);}
            digital {bad=memory.capacity;}
        """
        with self.assertRaises(CircuitError):
            compile_circuit(source)
        preview = compile_circuit(source,render_only=True)
        self.assertTrue(preview.model_issues)
        self.assertFalse(preview.properties)
        self.assertEqual([eq.output for eq in preview.behavior],["good"])
        self.assertNotIn("memory.capacity",preview.nodes)
        self.assertNotIn("bad",preview.nodes)
        with self.assertRaises(CircuitError) as caught:
            analyze(preview)
        self.assertEqual(caught.exception.code,"UnsupportedModelProperty")

    def test_bad_typed_override_does_not_fall_back_to_the_default_value(self):
        source = """V1(1V) between(input,0);
            analog {property gain:V=2V; V(bad)=gain; V(good)=V(input);}
            @override
            analog {property gain=3A;}
        """
        with self.assertRaises(CircuitError):
            compile_circuit(source)
        preview = compile_circuit(source,render_only=True)
        self.assertFalse(preview.properties)
        self.assertTrue(preview.model_issues)
        self.assertEqual([eq.output for eq in preview.behavior],["good"])

    def test_unsupported_current_adc_and_dac_equations_keep_valid_independent_math(self):
        for unsupported in ("I(0,out)=1mA;", "sample=ADC(V(input)); V(out)=DAC(sample);", "V(out)=DAC(V(input));"):
            source = "V1(1V) between(input,0);\nanalog {\nV(good)=V(input);\n"+unsupported+"\n}\n"
            with self.subTest(unsupported=unsupported):
                with self.assertRaises(CircuitError):
                    compile_circuit(source)
                preview = compile_circuit(source,render_only=True)
                self.assertTrue(preview.model_issues)
                self.assertEqual([eq.output for eq in preview.behavior],["good"])
                self.assertTrue(all(issue.line is not None for issue in preview.model_issues))
                with self.assertRaisesRegex(CircuitError,"Numerical model is incomplete"):
                    preview.require_models()

    def test_current_library_proposals_still_have_editable_topology(self):
        devices = {d.key:d for d in library_devices()}
        for key in ("lm13700","pt2399"):
            with self.subTest(key=key):
                circuit,root,view,boundary = device_preview(devices[key])
                self.assertIsNotNone(root)
                self.assertIsNotNone(view)
                self.assertTrue(boundary)
                self.assertTrue(circuit.model_issues)
                with self.assertRaises(CircuitError):
                    circuit.require_models()
                with self.assertRaises(CircuitError):
                    compile_circuit(devices[key].example)

    def test_malformed_property_block_syntax_stays_a_real_source_error(self):
        source = "V1(1V) between(input,0);\nanalog {properties { gain: ; } V(out)=V(input);}\n"
        with self.assertRaises(CircuitError):
            compile_circuit(source,render_only=True)

    def test_wholly_unsupported_math_chip_keeps_its_package_boundary_and_saved_layout(self):
        source = """chip Pending(a,out){analog{
                property memory.capacity=44kbit;
                V(out)=ADC(V(a));
            }}
            main{Pending(input,out);}
        """
        circuit = compile_circuit(source,render_only=True)
        self.assertFalse(circuit.components)
        self.assertFalse(circuit.behavior)
        self.assertTrue(circuit.model_issues)
        self.assertEqual({issue.owner for issue in circuit.model_issues},{0})
        top = next(hood_levels(circuit,set()))[1]
        self.assertEqual([part.kind for part in top.components],["H"])
        self.assertEqual(top.components[0].package_pins,(("a","input"),("out","out")))
        drawing = fast_layout(top,component_labels(top))
        self.assertTrue(preserves_nets(top,drawing))
        restored = restore_layout(pack_layout(drawing),top,())
        self.assertIsNotNone(restored)
        self.assertTrue(preserves_nets(top,restored))
        detail = next(row for row in hood_levels(circuit,{"@method:0"}) if row[0]=="@method:0")
        self.assertFalse(detail[1].components)
        self.assertEqual(set(detail[3]),{"input","out"})
        ports = fast_layout(detail[1],component_labels(detail[1]),boundary=detail[3])
        self.assertEqual(set(ports.boundary),set(detail[3]))
        self.assertTrue(preserves_nets(detail[1],ports))
        self.assertIsNotNone(restore_layout(pack_layout(ports),detail[1],detail[3]))
        with self.assertRaises(CircuitError):
            circuit.require_models()
        with self.assertRaises(CircuitError):
            compile_circuit(source)

    def test_nested_failed_math_preserves_the_visible_enclosing_chip(self):
        source = """circuit Inner(a,out){analog{V(out)=ADC(V(a));}}
            chip Outer(a,out){Inner(a,out);}
            main{Outer(input,out);}
        """
        circuit = compile_circuit(source,render_only=True)
        self.assertEqual({issue.owner for issue in circuit.model_issues},{1})
        top = next(hood_levels(circuit,set()))[1]
        self.assertEqual([part.label for part in top.components],["Outer"])
        inside = next(row for row in hood_levels(circuit,{"@method:0"}) if row[0]=="@method:0")
        self.assertEqual([part.label for part in inside[1].components],["Inner"])

    def test_issue_ownership_is_shifted_when_executable_files_are_linked(self):
        root = '''include("child.ckt");
            chip RootChip(a,out){analog{V(out)=ADC(V(a));}}
            main{RootChip(input,out);}
        '''
        child = '''chip ChildChip(a,out){analog{V(out)=DAC(V(a));}}
            main{ChildChip(child_input,child_out);}
        '''
        circuit = compile_circuit(root,path=Path.cwd()/"root.ckt",read_source=lambda _:child,render_only=True)
        self.assertEqual({issue.owner for issue in circuit.model_issues},{0,1})
        top = next(hood_levels(circuit,set()))[1]
        self.assertCountEqual([part.label for part in top.components],["RootChip","ChildChip"])
        drawing = fast_layout(top,component_labels(top))
        self.assertTrue(preserves_nets(top,drawing))
        with self.assertRaises(CircuitError):
            analyze(circuit)
