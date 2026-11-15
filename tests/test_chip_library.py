"""Exercise library includes through analysis, rendering and export."""

from itertools import product
from pathlib import Path
import unittest

from circuitlab.analysis import analyze
from circuitlab.compiler import compile_circuit
from circuitlab.graph import CircuitError
from circuitlab.hood import hood_levels
from circuitlab.layout_checks import preserves_nets
from circuitlab.ltspice import export_hierarchy
from circuitlab.report import component_labels
from circuitlab.routing import fast_layout
from circuitlab.spice import export_spice


class ChipLibraryTest(unittest.TestCase):
    def test_nand_called_from_twice_included_library(self):
        for a, b in product((0, 5), repeat=2):
            with self.subTest(a=a, b=b):
                source = f"""include("@chips/logic/74HC00.ckt");
include("@chips/logic/SN74HC00.ckt");
V1(5) between(vcc, 0);
V2({a}) between(a, 0);
V3({b}) between(b, 0);
main {{
    SN74HC00(a, b, y1, 0, 0, y2, 0, y3, 0, 0, y4, 0, 0, vcc);
    SN74HC00(a, b, z1, 0, 0, z2, 0, z3, 0, 0, z4, 0, 0, vcc);
}}
"""
                circuit = compile_circuit(source)
                roots = [inst for inst in circuit.instances if inst.parent is None]
                self.assertEqual([inst.pins[2] for inst in roots], [("y1", "y1"), ("y1", "z1")])
                self.assertEqual(len({p.name for p in circuit.components}), len(circuit.components))
                network = analyze(circuit).network
                for pin in ("y1", "z1"):
                    self.assertEqual(network.voltage[network.nodes.index(pin)] > 2.5, not (a and b))

    def test_pinout_renders_and_explains_why_analysis_is_unavailable(self):
        source = """include("@chips/converters/ADS1115.ckt");
main {
    ADS1115(addr, ready, 0, a0, a1, a2, a3, vdd, sda, scl);
}
"""
        circuit = compile_circuit(source, render_only=True)
        view = next(hood_levels(circuit, set()))[1]
        package, = view.components
        self.assertEqual(package.label, "ADS1115")
        self.assertEqual(package.package_pins[-2:], (("sda", "sda"), ("scl", "scl")))
        with self.assertRaisesRegex(CircuitError, "ADS1115 is available as a schematic pinout"):
            analyze(circuit)
        with self.assertRaisesRegex(CircuitError, "ADS1115 is available as a schematic pinout"):
            export_spice(source)

    def test_relative_definition_and_connector_circuits_work_together(self):
        path = Path.cwd() / "consumer.ckt"
        source = """include("definition.ckt");
include("load.ckt");
connector(out);
V1(5) between(vcc, 0);
main { Divider(vcc, out); }
"""
        sources = {
            "definition.ckt": "circuit Divider(vcc, out) { R1(1k) between(vcc, out); }",
            "load.ckt": "connector(out);\nR1(1k) between(out, 0);",
        }
        circuit = compile_circuit(source, path=path, read_source=lambda p: sources[p.name])
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("out")], 2.5)
        imported = next(p for p in circuit.components if p.kind == "R" and p.p == "vcc")
        self.assertEqual(circuit.component_files[imported.name], path.with_name("definition.ckt"))

    def test_switch_model_solves_and_exports(self):
        for drive, expected in ((0, 30e-6), (5, 30000 / 10470)):
            with self.subTest(drive=drive):
                source = f"""include("@chips/switches/CD4066.ckt");
V1(3) between(a, 0);
V2({drive}) between(enable, 0);
V3(5) between(vdd, 0);
R1(10k) between(b, 0);
main {{ CD4066(a, b, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, enable, vdd); }}
"""
                circuit = compile_circuit(source)
                network = analyze(circuit).network
                self.assertAlmostEqual(network.voltage[network.nodes.index("b")], expected, places=7)
                self.assertIn("a b enable 0 EC_SW1", export_spice(source))
                view = next(hood_levels(circuit, set()))[1]
                layout = fast_layout(view, component_labels(view))
                self.assertTrue(preserves_nets(view, layout))
                asc, companions, _ = export_hierarchy(circuit, layout)
                self.assertIn(".model EC_SW1 SW(", asc)
                self.assertTrue(any("SYMBOL sw " in text for text in companions.values()))
