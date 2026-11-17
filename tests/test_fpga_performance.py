"""Quartus report fixtures and corruption checks; no tools or board required."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from circuitlab.tooling import fpga
from circuitlab.tooling import fpga_performance as performance


FIXTURES = Path(__file__).parent / "fixtures/fpga_performance"
SOURCE = """chip HYBRID01(MAX10_CLK1_50, SW0, LEDR0) {
    MAX10_CLK1_50, SW0=input; LEDR0=output; LEDR0=SW0;
}"""


class FpgaPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = fpga.prepare_project(SOURCE, Path(self.temp.name) / "builds with spaces")
        self.out = self.project.directory / "build/output_files"
        self.out.mkdir()
        for fixture in FIXTURES.iterdir():
            if fixture.suffix in (".summary", ".rpt"):
                (self.out / fixture.name).write_bytes(fixture.read_bytes())

    def report(self, **kwargs):
        return performance.read_report(self.project, source_path="test.ckt", **kwargs)

    def paths(self, *, slack="14.706", delay="5.223", end=True):
        performance.report_command(self.project, Path(self.temp.name) / "Quartus tools")
        rows = ["OOC_TIMING_PATHS\t1", "SOURCE\t" + self.project.source_sha256,
                f"PATH\tslow 1200mV 85C\tTone_1_phase[0]\tTone_1_phase[28]\t{slack}\t{delay}\t1\t3.338\t1.885"]
        if end:
            rows.append("END")
        path = self.project.directory / performance.PATH_FILE
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_real_report_formats_use_worst_corner_and_resource_capacities(self):
        result = self.report(verified=True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["metrics"], {
            "fmax_mhz": 188.89, "clock_mhz": 50.0, "setup_slack_ns": 14.706,
            "hold_slack_ns": 0.148, "pulse_slack_ns": 9.325,
        })
        self.assertEqual(result["resources"]["logic_elements"], {"used": 111, "total": 49760})
        self.assertEqual(result["resources"]["registers"], {"used": 60, "total": 49760})
        self.assertEqual(result["resources"]["memory_bits"], {"used": 0, "total": 1677312})
        self.assertEqual(result["device"], fpga.DEVICE)
        self.assertTrue(result["quartus_version"].startswith("25.1std.0"))
        self.assertEqual(len(result["fmax_corners"]), 2)
        self.assertEqual(len(result["timing_checks"]), 9)
        self.assertEqual(result["clocks"][0]["period_ns"], 20.0)
        self.assertEqual(result["source_sha256"], self.project.source_sha256)
        json.dumps(result, allow_nan=False)

    def test_negative_timing_is_visible_without_claiming_a_verified_build(self):
        path = self.out / "HYBRID01.sta.summary"
        path.write_text(path.read_text().replace("14.706", "-0.250"))
        result = self.report(verification_error="Timing failed")
        self.assertEqual(result["metrics"]["setup_slack_ns"], -0.25)
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification_error"], "Timing failed")
        self.assertEqual(result["resources"]["logic_elements"]["used"], 111)

    def test_missing_reports_and_nonfinite_numbers_are_unknown(self):
        (self.out / "HYBRID01.fit.summary").unlink()
        (self.out / "HYBRID01.sta.summary").write_text("Type : Slow Model Setup 'clk'\nSlack : nan\n")
        path = self.out / "HYBRID01.sta.rpt"
        path.write_text(path.read_text().replace("188.89", "NaN").replace("208.46", "inf"))
        result = self.report()
        self.assertEqual(result["resources"]["logic_elements"], {"used": None, "total": None})
        self.assertIsNone(result["metrics"]["setup_slack_ns"])
        self.assertIsNone(result["metrics"]["fmax_mhz"])
        self.assertTrue(any("unavailable" in note for note in result["notes"]))
        json.dumps(result, allow_nan=False)

    def test_restricted_fmax_is_used_and_never_replaced_with_unrestricted(self):
        path = self.out / "HYBRID01.sta.rpt"
        path.write_text("; Slow Model Fmax Summary ;\n; Fmax ; Restricted Fmax ; Clock Name ; Note ;\n"
                        "; 999.00 MHz ; 100.00 MHz ; clk ; ;\n")
        self.assertEqual(self.report()["metrics"]["fmax_mhz"], 100.0)
        path.write_text(path.read_text().replace("100.00", "N/A"))
        self.assertIsNone(self.report()["metrics"]["fmax_mhz"])

    def test_stale_files_or_modified_generated_inputs_cannot_masquerade_as_current(self):
        path = self.out / "HYBRID01.sta.summary"
        os.utime(path, ns=(1, 1))
        result = self.report(verified=True)
        self.assertIsNone(result["metrics"]["setup_slack_ns"])
        self.assertFalse(result["verified"])
        self.assertTrue(any("stale" in note for note in result["notes"]))
        hdl = self.project.directory / "build/HYBRID01.v"
        hdl.write_text(hdl.read_text() + "// changed\n")
        result = self.report(verified=True)
        self.assertFalse(result["verified"])
        self.assertTrue(all(value is None for value in result["metrics"].values()))
        self.assertTrue(any("project changed" in note for note in result["notes"]))
        with self.assertRaises(ValueError):
            performance.report_command(self.project, "quartus")

    def test_manifest_and_source_mismatch_are_reported_without_exception(self):
        manifest = self.project.directory / "ooc-build.json"
        for contents in ("bad json", "{}", '{"source_sha256":"old"}'):
            manifest.write_text(contents)
            with self.subTest(contents=contents):
                self.assertFalse(self.report(verified=True)["verified"])
                self.assertTrue(self.report()["notes"])

    def test_path_extraction_preserves_endpoints_and_cell_routing_breakdown(self):
        self.paths()
        result = self.report()
        path = result["paths"][0]
        self.assertEqual(path["from_node"], "Tone_1_phase[0]")
        self.assertEqual(path["to_node"], "Tone_1_phase[28]")
        self.assertEqual(path["slack_ns"], 14.706)
        self.assertEqual(path["logic_levels"], 1)
        self.assertAlmostEqual(path["logic_delay_ns"] + path["routing_delay_ns"], path["data_delay_ns"])

    def test_incomplete_stale_or_nonfinite_path_extraction_is_not_displayed(self):
        for options in ({"end": False}, {"slack": "nan"}, {"delay": "inf"}):
            self.paths(**options)
            with self.subTest(options=options):
                self.assertEqual(self.report()["paths"], [])
        path = self.paths()
        os.utime(path, ns=(1, 1))
        self.assertEqual(self.report()["paths"], [])
        path = self.paths()
        path.write_text(path.read_text().replace(self.project.source_sha256, "old-source"))
        self.assertEqual(self.report()["paths"], [])

    def test_command_uses_separate_arguments_and_cannot_program_or_change_constraints(self):
        before = fpga._input_hashes(self.project.directory)
        argv = performance.report_command(self.project, Path(self.temp.name) / "Quartus tools")
        self.assertEqual(argv[1], "-t")
        self.assertEqual(Path(argv[0]).name, "quartus_sta.exe")
        self.assertEqual(len(argv), 3)
        script = Path(argv[2]).read_text()
        self.assertIn("get_available_operating_conditions", script)
        self.assertIn("get_timing_paths -setup", script)
        self.assertNotIn("quartus_pgm", script)
        self.assertEqual(before, fpga._input_hashes(self.project.directory))
        self.assertFalse((self.project.directory / "ooc-verified.json").exists())


if __name__ == "__main__":
    unittest.main()
