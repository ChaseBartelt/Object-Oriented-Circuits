"""Cross-file circuit connections through the compiler and exporters."""

import unittest
from pathlib import Path

from circuitlab.analysis import analyze
from circuitlab.compiler import compile_circuit
from circuitlab.graph import CircuitError
from circuitlab.layout_checks import preserves_nets
from circuitlab.ltspice import export_schematic
from circuitlab.report import component_labels
from circuitlab.routing import schematic_layout
from circuitlab.spice import export_spice


class LinkedCircuitTest(unittest.TestCase):
    def test_links_pins_by_name_and_keeps_other_nets_and_parts_local(self):
        path = Path.cwd() / "main.ckt"
        source = (
            'include("parts/load.ckt");\ninclude("parts/load.ckt");\n'
            "connector(signal, return_wire);\n"
            "V1(8) between(vcc, 0);\nR1(1k) between(vcc, signal);\n"
            "R2(2k) between(private, 0);\nR3(2k) between(return_wire, 0);"
        )
        child = (
            "connector(return_wire, signal);\n"
            "R1(3k) between(signal, private);\nR2(1k) between(private, return_wire);"
        )

        def read(included):
            self.assertEqual(included, path.parent / "parts" / "load.ckt")
            return child

        circuit = compile_circuit(source, path=path, read_source=read)
        self.assertEqual(len(circuit.components), 6)
        self.assertEqual(len({part.name for part in circuit.components}), 6)
        self.assertIn("@file1.private", circuit.nodes)
        result = analyze(circuit).network
        self.assertAlmostEqual(result.voltage[result.nodes.index("signal")], 8 * 6 / 7)
        netlist = export_spice(source, path=path, read_source=read)
        self.assertIn("signal n__file1_private 3000", netlist)
        drawing = schematic_layout(circuit, component_labels(circuit), "labeled")
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertEqual(export_schematic(circuit, drawing)[0].count("SYMBOL res"), 5)

    def test_reports_included_file_errors_and_cycles(self):
        path = Path.cwd() / "main.ckt"
        source = 'include("load.ckt");\nV1(5) between(vcc, 0);'
        for child, message in (
            ('include("main.ckt");', "Include cycle"),
            ("connector(missing);\nR1(1k) between(a, 0);", "not a net"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(CircuitError, message) as failure:
                compile_circuit(source, path=path, read_source=lambda _: child)
            self.assertEqual(failure.exception.path, path.with_name("load.ckt"))
            self.assertEqual(failure.exception.line, 1)
