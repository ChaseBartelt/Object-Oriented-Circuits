"""Display measured FPGA results and an explicitly saved comparison baseline."""
# Performance widgets display measured results and explicit baseline comparisons only.
# Missing values remain unknown, incompatible reports are not subtracted, and loading a
# saved report cannot grant the separate permission/verification needed to program hardware.

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from circuitlab.tooling.fpga import source_digest
from circuitlab.tooling.fpga_history import (
    compare_reports, load_baseline, load_latest, save_baseline, save_latest,
)


METRICS = (
    ("clock_mhz", "Requested clock", "MHz"),
    ("fmax_mhz", "Estimated Fmax (lowest reported corner)", "MHz"),
    ("setup_slack_ns", "Worst setup slack", "ns"),
    ("hold_slack_ns", "Worst hold slack", "ns"),
    ("pulse_slack_ns", "Worst pulse-width slack", "ns"),
)
RESOURCES = (
    ("logic_elements", "Logic elements"), ("registers", "Registers"),
    ("pins", "Pins"), ("memory_bits", "Memory bits"),
    ("multipliers", "9-bit multiplier elements"), ("plls", "PLLs"),
)
CLOCK_ONLY = {'clock_mhz', 'fmax_mhz', 'pulse_slack_ns'}


def number(value, unit="", *, delta=False):
    if value is None:
        return "—"
    text = f"{value:+,.3f}" if delta else f"{value:,.3f}"
    return text.rstrip("0").rstrip(".") + (f" {unit}" if unit else "")


def resource(value):
    used, total = value.get("used"), value.get("total")
    if used is None:
        return "—"
    text = f"{used:,}"
    if total is not None:
        text += f" / {total:,}"
        if total:
            text += f" ({used / total:.2%})"
    return text


