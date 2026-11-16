"""Exercise the asynchronous FPGA UI without invoking Quartus or hardware."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QProcess, QSettings, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QPushButton

from circuitlab.tooling import fpga
from circuitlab.viewer.fpga import FpgaDialog
from circuitlab.viewer.window import ViewerWindow

ROOT = Path(__file__).resolve().parents[1]
CABLE = "USB-Blaster [USB-0]"
CHAIN = f"1) {CABLE}\n   031050DD   10M50DA(.|ES)/10M50DC\n"
SUCCESS = "Info (209011): Successfully performed operation(s)\n"


class FakeProcess(QObject):
    """Signal-compatible process double: records commands, never executes them."""

    readyReadStandardOutput = Signal()
    finished = Signal(int, object)
    errorOccurred = Signal(object)
    ProcessChannelMode = QProcess.ProcessChannelMode
    ProcessError = QProcess.ProcessError
    ExitStatus = QProcess.ExitStatus

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = []
        self.data = b""
        self.directory = None

    def setProcessChannelMode(self, mode):
        self.channel_mode = mode

    def setWorkingDirectory(self, directory):
        self.directory = directory

    def start(self, command, arguments):
        self.started.append((command, arguments))

    def readAllStandardOutput(self):
        data, self.data = self.data, b""
        return data

    def errorString(self):
        return "test executable unavailable"

    def complete(self, text="", code=0, status=QProcess.ExitStatus.NormalExit):
        self.data += text.encode("utf-8")
        self.readyReadStandardOutput.emit()
        self.finished.emit(code, status)


class FpgaViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = (ROOT / "examples/digital/HYBRID01.ckt").read_text(encoding="utf-8")
        self.document = [self.root / "my oscillator.ckt", self.source]
        self.document[0].write_text(self.source, encoding="utf-8")
        self.process_patch = patch("circuitlab.viewer.fpga.QProcess", FakeProcess)
        self.process_patch.start()
        self.settings_patch = patch(
            "circuitlab.viewer.fpga.QSettings",
            side_effect=lambda *_: QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat),
        )
        self.settings_patch.start()
        self.dialog = FpgaDialog(None, lambda: tuple(self.document), build_dir=self.root / "builds")
        self.dialog._tools = Mock(return_value=self.root / "fake-quartus")

    def tearDown(self):
        self.dialog.operation = None
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.settings_patch.stop()
        self.process_patch.stop()
        self.temp.cleanup()

    def built(self):
        self.dialog.build()
        with patch("circuitlab.tooling.fpga.verify_build", return_value=self.root / "tested.sof"):
            self.dialog.process.complete("Build complete\n")
        self.assertTrue(self.dialog.ready)
        self.dialog.cables.addItem(CABLE)
        self.assertTrue(self.dialog.program_button.isEnabled())

    def test_build_uses_current_document_and_disables_programming_until_verified(self):
        self.document[1] = self.source.replace("440Hz", "220Hz")
        expected = fpga.compile_chips(self.document[1])[0][3]
        self.dialog.build()
        generated = (self.dialog.project.directory / "build/HYBRID01.v").read_text(encoding="utf-8")
        self.assertEqual(generated.strip(), expected.strip())
        self.assertTrue(self.dialog.project.matches_source(self.document[1]))
        self.assertFalse(self.dialog.project.matches_source(self.source))
        self.assertEqual(self.dialog.built_path, self.document[0])
        self.assertEqual(self.dialog.operation, "build")
        self.assertFalse(self.dialog.program_button.isEnabled())
        self.assertFalse(self.dialog.build_button.isEnabled())
        self.assertEqual(self.dialog.process.started[-1][1], ["--flow", "compile", "HYBRID01"])
        self.assertEqual(Path(self.dialog.process.directory), self.dialog.project.directory)
        self.dialog.process.complete("compiler failed", code=1)
        self.assertFalse(self.dialog.ready)
        self.dialog.cables.addItem(CABLE)
        count = len(self.dialog.process.started)
        self.dialog.program()
        self.assertEqual(len(self.dialog.process.started), count)
        self.assertFalse(self.dialog.program_button.isEnabled())
        self.assertTrue(self.dialog.build_button.isEnabled())

    def test_zero_exit_build_requires_verified_artifacts(self):
        self.dialog.build()
        with patch("circuitlab.tooling.fpga.verify_build", side_effect=fpga.FpgaError("missing fresh image")):
            self.dialog.process.complete()
        self.assertFalse(self.dialog.ready)
        self.assertFalse(self.dialog.busy)
        self.assertIn("missing fresh image", self.dialog.status.text())

    def test_source_change_during_build_or_after_build_invalidates_image(self):
        self.dialog.build()
        self.document[1] += "\n# newer source\n"
        with patch("circuitlab.tooling.fpga.verify_build", return_value=self.root / "tested.sof"):
            self.dialog.process.complete()
        self.assertFalse(self.dialog.ready)
        self.assertIn("OOC changed", self.dialog.status.text())
        self.built()
        self.document[0] = self.root / "another file.ckt"
        self.dialog.refresh_source()
        self.assertFalse(self.dialog.ready)
        self.assertFalse(self.dialog.program_button.isEnabled())

    def test_source_change_during_chain_scan_never_starts_programming(self):
        self.built()
        with patch("circuitlab.tooling.fpga.program_command", return_value=["fake-programmer", "program"]) as command:
            self.dialog.program()
            self.assertEqual(self.dialog.operation, "chain")
            self.document[1] += "\n# changed during scan\n"
            self.dialog.process.complete(CHAIN)
            self.assertEqual(command.call_count, 1)
        self.assertFalse(self.dialog.ready)
        self.assertFalse(self.dialog.busy)
        self.assertFalse(any(name == "fake-programmer" for name, _ in self.dialog.process.started))
        self.assertIn("Build again", self.dialog.status.text())

    def test_selected_chain_errors_never_start_programming(self):
        self.built()
        for scan in (
            CHAIN.replace("031050DD", "020F40DD"),
            CHAIN.replace(CABLE, "USB-Blaster [USB-1]"),
            CHAIN + "   031050DD   10M50DA\n",
            CHAIN + "Error: JTAG access denied\n",
        ):
            with self.subTest(scan=scan):
                with patch("circuitlab.tooling.fpga.program_command", return_value=["fake-programmer", "program"]) as command:
                    self.dialog.program()
                    self.dialog.process.complete(scan)
                    self.assertEqual(command.call_count, 1)
                self.assertFalse(self.dialog.busy)
                self.assertFalse(any(name == "fake-programmer" for name, _ in self.dialog.process.started))

    def test_success_requires_programmer_marker_and_normal_exit(self):
        self.built()
        for output, code, status, success in (
            ("done", 0, QProcess.ExitStatus.NormalExit, False),
            (SUCCESS, 1, QProcess.ExitStatus.NormalExit, False),
            (SUCCESS, 0, QProcess.ExitStatus.CrashExit, False),
            (SUCCESS + "Error: transfer failed", 0, QProcess.ExitStatus.NormalExit, False),
            (SUCCESS, 0, QProcess.ExitStatus.NormalExit, True),
        ):
            with self.subTest(output=output, code=code, status=status):
                with patch("circuitlab.tooling.fpga.program_command", return_value=["fake-programmer", "program"]) as command:
                    self.dialog.program()
                    self.dialog.process.complete(CHAIN)
                    self.assertEqual(self.dialog.operation, "program")
                    self.assertEqual(command.call_count, 2)
                    self.dialog.process.complete(output, code, status)
                self.assertFalse(self.dialog.busy)
                self.assertEqual("programmed successfully" in self.dialog.status.text(), success)
                self.assertIn(output, self.dialog.log.toPlainText())

    def test_failed_to_start_signal_unlocks_controls(self):
        self.dialog.build()
        self.dialog.process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
        self.assertFalse(self.dialog.busy)
        self.assertFalse(self.dialog.ready)
        self.assertTrue(self.dialog.build_button.isEnabled())
        self.assertTrue(self.dialog.close_button.isEnabled())
        self.assertIn("Could not start Quartus", self.dialog.status.text())
        # A late completion after FailedToStart cannot mark the build ready.
        self.dialog.process.complete()
        self.assertFalse(self.dialog.ready)

    def test_busy_dialog_cannot_close_or_reject(self):
        self.dialog.show()
        self.dialog.build()
        event = QCloseEvent()
        self.dialog.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.dialog.reject()
        self.assertTrue(self.dialog.isVisible())
        self.assertFalse(self.dialog.close_button.isEnabled())
        self.dialog.process.complete(code=1)
        self.assertTrue(self.dialog.close())

    def test_viewer_button_reuses_dialog_and_busy_viewer_cannot_close(self):
        window = ViewerWindow()
        try:
            window.session.path = self.document[0]
            window.session.set_source(self.source)
            button = next(button for button in window.findChildren(QPushButton) if button.text() == "FPGA…")
            button.click()
            dialog = window.fpga_window
            self.assertIsInstance(dialog, FpgaDialog)
            self.assertEqual(dialog.current_document(), (window.path, self.source))
            button.click()
            self.assertIs(window.fpga_window, dialog)
            dialog.operation = "build"
            event = QCloseEvent()
            window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertIn("Wait for the FPGA operation", window.status.text())
        finally:
            if getattr(window, "fpga_window", None):
                window.fpga_window.operation = None
                window.fpga_window.close()
            window.placement_dirty = False
            window.source_tools.checkpoint()
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
