"""Dotted property namespaces preserve model types and source diagnostics."""

import unittest

from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError, VoltageLimit
from circuitlab.model.properties import COMPONENT_CATEGORIES, MODEL_PROPERTIES
from circuitlab.tooling.source_info import SourceInfo


class ModelPropertyTest(unittest.TestCase):
    def test_common_properties_parse_voltage_rail_expressions_and_decibels(self):
        part, = compile_circuit("op1(a,b,out,high,low); analog op1 {common.min=vminus+3V; "
                                "common.max=vplus; common.rejection=100dB; common.impedance=1G;}").components
        self.assertEqual(part.opamp.common_min, VoltageLimit(3, 0, 1))
        self.assertEqual(part.opamp.common_max, VoltageLimit(0, 1, 0))
        self.assertEqual(part.opamp.common_rejection, 100)
        self.assertEqual(part.opamp.common_impedance, 1e9)

    def test_common_parameter_errors_retain_the_property_line(self):
        for field in ("common.min=3A", "common.max=unknown+3V", "common.min=vplus*vminus",
                      "common.rejection=-1dB", "common.rejection=3A", "common.impedance=0",
                      "common.impedance=1V"):
            with self.subTest(field=field), self.assertRaises(CircuitError) as caught:
                compile_circuit(f"op1(a,b,out,p,n);\nanalog op1 {{\n{field};\n}}")
            self.assertEqual(caught.exception.line, 3)

    def test_rail_relative_bounds_require_the_five_pin_form(self):
        with self.assertRaisesRegex(CircuitError, "both supply pins"):
            compile_circuit("op1(a,b,out); analog op1 {common.min=vminus+3V;}")

    def test_common_defaults_can_be_restored_by_an_explicit_override(self):
        circuit = compile_circuit("op1(a,b,out); analog op1 {common.impedance=1G; common.rejection=100dB;}\n"
                                  "@override\nanalog op1 {common.impedance=ideal; common.rejection=ideal;}\n")
        self.assertIsNone(circuit.components[0].opamp.common_impedance)
        self.assertIsNone(circuit.components[0].opamp.common_rejection)

    def test_property_catalog_separates_implemented_fields_from_planned_categories(self):
        self.assertEqual(COMPONENT_CATEGORIES["opamp"], ("gain","gbw","slew","offset","common","output","supply"))
        self.assertIn("distortion", COMPONENT_CATEGORIES["audio"])
        self.assertNotIn("distortion.total", MODEL_PROPERTIES)
        self.assertEqual(MODEL_PROPERTIES["common.rejection"].unit, "dB")
        self.assertEqual(MODEL_PROPERTIES["gain.default"].models, ("audio",))
        self.assertIn("CMRR", SourceInfo("analog op1 {\ncommon.rejection=100dB;\n}").hover_help(1,9))
