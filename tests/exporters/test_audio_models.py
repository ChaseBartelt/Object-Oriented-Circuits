"""Audio export retains all eight nets and the same behavioral network."""

import unittest

from circuitlab.exporters.ltspice import AUDIO_SYMBOL_NAME, export_hierarchy
from circuitlab.exporters.spice import export_spice
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.routing import fast_layout
from circuitlab.math.report import component_labels


SOURCE = """V1(10mV) between(input,0);
V2(6V) between(vcc,0);
op1(input,0,out,vcc,0,g1,g8,bypass);
"""


class AudioExportTest(unittest.TestCase):
    def test_spice_call_and_subcircuit_pin_order_match_the_source(self):
        text = export_spice(SOURCE)
        self.assertRegex(text, r"\nX_op1 input 0 out vcc 0 g1 g8 bypass EC_AudAmp\d+\n")
        self.assertIn("plus minus out vplus vminus gain1 gain8 bypass", text)
        self.assertIn("Rgain gain1 gain8 1350", text)
        self.assertIn("Gfeedback gain8 gain1 out bypass", text)
        self.assertIn("Rbypass bias bypass 50000", text)
        self.assertIn("Isupply vplus vminus DC 0.004", text)

    def test_ltspice_saves_a_real_eight_contact_cell_symbol(self):
        circuit = compile_circuit(SOURCE)
        drawing = fast_layout(circuit, component_labels(circuit))
        sheet, files, _ = export_hierarchy(circuit, drawing)
        self.assertIn(f"SYMBOL {AUDIO_SYMBOL_NAME} ", sheet)
        symbol = files[f"{AUDIO_SYMBOL_NAME}.asy"]
        self.assertEqual(symbol.count("PINATTR SpiceOrder"), 8)
        self.assertIn("SymbolType CELL", symbol)
        self.assertIn("SYMATTR Prefix X", symbol)
        self.assertIn("PINATTR PinName gain1", symbol)
        self.assertIn("PINATTR PinName gain8", symbol)
        self.assertIn("PINATTR PinName bypass", symbol)
        self.assertIn(".subckt EC_AudAmp", sheet)

    def test_bundled_lm386_exports_the_audio_cell_inside_its_package(self):
        source = SOURCE.replace("op1(input,0,out,vcc,0,g1,g8,bypass);",
                                'include("LM386");\nmain { LM386(g1,0,input,0,out,vcc,bypass,g8); }')
        circuit = compile_circuit(source)
        from circuitlab.layout.hierarchy import hood_levels
        view = next(hood_levels(circuit, set()))[1]
        sheet, files, _ = export_hierarchy(circuit, fast_layout(view, component_labels(view)))
        children = [text for name, text in files.items() if name.endswith(".asc")]
        self.assertEqual(len(children), 1)
        self.assertIn(f"SYMBOL {AUDIO_SYMBOL_NAME} ", children[0])
        self.assertIn("ec_audamp_core.asy", files)
        self.assertIn(".subckt EC_AudAmp", sheet)
