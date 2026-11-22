"""Diode exports keep polarity and carry their own nonlinear model definitions."""
import unittest
from circuitlab.language.compiler import compile_circuit
from circuitlab.exporters.spice import export_spice
from circuitlab.exporters.ltspice import export_schematic
from circuitlab.layout.routing import fast_layout
from circuitlab.math.report import component_labels

SOURCE = "V1(12) between(supply,0);\nR1(1k) between(supply,out);\nD.Zener1(5.1V) between(0,out);"


class DiodeExportTest(unittest.TestCase):
    def test_netlist_has_self_contained_models_and_anode_cathode_order(self):
        output = export_spice(SOURCE)
        self.assertIn("X_D_Zener1 0 out EC_Diode1", output)
        self.assertIn(".subckt EC_Diode1 a k", output)
        self.assertIn("Bjunction j k I=", output)
        self.assertIn("Bcharge j k I={ddt(", output)
        self.assertIn("5.1", output)
        self.assertIn(".ends EC_Diode1", output)

    def test_tunnel_and_photo_exports_keep_their_special_current_terms(self):
        output = export_spice(SOURCE.replace("D.Zener1(5.1V)","D.Tun1(ip=7mA, vp=80mV)"))
        self.assertIn("0.007", output)
        self.assertIn("0.08", output)
        self.assertIn("+if(V(j,k)<=0", output)
        output = export_spice(SOURCE.replace("D.Zener1(5.1V)","D.Photo1(light=25uA)"))
        self.assertIn("2.5e-05", output)

    def test_ltspice_symbol_is_a_two_pin_subcircuit(self):
        circuit = compile_circuit(SOURCE, "dc")
        layout = fast_layout(circuit, component_labels(circuit))
        output, _ = export_schematic(circuit, layout)
        self.assertIn("SYMBOL diode ", output)
        self.assertIn("SYMATTR Prefix X", output)
        self.assertIn("SYMATTR InstName X_D_Zener1", output)
