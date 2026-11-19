"""Build OOC and configure a DE10-Lite using Quartus's background tools."""
# The dialog sequences asynchronous build, report, chain-scan, and programming processes.
# A successful report or cached image is not sufficient to enable programming: the verified
# build must still match the current source and selected hardware when the user requests it.

from pathlib import Path
import re

from PySide6.QtCore import QProcess, QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QTabWidget, QVBoxLayout,
)

from circuitlab.model.graph import CircuitError
from circuitlab.tooling import fpga
from circuitlab.tooling import fpga_performance
from circuitlab.viewer.fpga_performance import PerformancePanel
from circuitlab.viewer.widgets import button


# Only one external process sequence runs at a time. Source changes invalidate the image
# while asynchronous results are in flight, preventing an older build from becoming current.
class FpgaDialog(QDialog):
    """One process at a time; programming requires an explicit button click."""

    def __init__(self, parent, current_document, *, build_dir=None):
        super().__init__(parent)
        self.current_document = current_document
        self.build_dir = Path(build_dir) if build_dir else (
            Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
            / "OOC" / "fpga" / "de10_lite"
        )
        self.project = None
        self.built_path = None
        self.ready = False
        self._build_image = None
        self._build_error = ""
        self._build_clocked = True
        self.operation = None
        self.output = bytearray()
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.setWindowTitle("FPGA — DE10-Lite / HYBRID01")
        self.resize(1020, 800)
        layout = QVBoxLayout(self)
        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.source_label = QLabel()
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel("Quartus bin folder"))
        self.settings = QSettings("Electric Circuits", "Object-Oriented Circuits")
        self.quartus = QLineEdit(self.settings.value("fpga/quartus_bin", "", type=str))
        self.quartus.setPlaceholderText("Auto-detect installed Quartus")
        tool_row.addWidget(self.quartus, 1)
        self.browse_button = button("Browse…", self._browse, tool_row)
        layout.addLayout(tool_row)
        actions = QHBoxLayout()
        self.build_button = button("Build from OOC", self.build, actions)
        self.refresh_button = button("Refresh cables", self.refresh_cables, actions)
        self.cables = QComboBox()
        self.cables.setMinimumContentsLength(20)
        self.cables.currentIndexChanged.connect(self._update_controls)
        actions.addWidget(self.cables, 1)
        self.program_button = button("Program FPGA", self.program, actions)
        layout.addLayout(actions)
        self.status = QLabel("Build your saved HYBRID01 OOC file to begin.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.report_tabs = QTabWidget()
        self.performance = PerformancePanel(self.build_dir)
        self.report_tabs.addTab(self.performance, "Performance")
        self.report_tabs.addTab(self.log, "Build log")
        layout.addWidget(self.report_tabs, 1)
        footer = QHBoxLayout()
        self.folder_button = button("Open build folder", self._open_folder, footer)
        footer.addStretch()
        self.close_button = button("Close", self.close, footer)
        layout.addLayout(footer)
        for control in self.findChildren(QPushButton):
            control.setStyleSheet("QPushButton:disabled { color: #888; background: #303030; border-color: #444; }")
        # A failed scan for an old installation must not keep its cable choices.
        self.quartus.textChanged.connect(self.cables.clear)
        self.refresh_source()

    @property
    def busy(self):
        return self.operation is not None

    def refresh_source(self):
        path, source = self.current_document()
        mode = 'Declare MAX10_CLK1_50 for synchronized switches, or omit it for clockless combinational logic.'
        try:
            _, _, constraints = fpga.compile_de10_lite(source)
            mode = ('50 MHz · switch inputs synchronized across two clock edges.'
                    if re.search(r'(?m)^create_clock\b', constraints) else
                    'Clockless combinational · switches drive logic directly; no synchronizers.')
        except (ValueError, CircuitError):
            pass
        self.description.setText(
            "Write and save OOC in your IDE. Build here, select the board's USB-Blaster, "
            "then program the FPGA. Quartus runs in the background.\n"
            "DE10-Lite · SW0–SW9 · LEDR0–LEDR9 · HEX0–HEX5 active-low displays · optional audio on JP1 pin 1. " + mode + "\n"
            "Programming lasts until power-off; it does not write flash.")
        self.source_label.setText(f"Source: {path or 'No OOC file loaded'}")
        self.performance.set_source(path, source)
        if self.ready and (path != self.built_path or not self.project.matches_source(source)):
            self.ready = False
            if not self.busy:
                self.status.setText("OOC changed. Build again before programming.")
        self._update_controls()

    def _update_controls(self, *_):
        idle = not self.busy
        self.build_button.setEnabled(idle)
        self.refresh_button.setEnabled(idle)
        self.program_button.setEnabled(idle and self.ready and self.cables.currentIndex() >= 0)
        for control in (self.quartus, self.browse_button, self.cables, self.close_button):
            control.setEnabled(idle)
        self.folder_button.setEnabled(self.project is not None)
        self.progress.setVisible(not idle)
        self.performance.building = self.operation in ("build", "performance")
        self.performance.refresh()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Quartus bin64 folder", self.quartus.text())
        if folder:
            self.quartus.setText(folder)

    def _tools(self):
        folder = fpga.discover_quartus(self.quartus.text().strip())
        self.settings.setValue("fpga/quartus_bin", str(folder))
        return folder

    def _open_folder(self):
        if self.project:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.directory)))

    def build(self):
        if self.busy:
            return
        self.ready = False
        self._build_image = None
        self._build_error = ""
        self._update_controls()
        try:
            path, source = self.current_document()
            if path is None:
                raise ValueError("Open and save your HYBRID01 OOC file in the IDE first.")
            self.tool_bin = self._tools()
            self.project = fpga.prepare_project(source, self.build_dir)
            constraints = (self.project.directory / 'HYBRID01.sdc').read_text(encoding='utf-8')
            self._build_clocked = bool(re.search(r'(?m)^create_clock\b', constraints))
            self.built_path = path
            self.log.clear()
            self.report_tabs.setCurrentWidget(self.log)
            self.log.appendPlainText(f"Building OOC: {path}\nBuild folder: {self.project.directory}\n")
            self._start("build", fpga.build_command(self.project, self.tool_bin),
                        "Building for DE10-Lite… Quartus is compiling and checking timing.")
        except (OSError, ValueError, CircuitError) as error:
            self._fail(str(error))

    def refresh_cables(self):
        if self.busy:
            return
        self.cables.clear()
        try:
            self.tool_bin = self._tools()
            self._start("cables", fpga.cable_list_command(self.tool_bin), "Looking for USB-Blaster cables…")
        except (OSError, ValueError) as error:
            self._fail(str(error))

    def program(self):
        if self.busy:
            return
        self.refresh_source()
        if not self.ready or self.cables.currentIndex() < 0:
            return
        try:
            self.tool_bin = self._tools()
            # Validate the current source and image before scanning the selected chain.
            fpga.program_command(self.project, self.tool_bin, self.cables.currentText(),
                                 source=self.current_document()[1])
            self.selected_cable = self.cables.currentText()
            self._start("chain", fpga.chain_command(self.tool_bin), "Checking the selected JTAG chain…")
        except (OSError, ValueError, CircuitError) as error:
            self._fail(str(error))

    def _start(self, operation, command, message):
        self.operation = operation
        self.output.clear()
        self.status.setText(message)
        self.log.appendPlainText("\n" + message)
        self.process.setWorkingDirectory(str(self.project.directory if self.project else Path.cwd()))
        self._update_controls()
        self.process.start(str(command[0]), [str(arg) for arg in command[1:]])

    def _read_output(self):
        data = bytes(self.process.readAllStandardOutput())
        self.output.extend(data)
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText(data.decode("utf-8", errors="replace"))

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            message = "Could not start Quartus: " + self.process.errorString()
            if self.operation == "performance":
                self.operation = None
                self._complete_performance(message)
            else:
                self._fail(message)

    def _finished(self, exit_code, exit_status):
        self._read_output()
        operation, self.operation = self.operation, None
        if operation is None:
            return
        output = self.output.decode("utf-8", errors="replace")
        if operation == "build":
            self._build_finished(exit_code, exit_status)
            return
        if operation == "performance":
            error = ""
            if exit_code or exit_status != QProcess.ExitStatus.NormalExit:
                error = f"Detailed timing-path extraction failed (exit {exit_code}); see Build log."
            self._complete_performance(error)
            return
        try:
            if exit_code or exit_status != QProcess.ExitStatus.NormalExit:
                raise ValueError(f"Quartus {operation} failed (exit {exit_code}). Read the log below.")
            if operation == "cables":
                self.cables.addItems(fpga.parse_cables(output))
                self.status.setText("Select the USB-Blaster connected to your DE10-Lite.")
            elif operation == "chain":
                fpga.verify_chain(output, self.selected_cable)
                self.refresh_source()
                if not self.ready:
                    raise ValueError("OOC changed. Build again before programming.")
                command = fpga.program_command(self.project, self.tool_bin, self.selected_cable,
                                               source=self.current_document()[1])
                self._start("program", command, "Programming FPGA volatile memory…")
                return
            elif operation == "program":
                fpga.programmer_success(output)
                self.status.setText("FPGA programmed successfully. The design runs until power-off or reprogramming.")
                self.refresh_source()
        except (OSError, ValueError, CircuitError) as error:
            self._fail(str(error))
        self._update_controls()

    def _build_finished(self, exit_code, exit_status):
        self._build_image = None
        self._build_error = ""
        try:
            if exit_code or exit_status != QProcess.ExitStatus.NormalExit:
                raise ValueError(f"Quartus build failed (exit {exit_code}). Read the Build log.")
            self._build_image = fpga.verify_build(self.project)
        except (OSError, ValueError, CircuitError) as error:
            self._build_error = str(error)
            self.log.appendPlainText("\n" + self._build_error)
        # Reporting is useful even when timing fails. It never grants permission
        # to program; only the existing build verification above can do that.
        timing = self.project.directory / "build/output_files/HYBRID01.sta.rpt"
        analyzer = self.tool_bin / "quartus_sta.exe"
        if timing.is_file() and analyzer.is_file():
            try:
                self._start("performance", fpga_performance.report_command(self.project, self.tool_bin),
                            "Collecting performance results and worst setup paths…")
                return
            except (OSError, ValueError) as error:
                self._complete_performance(f"Detailed timing paths unavailable: {error}")
                return
        self._complete_performance("Detailed timing paths unavailable: no timing report or Quartus timing analyzer.")

    def _complete_performance(self, extraction_error=""):
        try:
            report = fpga_performance.read_report(
                self.project, source_path=str(self.built_path),
                verified=self._build_image is not None, verification_error=self._build_error,
            )
            if extraction_error:
                report["notes"].append(extraction_error)
            self.performance.set_report(report)
            self.report_tabs.setCurrentWidget(self.performance)
        except (OSError, ValueError) as error:
            extraction_error = f"Performance report unavailable: {error}"
        if extraction_error:
            self.log.appendPlainText("\n" + extraction_error)
        self.ready = self._build_image is not None
        if self._build_error:
            self.status.setText(self._build_error)
        else:
            message = ("Build and timing checks passed. " if self._build_clocked else
                       "Clockless build and pin-to-pin delay checks passed. ")
            message += "Select a cable, then Program FPGA."
            if extraction_error:
                message += " Detailed performance information is incomplete; see Build log."
            self.status.setText(message)
        self.refresh_source()

    def _fail(self, message):
        self.operation = None
        self.status.setText(message)
        self.log.appendPlainText("\n" + message)
        self._update_controls()

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            self.status.setText("Wait for the FPGA operation to finish before closing this window.")
            event.ignore()
        else:
            event.accept()
