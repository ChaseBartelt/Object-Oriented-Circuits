"""Compare the reorganized implementation with immutable pre-refactor behavior.

These fixtures capture the audited implementation, not manufacturer accuracy.
Only source-root paths are normalized; electrical names, values, scopes and
pin ordering must agree exactly. Numerical outputs use the existing MATLAB
parity suite's tolerances (rtol=1e-9, atol=1e-11).
"""

from dataclasses import asdict
import json
from pathlib import Path
import unittest

import numpy as np

# Keep these public imports: existing integrations must remain compatible.
from circuitlab.analysis import analyze
from circuitlab.compiler import compile_circuit
from circuitlab.hood import hood_levels
from circuitlab.layout_checks import preserves_nets
from circuitlab.linked import CHIP_LIBRARY
from circuitlab.placement import pack_layout, read_placement, restore_layout, write_placement
from circuitlab.spice import spice_netlist


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "refactor"
EXPECTED = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))


def encode(value):
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, Path):
        if value.is_relative_to(FIXTURES):
            return "examples/" + value.relative_to(FIXTURES).as_posix()
        if value.is_relative_to(CHIP_LIBRARY):
            return "circuitlab/libraries/chips/" + value.relative_to(CHIP_LIBRARY).as_posix()
        raise AssertionError(f"Unexpected source origin: {value}")
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(type(value).__name__)


def serializable(value):
    return json.loads(json.dumps(value, default=encode))


def graph_record(circuit):
    return serializable({
        "mode": circuit.mode,
        "grounds": sorted(circuit.grounds),
        "nodes": list(circuit.nodes),
        "known": circuit.known,
        "parts": [asdict(part) for part in circuit.components],
        "terminal_nets": {part.name: part.terminal_nets for part in circuit.components},
        "instances": [asdict(instance) for instance in circuit.instances],
        "owners": circuit.owners,
        "component_lines": circuit.component_lines,
        "component_files": circuit.component_files,
        "connector_pins": circuit.connector_pins,
    })


def compile_fixture(name, *, render_only=False):
    path = FIXTURES / f"{name}.ckt"
    return compile_circuit(path.read_text(encoding="utf-8-sig"), path=path, render_only=render_only)


class RefactorContractsTest(unittest.TestCase):
    def test_compiler_preserves_electrical_graph_and_source_scopes(self):
        for name, expected in EXPECTED["cases"].items():
            for render_only in (False, True):
                with self.subTest(circuit=name, render_only=render_only):
                    actual = graph_record(compile_fixture(name, render_only=render_only))
                    self.assertEqual(actual, expected["graph"])

    def test_spice_export_preserves_names_values_and_terminal_order(self):
        for name in EXPECTED["cases"]:
            with self.subTest(circuit=name):
                expected = (FIXTURES / f"{name}.cir").read_text(encoding="ascii")
                self.assertEqual(spice_netlist(compile_fixture(name, render_only=True)), expected)

    def test_calculations_preserve_the_audited_operating_points(self):
        for name, expected in EXPECTED["cases"].items():
            with self.subTest(circuit=name):
                result = analyze(compile_fixture(name))
                actual = serializable({
                    "calculated": result.calculated,
                    "readings": result.readings,
                    "diagnostics": result.diagnostics,
                    "network": asdict(result.network) if result.network else None,
                    "dc_network": asdict(result.dc_network) if result.dc_network else None,
                })
                self.assert_numerically_equal(actual, expected["solution"])

    def assert_numerically_equal(self, actual, expected):
        if isinstance(expected, dict):
            self.assertEqual(actual.keys(), expected.keys())
            for key in expected:
                self.assert_numerically_equal(actual[key], expected[key])
        elif isinstance(expected, list):
            self.assertEqual(len(actual), len(expected))
            for value, reference in zip(actual, expected):
                self.assert_numerically_equal(value, reference)
        elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
            np.testing.assert_allclose(actual, expected, rtol=1e-9, atol=1e-11)
        else:
            self.assertEqual(actual, expected)

    def test_saved_v1_layouts_preserve_geometry_at_each_expanded_level(self):
        path = FIXTURES / "timer555.ckt"
        mode, records = read_placement(path)
        self.assertEqual(mode, EXPECTED["saved_layout"]["mode"])
        expected_records = {
            (level, style): geometry
            for level, style, geometry in EXPECTED["saved_layout"]["restored"]
        }
        self.assertEqual(records.keys(), expected_records.keys())
        circuit = compile_fixture("timer555", render_only=True)
        expanded = {level for level, _ in records if level is not None}
        levels = {
            level: (view, boundary)
            for level, view, _, boundary in hood_levels(circuit, expanded)
        }
        packed = {}
        for key, record in records.items():
            with self.subTest(level=key[0], style=key[1]):
                view, boundary = levels[key[0]]
                layout = restore_layout(record, view, boundary)
                self.assertIsNotNone(layout)
                self.assertTrue(preserves_nets(view, layout))
                packed[key] = pack_layout(layout)
                self.assertEqual(serializable(packed[key]), expected_records[key])

        saved = json.loads(write_placement(mode, packed))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["mode"], mode)
        for level, style, record in saved["layouts"]:
            view, boundary = levels[level]
            layout = restore_layout(record, view, boundary)
            self.assertIsNotNone(layout)
            self.assertTrue(preserves_nets(view, layout))
            self.assertEqual(serializable(pack_layout(layout)), expected_records[level, style])
