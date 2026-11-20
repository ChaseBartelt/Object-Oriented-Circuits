"""Parameterized op amps retain their behavior and pin order in exports."""

import unittest

from circuitlab.exporters.ltspice import export_schematic
from circuitlab.exporters.spice import export_spice, spice_circuit
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.routing import schematic_layout
from circuitlab.math.report import component_labels


SOURCE = """V1(1) between(input,0);
Vp(5) between(pos,0);
Vn(-5) between(neg,0);
opamp op1 { gain=100; bandwidth=1MHz; slew=2V/us; offset=1mV; supply_current=1mA; }
op1(input,out,out,pos,neg);"""


class OpampModelExportTest(unittest.TestCase):
    def test_netlist_retains_state_equation_slew_offset_and_supply_current(self):
        text = export_spice(SOURCE)
        self.assertIn("X_op1 out 0 input out pos neg EC_OpAmp1", text)
        self.assertIn(".subckt EC_OpAmp1 out return plus minus vplus vminus", text)
        self.assertIn("Cstate state return 1p", text)
        self.assertIn("limit(", text)
        self.assertIn("V(plus,minus)+(0.001)-0.01*V(state,return)", text)
        self.assertIn(",-2000000,2000000)", text)
        self.assertIn("Iquiescent vplus vminus DC 0.001", text)
        self.assertNotIn("EC_IdealOpAmp", text)

    def test_ltspice_symbol_uses_the_explicit_five_pin_adapter(self):
        circuit = compile_circuit(SOURCE)
        layout = schematic_layout(circuit, component_labels(circuit), "labeled")
        text, *_ = export_schematic(circuit, layout)
        self.assertIn("SYMATTR Value EC_OpAmp1_ASC", text)
        self.assertIn(".subckt EC_OpAmp1_ASC plus minus vplus vminus out", text)
        self.assertIn("Xcore out 0 plus minus vplus vminus EC_OpAmp1", text)

    def test_unpowered_gain_and_offset_models_use_the_correct_return(self):
        text = export_spice("V1(1) between(input,0);\nopamp op1 { gain=100; offset=1mV; }\n"
                            "op1(input,out,out);")
        self.assertIn("Boutput out return V=100*(V(plus,minus)+(0.001))", text)
        self.assertIn("Xcore out vminus plus minus EC_OpAmp1", text)

    def test_identical_instances_share_a_model_and_different_overrides_do_not(self):
        source = SOURCE + "\nopamp op2 { gain=100; bandwidth=1MHz; slew=2V/us; offset=1mV; supply_current=1mA; }\n"
        source += "op2(input,other,other,pos,neg);\nopamp op3 { gain=10; }\nop3(input,last,last);"
        parts = spice_circuit(compile_circuit(source)).parts
        self.assertEqual(parts["op1"].value, parts["op2"].value)
        self.assertNotEqual(parts["op1"].value, parts["op3"].value)

    def test_declared_ideal_defaults_keep_the_existing_export(self):
        source = "V1(1) between(input,0);\nop1(input,out,out);"
        self.assertEqual(export_spice(source), export_spice("opamp op1;\n" + source))
