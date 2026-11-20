"""Math is exported as behavior, never as additional schematic symbols."""

import unittest

from circuitlab.exporters.spice import export_spice
from circuitlab.exporters.ltspice import export_hierarchy
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.routing import schematic_layout
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError


class MathExportTest(unittest.TestCase):
    def test_netlist_contains_the_equation_and_its_actual_net_names(self):
        text = export_spice("V1(2) between(input,0);\nanalog:math V(out)=V(input)*10+5V;")
        self.assertRegex(text, r"\nB\S+ out 0 V=")
        self.assertIn("V(input,0)", text)
        self.assertIn("(10)", text)
        self.assertIn("(5)", text)

    def test_math_only_package_exports_ports_and_behavior_without_fake_symbols(self):
        source = """chip Comparator(plus,minus,out){digital{out=V(plus)>V(minus);}}
            Vp(2) between(a,0);
            Vm(1) between(b,0);
            R1(1k) between(out,0);
            main{Comparator(a,b,out);}
        """
        circuit = compile_circuit(source)
        view = next(hood_levels(circuit, set()))[1]
        drawing = schematic_layout(view, component_labels(view), "labeled")
        sheet, files, *_ = export_hierarchy(circuit, drawing)
        children = [text for name, text in files.items() if name.endswith(".asc")]
        self.assertEqual(len(children), 1)
        self.assertRegex(children[0], r"!B\S+ out 0 V=if")
        self.assertNotIn("SYMBOL", children[0])
        self.assertIn("SYMATTR InstName X_Comparator", sheet)

    def test_state_history_export_is_a_source_located_explicit_error(self):
        for expression in ("integrate(V(input),0V)", "delay(V(input),1ms)",
                           "transition(V(input)>1V,1us,2us)"):
            with self.subTest(expression=expression), self.assertRaises(CircuitError) as caught:
                export_spice("V1(2V) between(input,0);\nanalog V(out)=" + expression + ";")
            self.assertEqual(caught.exception.code, "UnsupportedStatefulExport")
            self.assertEqual(caught.exception.line, 2)

    def test_configured_comparator_export_cannot_silently_use_the_drawn_transistors(self):
        source = ('include("LM311");\nV1(5V) between(vcc,0);\n'
                  'main {LM311(0,a,b,0,bal,strobe,out,vcc);}')
        with self.assertRaises(CircuitError) as caught:
            export_spice(source)
        self.assertEqual(caught.exception.code, "UnsupportedComparatorExport")
