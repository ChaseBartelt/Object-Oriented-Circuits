"""Exercise library includes through analysis, rendering and export."""
# Treat catalog definitions as the models actually supplied by the application, not as proof
# of manufacturer-level accuracy. Includes, pinout-only models, and physical exports are separate.

from itertools import product
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from circuitlab.math.analysis import analyze
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.linked import CHIP_LIBRARY, include_path, read_includes
from circuitlab.model.graph import CircuitError
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.checks import preserves_nets
from circuitlab.exporters.ltspice import export_hierarchy
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import fast_layout
from circuitlab.exporters.spice import export_spice


class ChipLibraryTest(unittest.TestCase):
    def test_every_bundled_chip_resolves_by_short_name(self):
        files = list(CHIP_LIBRARY.rglob("*.ckt"))
        self.assertEqual(len(files), 59)
        for file in files:
            with self.subTest(chip=file.stem):
                self.assertEqual(include_path(file.stem), file.resolve())
                self.assertEqual(include_path(file.stem.swapcase()), file.resolve())

    def test_short_and_explicit_imports_share_definitions_and_spice_output(self):
        source = (Path(__file__).resolve().parents[2] / "tests/fixtures/refactor/chip_library_nand.ckt").read_text()
        source = source.replace('include("@chips/logic/74HC00.ckt");', 'include("74HC00");')
        short = source.replace('include("74HC00");', 'include("74HC00");\ninclude("@chips/logic/74HC00.ckt");')
        explicit = source.replace('include("74HC00");', 'include("@chips/logic/74HC00.ckt");')
        self.assertEqual(export_spice(short), export_spice(explicit))
        for path in (None, Path.cwd() / "example.ckt"):
            with self.subTest(path=path):
                circuit = compile_circuit(short, path=path)
                self.assertEqual(len([inst for inst in circuit.instances if inst.parent is None]), 1)
                view = next(hood_levels(circuit, set()))[1]
                self.assertTrue(preserves_nets(view, fast_layout(view, component_labels(view))))

    def test_short_names_do_not_change_explicit_local_file_resolution(self):
        with TemporaryDirectory() as directory:
            entry = Path(directory) / "main.ckt"
            local = entry.with_name("74HC00.ckt")
            local.write_text("circuit Local(a,b) { R1(1k) between(a,b); }", encoding="utf-8")
            self.assertEqual(include_path("74HC00", entry), CHIP_LIBRARY / "logic/74HC00.ckt")
            self.assertEqual(include_path("74HC00.ckt", entry), local)
            files, _ = read_includes('include("74HC00.ckt");', entry)
            self.assertIn("circuit Local", files[local])

    def test_missing_and_ambiguous_chip_names_report_the_include_location(self):
        entry = Path.cwd() / "main.ckt"
        with self.assertRaisesRegex(CircuitError, 'Unknown chip "NotAChip"') as failure:
            compile_circuit('# header\ninclude("NotAChip");', path=entry)
        self.assertEqual((failure.exception.path, failure.exception.line), (entry, 2))
        with TemporaryDirectory() as directory, patch("circuitlab.language.linked.CHIP_LIBRARY", Path(directory)):
            for category in ("logic", "timers"):
                target = Path(directory) / category / "Demo.ckt"
                target.parent.mkdir()
                target.write_text("circuit Demo(a,b) { R1(1k) between(a,b); }", encoding="utf-8")
            with self.assertRaisesRegex(CircuitError, 'Ambiguous chip name "Demo"') as failure:
                compile_circuit('include("Demo");', path=entry)
            self.assertEqual((failure.exception.path, failure.exception.line), (entry, 1))
            self.assertIn("@chips/logic/Demo.ckt", failure.exception.message)
            self.assertEqual(include_path("@chips/logic/Demo.ckt"), Path(directory) / "logic/Demo.ckt")

    def test_short_imports_preserve_cycle_and_library_boundary_checks(self):
        with TemporaryDirectory() as directory, patch("circuitlab.language.linked.CHIP_LIBRARY", Path(directory)):
            (Path(directory) / "Demo.ckt").write_text('include("Demo");', encoding="utf-8")
            with self.assertRaisesRegex(CircuitError, "Include cycle"):
                compile_circuit('include("Demo");')
            with self.assertRaisesRegex(CircuitError, "inside the chip library"):
                compile_circuit('include("@chips/../outside.ckt");')
        with self.assertRaisesRegex(CircuitError, "Save this circuit"):
            compile_circuit('include("local.ckt");')

    def test_nand_called_from_twice_included_library(self):
        for a, b in product((0, 5), repeat=2):
            with self.subTest(a=a, b=b):
                source = f"""include("74HC00");
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
