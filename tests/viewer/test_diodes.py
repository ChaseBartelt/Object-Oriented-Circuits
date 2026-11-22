"""Diode glyphs, terminal rotation, source edits and editor inspection."""
import os
from pathlib import Path
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.math.report import component_labels, result_rows
from circuitlab.tooling.source_info import SourceInfo
from circuitlab.tooling.source_edits import edit_value
from circuitlab.viewer.schematic import SchematicView
from circuitlab.layout.geometry import routed_pins
from circuitlab.layout.checks import preserves_nets


class DiodeViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_presets_draw_and_report_without_moving_contacts(self):
        for kind in ("rect", "Schot", "Zener", "LED", "Tun", "PIN", "Varactor", "Photo", "Avalanche"):
            with self.subTest(kind=kind):
                source = f"V1(5) between(v,0);\nR1(1k) between(v,out);\nD.{kind}1() between(out,0);"
                circuit = compile_circuit(source, "dc")
                result = analyze(circuit)
                self.assertTrue(result_rows(result))
                view = SchematicView()
                try:
                    view.draw(circuit)
                    symbol = next(symbol for symbol in view.layout.symbols if symbol[0].kind == "D")
                    before = routed_pins(symbol, 0)
                    after = routed_pins(symbol, 1)
                    self.assertEqual([net for net,_ in before], ["out", "0"])
                    self.assertEqual([net for net,_ in after], ["out", "0"])
                    self.assertNotEqual(before, after)
                    self.assertIn("D."+kind+"1",component_labels(circuit))
                finally:
                    view.close()

    def test_typed_diode_wires_hover_and_value_edit(self):
        source = "D.Zener1(5.1V) between(0,out); // keep polarity"
        info = SourceInfo(source)
        self.assertEqual(info.wire_at(0,source.index("out")+1)[1:], ("D.Zener1",1,"out"))
        self.assertIn("anode",info.hover_help(0,4))
        path = Path("diode.ckt").resolve()
        circuit = compile_circuit(source,"dc",path=path)
        plan = edit_value(circuit,"D.Zener1","6.2V, rs=2ohm",path,source,lambda p:source)
        changed = compile_circuit(plan.after,"dc")
        self.assertEqual(changed.components[0].diode.breakdown,6.2)
        self.assertEqual(changed.components[0].terminal_nets,("0","out"))
        self.assertIn("// keep polarity",plan.after)

    def test_diodes_do_not_prevent_the_other_symbols_from_getting_short_leads(self):
        source = (Path(__file__).resolve().parents[2] / "examples/diodes.ckt").read_text()
        circuit = compile_circuit(source, "dc")
        view = SchematicView()
        try:
            view.draw(circuit)
            self.assertTrue(preserves_nets(circuit, view.layout))
            for part,a,b,_ in view.layout.symbols:
                span = abs(a[0]-b[0]) + abs(a[1]-b[1])
                if part.kind == "R":
                    self.assertLessEqual(span, 2.0, part.name)
                if part.kind == "D":
                    self.assertLessEqual(span, 1.5, part.name)
        finally:
            view.close()
