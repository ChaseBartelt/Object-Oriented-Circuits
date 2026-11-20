"""Custom chip properties infer units and become scoped equation constants."""

from pathlib import Path
import unittest

from circuitlab.exporters.ltspice import export_hierarchy
from circuitlab.exporters.spice import export_spice
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.properties import property_value
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.routing import fast_layout
from circuitlab.math.analysis import analyze
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.source_info import SourceInfo


CHIP = """chip MyAmplifier(input, out, gnd) {
    analog {
        properties {
            gain = 10;
            offset = 2mV;
            output.max = 12V;
        }
        V(out, gnd) = min(
            gain * V(input, gnd) + offset,
            output.max
        );
    }
}
"""
MAIN = "V1(2V) between(input,0);\nmain {MyAmplifier(input,out,0);}\n"
PATCH = "@override\nchip MyAmplifier {analog {properties {gain=2; offset=3mV; output.max=9V;}}}\n"


class CustomPropertyTest(unittest.TestCase):
    def test_requested_grouped_form_infers_units_and_evaluates(self):
        circuit = compile_circuit(CHIP + MAIN)
        self.assertEqual([(p.name,p.value,p.unit) for p in circuit.properties],
                         [("gain",10,""),("offset",.002,"V"),("output.max",12,"V")])
        for network in (analyze(circuit).network, TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 12)
            self.assertEqual(network.names, ("V1",))
        self.assertFalse(any("gain" in node or "output.max" in node for node in circuit.nodes))

    def test_chip_patch_changes_constants_before_the_original_equation_is_compiled(self):
        for source in (CHIP + PATCH + MAIN, CHIP + MAIN + PATCH, PATCH + CHIP + MAIN):
            network = analyze(compile_circuit(source)).network
            self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 4.003)

    def test_includes_preserve_properties_and_original_package_connections(self):
        entry = Path.cwd() / "main.ckt"
        base = compile_circuit('include("amplifier.ckt");\n' + MAIN, path=entry, read_source=lambda _: CHIP)
        patched = compile_circuit('include("amplifier.ckt");\n' + MAIN + PATCH, path=entry, read_source=lambda _: CHIP)
        self.assertEqual(base.instances, patched.instances)
        self.assertEqual(base.components, patched.components)
        self.assertAlmostEqual(analyze(patched).network.voltage[patched.nodes.index("out")], 4.003)

    def test_each_instance_has_its_own_property_metadata(self):
        source = CHIP + "V1(1V) between(a,0);\nV2(2V) between(b,0);\nmain {MyAmplifier(a,x,0); MyAmplifier(b,y,0);}\n"
        circuit = compile_circuit(source)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("x")], 10.002)
        self.assertAlmostEqual(network.voltage[network.nodes.index("y")], 12)
        self.assertEqual(len(circuit.properties), 6)
        self.assertEqual(len({p.scope for p in circuit.properties}), 2)
        self.assertEqual({p.owner for p in circuit.properties}, {0,1})

    def test_properties_can_follow_equations_and_be_shared_with_electrical_digital_math(self):
        source = """V1(2V) between(input,0);
            analog V(out)=gain*V(input)+offset;
            analog {properties {gain=3; offset=2mV; threshold=5V;}}
            digital {logic=V(out)>threshold;}
        """
        circuit = compile_circuit(source)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 6.002)
        self.assertEqual(network.voltage[network.nodes.index("logic")], 1)
        self.assertEqual(circuit.mode, "dc")

    def test_properties_do_not_leak_between_chip_definitions(self):
        source = CHIP + "chip Other(a,out){analog V(out)=gain*V(a);}\nmain{Other(input,out);}\n"
        with self.assertRaisesRegex(CircuitError, "Unknown math name.*gain"):
            compile_circuit(source)

    def test_nested_properties_and_digital_or_do_not_select_ac_or_mixed_mode(self):
        source = """V1(1V) between(a,0);
            V2(0V) between(b,0);
            digital {
                properties {f=1; invert=false;}
                out=(a|b)^invert;
            }
        """
        circuit = compile_circuit(source)
        self.assertEqual(circuit.mode, "dc")
        network = analyze(circuit).network
        self.assertEqual(network.voltage[network.nodes.index("out")], 1)

    def test_duplicates_unknown_overrides_and_incompatible_units_are_errors(self):
        for change in ("gain=2; gain=3;", "missing=2V;", "offset=3A;"):
            patch = "@override\nchip MyAmplifier {analog {properties {" + change + "}}}\n"
            with self.subTest(change=change), self.assertRaises(CircuitError):
                compile_circuit(CHIP + MAIN + patch)
        with self.assertRaisesRegex(CircuitError, "already declared"):
            compile_circuit("analog {properties {gain=2; gain=3;} V(out)=gain;}\n")

    def test_equal_priority_patch_conflicts_are_order_independent(self):
        for values in ((2,3),(3,2)):
            patches = "\n".join("@override\nchip MyAmplifier {analog {properties {gain=" + str(v) + ";}}}" for v in values)
            with self.assertRaisesRegex(CircuitError, "Conflicting overrides"):
                compile_circuit(CHIP + MAIN + patches)

    def test_unit_inference_and_override_validation_cover_si_values(self):
        for raw, value, unit in (("2mV",.002,"V"),("12V",12,"V"),("1Gohm",1e9,"ohm"),
                                 ("1G",1e9,""),("2.5V/us",2.5e6,"V/s"),("true",True,"bool")):
            with self.subTest(raw=raw):
                self.assertEqual(property_value(raw), (value,unit))
        for raw in ("nan", "1e999", "__import__('os')", "3-4j"):
            with self.subTest(raw=raw), self.assertRaises(CircuitError):
                compile_circuit("analog {properties {gain=" + raw + ";} V(out)=gain;}\n")

    def test_only_declared_dotted_names_can_be_read(self):
        with self.assertRaisesRegex(CircuitError, "Dotted math names"):
            compile_circuit("analog V(out)=output.max;\n")
        with self.assertRaises(CircuitError):
            compile_circuit("analog {properties {x.__class__=1;} V(out)=x.__class__;}\n")

    def test_property_values_export_as_behavior_without_extra_symbols(self):
        circuit = compile_circuit(CHIP + MAIN + PATCH)
        view = next(hood_levels(circuit,set()))[1]
        drawing = fast_layout(view,component_labels(view))
        self.assertTrue(preserves_nets(view,drawing))
        self.assertCountEqual([p.kind for p in view.components], ["V","H"])
        sheet,files,_ = export_hierarchy(circuit,drawing)
        child = next(text for name,text in files.items() if name.endswith(".asc"))
        self.assertNotIn("SYMBOL",child)
        self.assertIn("V=",child)
        netlist = export_spice(CHIP + MAIN + PATCH)
        self.assertIn("(2)",netlist)
        self.assertNotIn("properties",netlist)
        self.assertNotIn("output.max",netlist)

    def test_hover_describes_custom_dotted_properties_in_their_chip_scope(self):
        help_text = SourceInfo(CHIP).hover_help(5,18)
        self.assertIn("output.max = 12V",help_text)
        self.assertIn("inferred unit: V",help_text)
