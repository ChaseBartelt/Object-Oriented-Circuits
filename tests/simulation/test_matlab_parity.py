"""Compare Python results with data captured from the MATLAB application."""
# Captured MATLAB values provide an external regression oracle with explicit tolerances.
# Do not regenerate expected values from the Python solver under test.

import json
import unittest
from pathlib import Path

import numpy as np

from circuitlab.math.analysis import solve_input
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.layout.automatic import compact
from circuitlab.layout.legacy import route
from circuitlab.math.report import result_rows

HERE = Path(__file__).resolve().parents[1] / "fixtures" / "matlab"
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
REFERENCE = {
    row["id"]: row
    for row in json.loads(
        (HERE / "matlab_reference.json").read_text(encoding="utf-8-sig")
    )
}


def complex_values(pairs):
    array = np.asarray(pairs).reshape(-1, 2)
    return array[:, 0] + 1j * array[:, 1]


class MatlabParity(unittest.TestCase):
    def test_calculations_and_connectivity(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                expected = REFERENCE[case["id"]]
                if expected["error"]:
                    with self.assertRaises(CircuitError) as caught:
                        solve_input(case["source"], case["mode"])
                    if case["id"] in ("inconsistent", "underdetermined"):
                        self.assertEqual(
                            "ec:" + caught.exception.code, expected["error"]
                        )
                    continue
                result = solve_input(case["source"], case["mode"])
                self.assertEqual(result.circuit.mode, expected["mode"])
                self.check_values(result.circuit.known, expected["known"])
                self.check_values(result.calculated, expected["calculated"])
                self.check_branches(result.circuit.components, expected["branches"])
                self.check_network(result.network, expected["network"])
                self.check_network(result.dc_network, expected["dcNetwork"])
                reference = expected["probeValues"]
                if expected["legacyMeasurement"]:
                    measure = expected["legacyMeasurement"]
                    reference = [[measure["peak"], 0]]
                    if measure["phase"] != []:
                        reference.append([measure["phase"], 0])
                np.testing.assert_allclose(
                    [r[2] for r in result.readings],
                    complex_values(reference),
                    rtol=1e-9,
                    atol=1e-11,
                )
                self.assertEqual(
                    any("conflict" in d.lower() for d in result.diagnostics),
                    any("conflict" in d.lower() for d in expected["diagnostics"]),
                )
                self.assertCountEqual(
                    [row[0] for row in result_rows(result)],
                    [label.replace("_", " ") for label in expected["rowLabels"]],
                )

    def test_render_graph_and_routed_placement(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                expected = REFERENCE[case["id"]]
                if not case["render"]:
                    continue
                if expected["renderError"]:
                    with self.assertRaises(CircuitError):
                        compile_circuit(case["source"], case["mode"], render_only=True)
                    continue
                c = compile_circuit(case["source"], case["mode"], render_only=True)
                self.check_branches(c.components, expected["renderBranches"])
                routed = route(c, {e.name: [e.name] for e in c.components})
                np.testing.assert_allclose(
                    [routed.positions[e.name] for e in c.components],
                    expected["positions"],
                    rtol=1e-12,
                    atol=1e-12,
                )
                self.assertEqual(
                    set(routed.wires), {path["net"] for path in expected["paths"]}
                )
                for path in expected["paths"]:
                    name = path["net"]
                    endpoints = [
                        point for point in path["segments"] if point[0] is not None
                    ]
                    np.testing.assert_allclose(
                        np.asarray(routed.wires[name]).reshape(-1, 2),
                        endpoints,
                        rtol=1e-12,
                        atol=1e-12,
                    )
                    np.testing.assert_allclose(
                        np.asarray(routed.junctions.get(name, [])).reshape(-1, 2),
                        np.asarray(path["junctions"]).reshape(-1, 2),
                        atol=1e-12,
                    )
                drawing = compact(c) or routed
                self.assertEqual(set(drawing.positions), {e.name for e in c.components})

    def check_values(self, actual, expected):
        self.assertEqual(set(actual), set(expected))
        np.testing.assert_allclose(
            list(actual.values()),
            [complex(*expected[k]) for k in actual],
            rtol=1e-9,
            atol=1e-11,
        )

    def check_branches(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for e, m in zip(actual, expected):
            self.assertEqual(
                (e.name, e.kind, e.p, e.n, e.in_plus, e.in_minus),
                tuple(m[k] for k in ("name", "kind", "p", "n", "plus", "minus")),
            )
            np.testing.assert_allclose(
                e.value, complex(*m["value"]), rtol=1e-9, atol=1e-12
            )

    def check_network(self, actual, expected):
        if not expected:
            self.assertIsNone(actual)
            return
        self.assertEqual(actual.nodes, tuple(expected["nodes"]))
        self.assertEqual(actual.names, tuple(expected["names"]))
        for value, key in (
            (actual.voltage, "voltage"),
            (actual.branch_voltage, "branchVoltage"),
            (actual.branch_current, "current"),
            (actual.branch_power, "power"),
        ):
            np.testing.assert_allclose(
                value, complex_values(expected[key]), rtol=1e-9, atol=1e-11
            )
        if expected["frequency"] == []:
            self.assertIsNone(actual.frequency)
        else:
            self.assertAlmostEqual(actual.frequency, expected["frequency"])
        if expected["equivalent"]:
            for k, v in expected["equivalent"].items():
                if v == []:
                    self.assertIsNone(actual.equivalent[k])
                else:
                    np.testing.assert_allclose(
                        actual.equivalent[k],
                        complex_values(v)[0],
                        rtol=1e-9,
                        atol=1e-11,
                    )