class PerformancePanel(QWidget):
    def __init__(self, build_dir, parent=None):
        super().__init__(parent)
        self.build_dir = Path(build_dir)
        self.report = None
        self.source_path = None
        self.source_hash = ""
        self.baseline = None
        self.building = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.summary = QLabel("Build an OOC design to see measured Quartus results.")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        self.baseline_button = QPushButton("Save as baseline")
        self.baseline_button.setToolTip("Keep this verified build for comparisons after editing and rebuilding this file.")
        self.baseline_button.clicked.connect(self.save_current_baseline)
        row.addWidget(self.baseline_button)
        layout.addLayout(row)
        self.comparison = QLabel()
        self.comparison.setTextFormat(Qt.TextFormat.PlainText)
        self.comparison.setWordWrap(True)
        layout.addWidget(self.comparison)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(len(METRICS) + len(RESOURCES), 4)
        self.table.setHorizontalHeaderLabels(("Metric", "Baseline", "Latest build", "Change"))
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)
        self.paths = QPlainTextEdit()
        self.paths.setReadOnly(True)
        self.paths.setPlaceholderText("Worst setup paths and report notes appear after a build.")
        splitter.addWidget(self.paths)
        splitter.setSizes([330, 190])
        layout.addWidget(splitter, 1)
        caveat = QLabel("Fmax is an estimate, not the running clock. Higher Fmax/slack and fewer resources can help; "
                        "a comparison does not prove equivalent behavior or latency. — means unavailable; "
                        "N/A means that clocked measurement does not apply.")
        caveat.setWordWrap(True)
        layout.addWidget(caveat)
        self.refresh()

    def set_source(self, path, source):
        path = Path(path).resolve() if path else None
        if path != self.source_path:
            self.source_path = path
            self.baseline = load_baseline(self.build_dir, path) if path else None
            self.report = load_latest(self.build_dir, path) if path else None
        self.source_hash = source_digest(source)
        self.refresh()

    def set_report(self, report):
        self.report = report
        self.building = False
        self.refresh()
        if report and report.get("verified") and self._current():
            try:
                save_latest(self.build_dir, self.source_path, report)
            except (OSError, ValueError) as error:
                self.comparison.setText(self.comparison.text() + f" Could not retain latest results: {error}")

    def _current(self):
        if self.report and self.source_path and self.report.get("source_path"):
            if Path(self.report["source_path"]).resolve() == self.source_path:
                return self.report
        return None

    # Display only comparable deltas and preserve unknown measurements as unknown.
    # A saved report describes performance, not a currently verified programming image.
    def refresh(self):
        current = self._current()
        baseline = self.baseline or {}
        comparison = compare_reports(current, baseline) if current and baseline else None
        self.baseline_button.setEnabled(bool(current and current.get("verified") and not self.building))
        if current:
            status = "Verified build" if current.get("verified") else "Build not verified for programming"
            if current.get("source_sha256") != self.source_hash:
                status += " · source changed; rebuild for current results"
            if self.building:
                status = "Building… showing previous results. " + status
            if current.get('clockless'):
                status += ' · clockless combinational'
            self.summary.setText(f"{status}\n{current.get('device') or 'Unknown device'} · "
                                 f"Quartus {current.get('quartus_version') or 'unknown'}\n"
                                 f"{current.get('captured_at', '')} · source {current.get('source_sha256', '')[:12]}")
        else:
            self.summary.setText("Building…" if self.building else "Build this OOC file to see measured Quartus results.")
        if baseline:
            text = f"Baseline: {baseline.get('captured_at', '')} · source {baseline.get('source_sha256', '')[:12]}. "
            if comparison and not comparison["compatible"]:
                text += "Changes unavailable: " + "; ".join(comparison["reasons"])
            elif comparison:
                text += "Change = latest − baseline."
            self.comparison.setText(text)
        else:
            self.comparison.setText("Save a verified build as baseline, edit your OOC, then build again to compare.")
        for row, (key, label, unit) in enumerate(METRICS):
            delta = comparison["metrics"].get(key) if comparison else None
            if current and current.get('clockless'):
                label = {'setup_slack_ns': 'Worst pin-to-pin max-delay slack',
                         'hold_slack_ns': 'Worst pin-to-pin min-delay slack'}.get(key, label)
            def measurement(report):
                value = (report or {}).get('metrics', {}).get(key)
                return 'N/A' if (report or {}).get('clockless') and key in CLOCK_ONLY and value is None else number(value, unit)
            self._row(row, label, measurement(baseline), measurement(current), number(delta, unit, delta=True))
        for row, (key, label) in enumerate(RESOURCES, len(METRICS)):
            delta = comparison["resources"].get(key) if comparison else None
            self._row(row, label, resource(baseline.get("resources", {}).get(key, {})),
                      resource((current or {}).get("resources", {}).get(key, {})), number(delta, delta=True))
        self.paths.setPlainText(self._path_text(current) if current else "")

    def _row(self, row, *values):
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            self.table.setItem(row, column, item)

    @staticmethod
    def _path_text(report):
        lines = []
        if report.get("verification_error"):
            lines.extend((report["verification_error"], ""))
        paths = report.get("paths", [])
        clockless = report.get('clockless', False)
        for index, path in enumerate(sorted(paths, key=lambda p: p.get("slack_ns") if p.get("slack_ns") is not None else float("inf"))):
            title = ('WORST PIN-TO-PIN PATH' if index == 0 else 'Pin-to-pin path') if clockless else (
                'WORST SETUP PATH' if index == 0 else 'Setup path')
            lines.extend((
                f"{title} · {path.get('corner', 'Unknown corner')}",
                f"From: {path.get('from_node') or 'Unavailable'}",
                f"To:   {path.get('to_node') or 'Unavailable'}",
                f"Slack: {number(path.get('slack_ns'), 'ns')} · Data delay: {number(path.get('data_delay_ns'), 'ns')}",
                f"Cells (incl. clock-to-Q): {number(path.get('logic_delay_ns'), 'ns')} · Routing: {number(path.get('routing_delay_ns'), 'ns')}"
                f" · Logic levels: {number(path.get('logic_levels'))}", "",
            ))
        if not paths:
            lines.append("Detailed pin-to-pin path unavailable. See report notes and the Quartus build log."
                         if clockless else "Detailed setup path unavailable. See report notes and the Quartus build log.")
        if clockless:
            lines.append('No clock is generated. Pin-to-pin delay constraints apply; clock frequency and Fmax do not.')
        lines.extend(report.get("notes", []))
        lines.extend(("", f"Reports: {report.get('build_directory', '')}"))
        return "\n".join(lines)

    # Make baseline replacement an explicit operation rather than a side effect of building.
    def save_current_baseline(self):
        current = self._current()
        if not current or not current.get("verified") or self.building:
            return
        try:
            save_baseline(self.build_dir, self.source_path, current)
            self.baseline = load_baseline(self.build_dir, self.source_path)
            self.refresh()
        except (OSError, ValueError) as error:
            self.comparison.setText(f"Could not save baseline: {error}")
