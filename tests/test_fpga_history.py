"""Performance comparisons preserve a user-selected baseline across builds."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from circuitlab.tooling import fpga_history as history


class FpgaHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "my synth.ckt"
        self.report = {
            "schema": 1, "captured_at": "2026-11-16T20:00:00+00:00",
            "source_path": str(self.source), "source_sha256": "a" * 64,
            "build_directory": str(self.root / "build-1"),
            "device": "10M50DAF484C7G", "quartus_version": "25.1std.0",
            "constraints_sha256": "b" * 64, "verified": True, "verification_error": "",
            "metrics": {"clock_mhz": 50.0, "fmax_mhz": 188.89, "setup_slack_ns": 14.706,
                        "hold_slack_ns": 0.148, "pulse_slack_ns": 9.325},
            "resources": {"logic_elements": {"used": 111, "total": 49760},
                          "registers": {"used": 60, "total": None},
                          "memory_bits": {"used": 0, "total": 1677312}},
            "paths": [], "notes": [],
        }

    def test_round_trip_is_per_source_and_survives_later_build_objects(self):
        self.assertIsNone(history.load_baseline(self.root, self.source))
        history.save_baseline(self.root, self.source, self.report)
        other_source = self.root / "elsewhere" / "my synth.ckt"
        other = {**self.report, "source_path": str(other_source), "source_sha256": "c" * 64}
        history.save_baseline(self.root, other_source, other)
        current = deepcopy(self.report)
        current["source_sha256"] = "d" * 64
        current["resources"]["logic_elements"]["used"] = 90
        history.compare_reports(current, history.load_baseline(self.root, self.source))
        self.assertEqual(history.load_baseline(self.root, self.source), self.report)
        self.assertEqual(history.load_baseline(self.root, other_source), other)
        self.assertEqual(len(list((self.root / "performance-baselines").glob("*.json"))), 2)
        equivalent = self.root / "unused" / ".." / "my synth.ckt"
        self.assertEqual(history.load_baseline(self.root, equivalent), self.report)

    def test_unverified_wrong_source_and_empty_measurements_do_not_replace_baseline(self):
        history.save_baseline(self.root, self.source, self.report)
        bad = [
            {**self.report, "verified": False},
            {**self.report, "source_path": str(self.root / "different.ckt")},
            {**self.report, "metrics": {"clock_mhz": 50}, "resources": {}},
            {**self.report, "constraints_sha256": None},
            {**self.report, "metrics": {"fmax_mhz": float("inf")}},
            {**self.report, "metrics": {"fmax_mhz": True}},
        ]
        for report in bad:
            with self.subTest(report=report), self.assertRaises(ValueError):
                history.save_baseline(self.root, self.source, report)
            self.assertEqual(history.load_baseline(self.root, self.source), self.report)

    def test_damaged_and_oversized_snapshots_are_ignored(self):
        history.save_baseline(self.root, self.source, self.report)
        file = next((self.root / "performance-baselines").glob("*.json"))
        for data in (b"{", b"[]", b"null", b"x" * (history.MAX_BASELINE_BYTES + 1),
                     json.dumps({**self.report, "metrics": {"fmax_mhz": float("nan")}}).encode(),
                     json.dumps({**self.report, "resources": []}).encode()):
            with self.subTest(data=data[:30]):
                file.write_bytes(data)
                self.assertIsNone(history.load_baseline(self.root, self.source))

    def test_changed_source_has_real_deltas_and_missing_values_stay_missing(self):
        current = deepcopy(self.report)
        current["source_sha256"] = "c" * 64
        current["metrics"].update(fmax_mhz=200, setup_slack_ns=15.0, hold_slack_ns=None)
        current["resources"]["logic_elements"]["used"] = 100
        current["resources"]["registers"]["used"] = 65
        result = history.compare_reports(current, self.report)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["reasons"], [])
        self.assertAlmostEqual(result["metrics"]["fmax_mhz"], 11.11)
        self.assertAlmostEqual(result["metrics"]["setup_slack_ns"], .294)
        self.assertEqual(result["metrics"]["clock_mhz"], 0)
        self.assertIsNone(result["metrics"]["hold_slack_ns"])
        self.assertEqual(result["resources"]["logic_elements"], -11)
        self.assertEqual(result["resources"]["registers"], 5)
        self.assertEqual(result["resources"]["memory_bits"], 0)
        self.assertIsNone(result["resources"]["plls"])

    def test_malformed_path_fields_are_rejected_when_restoring_latest(self):
        history.save_latest(self.root, self.source, self.report)
        file = next((self.root / "performance-latest").glob("*.json"))
        malformed = [
            {**self.report, "paths": [{"slack_ns": "invalid"}]},
            {**self.report, "paths": [{"data_delay_ns": []}]},
            {**self.report, "paths": [{"logic_levels": True}]},
            {**self.report, "paths": [{"from_node": {}}]},
            {**self.report, "verification_error": ["bad value"]},
        ]
        for report in malformed:
            with self.subTest(report=report):
                file.write_text(json.dumps(report), encoding="utf-8")
                self.assertIsNone(history.load_latest(self.root, self.source))
                with self.assertRaises(ValueError):
                    history.save_latest(self.root, self.source, report)

    def test_changed_build_settings_disable_deltas_instead_of_claiming_speedup(self):
        mutations = (
            ("device", "different"), ("quartus_version", "26.0"),
            ("constraints_sha256", "c" * 64), ("source_path", str(self.root / "different.ckt")),
            ("quartus_version", None), ("metrics", {"clock_mhz": 100}),
            ("metrics", {"clock_mhz": None}), ("metrics", []),
        )
        for key, value in mutations:
            result = history.compare_reports({**self.report, key: value}, self.report)
            with self.subTest(key=key, value=value):
                self.assertFalse(result["compatible"])
                self.assertTrue(result["reasons"])
                self.assertTrue(all(value is None for value in result["metrics"].values()))
                self.assertTrue(all(value is None for value in result["resources"].values()))

    def test_failed_timing_can_be_compared_but_cannot_be_saved_as_verified(self):
        current = deepcopy(self.report)
        current.update(verified=False, verification_error="Timing failed")
        current["metrics"]["setup_slack_ns"] = -1
        result = history.compare_reports(current, self.report)
        self.assertTrue(result["compatible"])
        self.assertAlmostEqual(result["metrics"]["setup_slack_ns"], -15.706)
        with self.assertRaises(ValueError):
            history.save_baseline(self.root, self.source, current)

    def test_nonfinite_and_out_of_range_values_never_become_numeric_deltas(self):
        current = deepcopy(self.report)
        current["metrics"].update(fmax_mhz=float("nan"), hold_slack_ns=10 ** 400)
        current["resources"]["registers"]["used"] = float("inf")
        result = history.compare_reports(current, self.report)
        self.assertTrue(result["compatible"])
        self.assertIsNone(result["metrics"]["fmax_mhz"])
        self.assertIsNone(result["metrics"]["hold_slack_ns"])
        self.assertIsNone(result["resources"]["registers"])

    def test_failed_atomic_replace_keeps_previous_snapshot(self):
        history.save_baseline(self.root, self.source, self.report)
        updated = {**self.report, "source_sha256": "c" * 64}
        with patch.object(history.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                history.save_baseline(self.root, self.source, updated)
        self.assertEqual(history.load_baseline(self.root, self.source), self.report)
        self.assertFalse(list((self.root / "performance-baselines").glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
