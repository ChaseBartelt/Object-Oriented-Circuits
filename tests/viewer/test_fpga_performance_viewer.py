"""FPGA reports remain read-only and never bypass programming verification."""

from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess, QSettings
from PySide6.QtWidgets import QAbstractItemView, QApplication

from circuitlab.tooling import fpga
from circuitlab.tooling.fpga_history import load_baseline
from circuitlab.viewer.fpga import FpgaDialog
from circuitlab.viewer.fpga_performance import PerformancePanel
from tests.viewer.test_fpga_viewer import FakeProcess


SOURCE = """chip HYBRID01(MAX10_CLK1_50, SW0, LEDR0) {
    MAX10_CLK1_50, SW0 = input;
    LEDR0 = output;
    LEDR0 = SW0;
}
"""


def report_for(path, source=SOURCE):
    return {
        "schema": 1, "captured_at": "2026-11-16T20:00:00+00:00",
        "source_path": str(path), "source_sha256": fpga.source_digest(source),
        "build_directory": str(Path(path).parent / "build-1"),
        "device": fpga.DEVICE, "quartus_version": "25.1std.0",
        "constraints_sha256": "b" * 64, "verified": True, "verification_error": "",
        "metrics": {"clock_mhz": 50, "fmax_mhz": 188.89, "setup_slack_ns": 14.706,
                    "hold_slack_ns": 0.148, "pulse_slack_ns": 9.325},
        "resources": {"logic_elements": {"used": 111, "total": 49760},
                      "registers": {"used": 60, "total": None},
                      "memory_bits": {"used": 0, "total": 1677312}},
        "paths": [], "notes": [],
    }


class PerformancePanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "oscillator.ckt"
        self.path.write_text(SOURCE)
        self.panel = PerformancePanel(self.root / "builds")
        self.panel.set_source(self.path, SOURCE)
        self.report = report_for(self.path)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def cell(self, row, column):
        return self.panel.table.item(row, column).text()

    def test_saved_baseline_survives_reopening_and_later_builds_show_deltas(self):
        self.panel.set_report(self.report)
        self.panel.baseline_button.click()
        saved = load_baseline(self.root / "builds", self.path)
        self.assertEqual(saved, self.report)
        self.assertIn("Baseline:", self.panel.comparison.text())
        updated_source = SOURCE + "# optimized implementation\n"
        updated = report_for(self.path, updated_source)
        updated["metrics"]["fmax_mhz"] = 200
        updated["resources"]["logic_elements"]["used"] = 100
        self.panel.set_source(self.path, updated_source)
        self.panel.set_report(updated)
        self.assertEqual(self.cell(1, 1), "188.89 MHz")
        self.assertEqual(self.cell(1, 2), "200 MHz")
        self.assertEqual(self.cell(1, 3), "+11.11 MHz")
        self.assertEqual(self.cell(5, 3), "-11")
        self.assertEqual(load_baseline(self.root / "builds", self.path), saved)
        reopened = PerformancePanel(self.root / "builds")
        try:
            reopened.set_source(self.path, updated_source)
            self.assertEqual(reopened.baseline, saved)
            self.assertEqual(reopened.report, updated)
            self.assertEqual(reopened.table.item(1, 3).text(), "+11.11 MHz")
        finally:
            reopened.close()
            reopened.deleteLater()

    def test_changed_source_is_labeled_and_other_files_do_not_inherit_results(self):
        self.panel.set_report(self.report)
        self.panel.baseline_button.click()
        self.panel.set_source(self.path, SOURCE + "# unsaved IDE change\n")
        self.assertIn("source changed", self.panel.summary.text())
        self.assertEqual(self.cell(1, 2), "188.89 MHz")
        another = self.root / "another.ckt"
        self.panel.set_source(another, SOURCE)
        self.assertIsNone(self.panel.baseline)
        self.assertIsNone(self.panel._current())
        self.assertEqual(self.cell(1, 1), "—")
        self.assertEqual(self.cell(1, 2), "—")
        self.assertEqual(self.panel.paths.toPlainText(), "")
        self.assertFalse(self.panel.baseline_button.isEnabled())
        self.panel.set_source(self.path, SOURCE)
        self.assertEqual(self.panel.baseline, self.report)

    def test_reopening_saved_results_never_enables_programming(self):
        self.panel.set_report(self.report)
        dialog = FpgaDialog(None, lambda: (self.path, SOURCE), build_dir=self.root / "builds")
        try:
            self.assertEqual(dialog.performance.report, self.report)
            self.assertFalse(dialog.ready)
            self.assertFalse(dialog.program_button.isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_incompatible_clock_or_tool_version_shows_raw_values_without_deltas(self):
        self.panel.set_report(self.report)
        self.panel.baseline_button.click()
        for key in ("quartus_version", "clock_mhz"):
            updated = deepcopy(self.report)
            if key == "clock_mhz":
                updated["metrics"][key] = 100
            else:
                updated[key] = "26.0"
            self.panel.set_report(updated)
            with self.subTest(key=key):
                self.assertIn("Changes unavailable", self.panel.comparison.text())
                self.assertEqual(self.cell(1, 2), "188.89 MHz")
                self.assertTrue(all(self.cell(row, 3) == "—" for row in range(self.panel.table.rowCount())))

    def test_missing_measurements_remain_unknown_and_widgets_are_read_only(self):
        incomplete = deepcopy(self.report)
        incomplete["metrics"]["fmax_mhz"] = None
        incomplete["resources"]["logic_elements"]["used"] = None
        long_node = "u_logic|" + "nested_stage_" * 40 + "phase[31]"
        incomplete["paths"] = [{"corner": "Slow 85C", "from_node": long_node, "to_node": "count[25]",
                               "slack_ns": 1.25, "data_delay_ns": None, "logic_delay_ns": None,
                               "routing_delay_ns": None, "logic_levels": None}]
        self.panel.set_report(incomplete)
        self.assertEqual(self.cell(1, 2), "—")
        self.assertEqual(self.cell(5, 2), "—")
        self.assertIn("Data delay: —", self.panel.paths.toPlainText())
        self.assertIn(long_node, self.panel.paths.toPlainText())
        self.assertTrue(self.panel.paths.isReadOnly())
        self.assertEqual(self.panel.table.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
        self.assertEqual(self.path.read_text(), SOURCE)

    def test_clockless_report_distinguishes_clock_metrics_from_pin_delay(self):
        clockless = deepcopy(self.report)
        clockless['clockless'] = True
        for key in ('clock_mhz', 'fmax_mhz', 'pulse_slack_ns'):
            clockless['metrics'][key] = None
        clockless['metrics'].update(setup_slack_ns=13.5, hold_slack_ns=1.25)
        clockless['resources']['registers']['used'] = 0
        clockless['paths'] = [{'from_node': 'SW0', 'to_node': 'LEDR0', 'slack_ns': 13.5}]
        self.panel.set_report(clockless)
        self.assertEqual([self.cell(row, 2) for row in (0, 1, 4)], ['N/A'] * 3)
        self.assertEqual(self.cell(2, 2), '13.5 ns')
        self.assertEqual(self.cell(3, 2), '1.25 ns')
        self.assertIn('pin-to-pin max-delay', self.cell(2, 0))
        self.assertEqual(self.cell(6, 2), '0')
        self.assertIn('clockless combinational', self.panel.summary.text())
        self.assertTrue(self.panel.paths.toPlainText().startswith('WORST PIN-TO-PIN PATH'))
        self.assertIn('Fmax do not', self.panel.paths.toPlainText())

    def test_worst_path_is_first_and_building_or_failed_reports_cannot_replace_baseline(self):
        report = deepcopy(self.report)
        report["paths"] = [
            {"corner": "Fast", "from_node": "a", "to_node": "b", "slack_ns": 4},
            {"corner": "Slow", "from_node": "c", "to_node": "d", "slack_ns": -1},
        ]
        self.panel.set_report(report)
        self.assertTrue(self.panel.paths.toPlainText().startswith("WORST SETUP PATH · Slow"))
        self.panel.building = True
        self.panel.refresh()
        self.assertFalse(self.panel.baseline_button.isEnabled())
        self.panel.save_current_baseline()
        self.assertIsNone(load_baseline(self.root / "builds", self.path))
        report.update(verified=False, verification_error="Timing failed")
        self.panel.set_report(report)
        self.assertFalse(self.panel.baseline_button.isEnabled())
        self.assertIn("Timing failed", self.panel.paths.toPlainText())
        self.panel.save_current_baseline()
        self.assertIsNone(load_baseline(self.root / "builds", self.path))


class FpgaPerformanceFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.document = [self.root / "test.ckt", SOURCE]
        self.document[0].write_text(SOURCE)
        self.bins = self.root / "quartus"
        self.bins.mkdir()
        (self.bins / "quartus_sta.exe").touch()
        self.patches = [
            patch("circuitlab.viewer.fpga.QProcess", FakeProcess),
            patch("circuitlab.viewer.fpga.QSettings", side_effect=lambda *_: QSettings(
                str(self.root / "settings.ini"), QSettings.Format.IniFormat)),
            patch("circuitlab.tooling.fpga_performance.report_command", return_value=[
                str(self.bins / "quartus_sta.exe"), "-t", "report.tcl"]),
            patch("circuitlab.tooling.fpga_performance.read_report", side_effect=self.read_report),
        ]
        for item in self.patches:
            item.start()
        self.dialog = FpgaDialog(None, lambda: tuple(self.document), build_dir=self.root / "builds")
        self.dialog._tools = Mock(return_value=self.bins)

    def tearDown(self):
        self.dialog.operation = None
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def read_report(self, project, *, source_path, verified, verification_error):
        report = report_for(source_path)
        report.update(source_sha256=project.source_sha256, verified=verified, verification_error=verification_error)
        if verification_error:
            report["metrics"]["setup_slack_ns"] = -1
        return report

    def start_reporting(self, *, verified=True):
        self.dialog.build()
        timing = self.dialog.project.directory / "build/output_files/HYBRID01.sta.rpt"
        timing.parent.mkdir(parents=True, exist_ok=True)
        timing.write_text("Report availability checked by the UI; parser is mocked in this test.")
        with patch("circuitlab.tooling.fpga.verify_build", return_value=self.root / "verified.sof",
                   side_effect=None if verified else fpga.FpgaError("Timing failed")):
            self.dialog.process.complete("Build finished\n")
        self.assertEqual(self.dialog.operation, "performance")
        self.assertFalse(self.dialog.ready)
        self.assertFalse(self.dialog.program_button.isEnabled())
        self.assertFalse(self.dialog.close_button.isEnabled())

    def test_report_success_enables_only_original_verified_build_without_programming(self):
        self.start_reporting()
        self.dialog.process.complete("Timing paths collected\n")
        self.dialog.cables.addItem("USB-Blaster [USB-0]")
        self.assertTrue(self.dialog.ready)
        self.assertTrue(self.dialog.program_button.isEnabled())
        self.assertIs(self.dialog.report_tabs.currentWidget(), self.dialog.performance)
        self.assertTrue(self.dialog.performance.report["verified"])
        self.assertEqual(len(self.dialog.process.started), 2)
        self.assertFalse(any("pgm" in executable for executable, _ in self.dialog.process.started))

    def test_failed_path_extraction_does_not_discard_verified_build_or_unlock_early(self):
        self.start_reporting()
        self.dialog.process.complete("Path extraction failed\n", code=1)
        self.dialog.cables.addItem("USB-Blaster [USB-0]")
        self.assertTrue(self.dialog.ready)
        self.assertTrue(self.dialog.program_button.isEnabled())
        self.assertIn("extraction failed", self.dialog.performance.paths.toPlainText())
        self.assertFalse(self.dialog.busy)

    def test_failed_to_start_analyzer_keeps_verified_build_and_does_not_double_finish(self):
        self.start_reporting()
        self.dialog.process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
        self.assertTrue(self.dialog.ready)
        self.assertFalse(self.dialog.busy)
        self.assertIn("Could not start Quartus", self.dialog.performance.paths.toPlainText())
        report = self.dialog.performance.report
        self.dialog.process.complete(code=1)
        self.assertIs(self.dialog.performance.report, report)

    def test_timing_failure_still_shows_results_but_never_allows_programming_or_baseline(self):
        self.start_reporting(verified=False)
        self.dialog.process.complete("Timing paths collected\n")
        self.dialog.cables.addItem("USB-Blaster [USB-0]")
        self.assertFalse(self.dialog.ready)
        self.assertFalse(self.dialog.program_button.isEnabled())
        self.assertFalse(self.dialog.performance.baseline_button.isEnabled())
        self.assertEqual(self.dialog.performance.table.item(2, 2).text(), "-1 ns")
        self.assertIn("Timing failed", self.dialog.performance.paths.toPlainText())
        operations = len(self.dialog.process.started)
        self.dialog.program()
        self.assertEqual(len(self.dialog.process.started), operations)

    def test_source_change_during_reporting_keeps_results_stale_and_programming_disabled(self):
        self.start_reporting()
        self.document[1] += "# changed during report extraction\n"
        self.dialog.process.complete()
        self.dialog.cables.addItem("USB-Blaster [USB-0]")
        self.assertFalse(self.dialog.ready)
        self.assertFalse(self.dialog.program_button.isEnabled())
        self.assertIn("source changed", self.dialog.performance.summary.text())
        self.assertIn("Build again", self.dialog.status.text())
        self.assertEqual(self.document[0].read_text(), SOURCE)


if __name__ == "__main__":
    unittest.main()
