"""Model declarations have useful editor help and working snippet defaults."""

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.tooling.source_info import SourceInfo
from tests.tooling.test_ide_snippets import default_snippet


class OpampModelEditorTest(unittest.TestCase):
    def test_parameter_help_is_limited_to_an_opamp_configuration(self):
        source = "opamp op1 {\n bandwidth=7.2MHz;\n}"
        self.assertIn("Gain-bandwidth", SourceInfo(source).hover_help(1, 4))
        self.assertEqual(SourceInfo("bandwidth=input;").hover_help(0, 4), "")

    def test_model_and_default_snippets_compile(self):
        for prefix in ("opamp-default", "opamp-model", "opamp-audio", "opamp-common"):
            with self.subTest(prefix=prefix):
                circuit = compile_circuit(default_snippet(prefix), render_only=True)
                self.assertEqual(len(circuit.components), 1)
                self.assertIsNotNone(circuit.components[0].audio if prefix == "opamp-audio"
                                     else circuit.components[0].opamp)

    def test_dotted_property_hover_covers_both_sides_of_the_separator(self):
        source = "op1(a,b,out,p,n,g1,g8,bp);\nanalog op1 {\n gain.default=20;\n}"
        for column in (2, 5, 8):
            self.assertIn("open gain pins", SourceInfo(source).hover_help(2,column))
        self.assertIn("gain1, gain8, bypass", SourceInfo(source).hover_help(0,1))

    def test_comparator_timing_and_typed_property_help(self):
        source = "analog comparator {\n delay.rise=115ns;\n output.low.at4mA=150mV;\n}"
        self.assertIn("collector opens", SourceInfo(source).hover_help(1, 5))
        self.assertIn("on-resistance", SourceInfo(source).hover_help(2, 12))
        source = "chip Filter(a,out) {\nanalog {\nproperty offset: V = 2mV;\nV(out)=V(a)+offset;\n}}"
        self.assertIn("inferred unit: V", SourceInfo(source).hover_help(3, 15))
        self.assertEqual(SourceInfo(source + "\nanalog V(other)=offset;").hover_help(5, 17), "")

    def test_stateful_filter_snippet_compiles_and_functions_explain_history(self):
        circuit = compile_circuit(default_snippet("analog-state"))
        self.assertEqual(len(circuit.behavior), 1)
        self.assertFalse(circuit.components)
        self.assertNotIn("error", circuit.nodes)
        for name, detail in (("integrate", "accepted electrical step"), ("delay", "transport"),
                             ("transition", "cancelled"), ("square", "cycles")):
            with self.subTest(function=name):
                self.assertIn(detail, SourceInfo(f"analog V(out)={name}(a,b);").hover_help(0,15))
