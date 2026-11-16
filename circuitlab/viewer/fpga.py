"""Build OOC and configure a DE10-Lite using Quartus's background tools."""

from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
)

from circuitlab.model.graph import CircuitError
from circuitlab.tooling import fpga


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
        self.operation = None
        self.output = bytearray()
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.setWindowTitle("FPGA — DE10-Lite / HYBRID01")
        self.resize(850, 600)
        layout = QVBoxLayout(self)
        description = QLabel(
            "Write and save OOC in your IDE. Build here, select the board's USB-Blaster, "
            "then program the FPGA. Quartus runs in the background.\n"
            "Profile: HYBRID01 · DE10-Lite · 50 MHz · SW0–SW9 synchronized automatically · LED0–LED9 · optional audio on JP1 pin 1. "
            "Programming lasts until power-off; it does not write flash."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
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
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._browse)
        tool_row.addWidget(self.browse_button)
        layout.addLayout(tool_row)
        actions = QHBoxLayout()
        self.build_button = QPushButton("Build from OOC")
        self.build_button.clicked.connect(self.build)
        actions.addWidget(self.build_button)
        self.refresh_button = QPushButton("Refresh cables")
        self.refresh_button.clicked.connect(self.refresh_cables)
        actions.addWidget(self.refresh_button)
        self.cables = QComboBox()
        self.cables.setMinimumContentsLength(20)
        self.cables.currentIndexChanged.connect(self._update_controls)
        actions.addWidget(self.cables, 1)
        self.program_button = QPushButton("Program FPGA")
        self.program_button.clicked.connect(self.program)
        actions.addWidget(self.program_button)
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
        layout.addWidget(self.log, 1)
        footer = QHBoxLayout()
        self.folder_button = QPushButton("Open build folder")
        self.folder_button.clicked.connect(self._open_folder)
        footer.addWidget(self.folder_button)
        footer.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
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
        self.source_label.setText(f"Source: {path or 'No OOC file loaded'}")
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
        self.quartus.setEnabled(idle)
        self.browse_button.setEnabled(idle)
        self.cables.setEnabled(idle)
        self.close_button.setEnabled(idle)
        self.folder_button.setEnabled(self.project is not None)
        self.progress.setVisible(not idle)

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
        self._update_controls()
        try:
            path, source = self.current_document()
            if path is None:
                raise ValueError("Open and save your HYBRID01 OOC file in the IDE first.")
            self.tool_bin = self._tools()
            self.project = fpga.prepare_project(source, self.build_dir)
            self.built_path = path
            self.log.clear()
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
            self._fail("Could not start Quartus: " + self.process.errorString())

    def _finished(self, exit_code, exit_status):
        self._read_output()
        operation, self.operation = self.operation, None
        if operation is None:
            return
        output = self.output.decode("utf-8", errors="replace")
        try:
            if exit_code or exit_status != QProcess.ExitStatus.NormalExit:
                raise ValueError(f"Quartus {operation} failed (exit {exit_code}). Read the log below.")
            if operation == "build":
                image = fpga.verify_build(self.project)
                self.ready = True
                self.status.setText(f"Build and timing checks passed. Select a cable, then Program FPGA.\n{image}")
                self.refresh_source()
            elif operation == "cables":
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
