"""Keep theoretical AC impedances out of physical SPICE export formats."""
# A theoretical Z value has no automatic physical SPICE equivalent. Verify that every export
# path rejects it explicitly, including a device hidden inside a collapsed package.

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from circuitlab.exporters.ltspice import export_hierarchy, export_schematic
from circuitlab.exporters.spice import export_spice, spice_circuit, spice_netlist
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.routing import schematic_layout
from circuitlab.math.report import component_labels
from circuitlab.model.graph import Circuit, CircuitError, Component


class ImpedanceExportTest(unittest.TestCase):
    def test_unspecified_parts_cannot_export_as_zero_or_missing_branches(self):
        for kind in 'RCLVIZ':
            source = f'# unfinished value\n{kind}1 between(a,0);'
            circuit = compile_circuit(source)
            for exporter in (
                lambda: export_spice(source),
                lambda: export_schematic(circuit, None),
                lambda: export_hierarchy(circuit, None),
            ):
                with self.subTest(kind=kind), self.assertRaises(CircuitError) as raised:
                    exporter()
                self.assertEqual(raised.exception.code, 'MissingComponentValue')
                self.assertIn(f'{kind}1', raised.exception.message)
                self.assertEqual(raised.exception.line, 2)

    def assert_impedance_error(self, action):
        with self.assertRaises(CircuitError) as raised:
            action()
        error = raised.exception
        self.assertEqual(error.code, 'UnsupportedImpedanceExport')
        self.assertIn('theoretical AC impedance', error.message)
        self.assertIn('SPICE/LTspice', error.message)
        self.assertIn('R/L/C', error.message)
        return error

    def test_all_z_values_require_an_explicit_export_model(self):
        for value in (2 - 2j, 2 + 2j, 2j, -2j, 4, -4):
            circuit = Circuit('ac', known={'f': 1000}, components=[
                Component('V1', 'V', 'in', '0', 1),
                Component('Z1', 'Z', 'in', '0', value),
            ], component_lines={'Z1': 7})
            # Rejection precedes drawing, so no invalid partial sheet can escape.
            exporters = (
                lambda: spice_circuit(circuit),
                lambda: spice_netlist(circuit),
                lambda: export_schematic(circuit, None),
                lambda: export_hierarchy(circuit, None),
            )
            for index, exporter in enumerate(exporters):
                with self.subTest(value=value, exporter=index):
                    self.assertEqual(self.assert_impedance_error(exporter).line, 7)

    def test_both_source_spellings_reject_z_without_dropping_its_reactance(self):
        for declaration in ('Zload(2-j2) between(in,0);', 'Zload in 0 2-j2'):
            source = 'f=1k\nV1(1 Vrms) between(in,0);\n' + declaration
            with self.subTest(declaration=declaration):
                circuit = compile_circuit(source)
                impedance = next(part for part in circuit.components if part.kind == 'Z')
                self.assertEqual(impedance.value, 2 - 2j)
                error = self.assert_impedance_error(lambda: export_spice(source))
                self.assertIn('Zload', error.message)

    def test_collapsed_nested_chip_and_included_definition_are_validated(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            library = folder / 'load.ckt'
            library.write_text(
                'circuit branch(a,b) {\n  Z1(2-j2) between(a,b);\n}\n'
                'chip load(a,b) { branch(a,b); }\n', encoding='utf-8',
            )
            path = folder / 'main.ckt'
            source = ('include("load.ckt");\nf=1k\n'
                      'V1(1 Vrms) between(in,0);\nmain { load(in,0); }\n')
            path.write_text(source, encoding='utf-8')
            circuit = compile_circuit(source, path=path)
            top = next(hood_levels(circuit, set()))[1]
            self.assertFalse(any(part.kind == 'Z' for part in top.components))
            self.assertTrue(any(part.kind == 'Z' for part in circuit.components))
            drawing = schematic_layout(top, component_labels(top), 'auto')
            for exporter in (
                lambda: export_spice(source, path=path),
                lambda: export_schematic(circuit, drawing),
                lambda: export_hierarchy(circuit, drawing),
            ):
                error = self.assert_impedance_error(exporter)
                self.assertEqual(error.path, library.resolve())
                self.assertEqual(error.line, 2)

    def test_grouped_impedance_cannot_be_exported_as_a_resistor(self):
        source = 'f=1k\nV=2\n(series)\nZ1=2-j2\nR1=1k\n(end series)\n'
        self.assert_impedance_error(lambda: export_spice(source))

    def test_explicit_rlc_model_still_exports_to_both_formats(self):
        source = ('f=1k\nV1(1 Vrms) between(in,0);\n'
                  'R1(1k) between(in,middle);\nL1(10m) between(middle,out);\n'
                  'C1(1u) between(out,0);\n')
        circuit = compile_circuit(source)
        netlist = export_spice(source)
        self.assertIn('R1 in middle 1000\n', netlist)
        self.assertIn('L1 middle out 0.01 Rser=0\n', netlist)
        self.assertIn('C1 out 0 1e-06\n', netlist)
        self.assertIn('.ac list 1000\n', netlist)
        drawing = schematic_layout(circuit, component_labels(circuit), 'auto')
        sheet, _ = export_schematic(circuit, drawing)
        hierarchical, companions, _ = export_hierarchy(circuit, drawing)
        for exported in (sheet, hierarchical):
            self.assertIn('SYMBOL res ', exported)
            self.assertIn('SYMBOL ind ', exported)
            self.assertIn('SYMBOL cap ', exported)
            self.assertIn('!.ac list 1000', exported)
        self.assertFalse(companions)

