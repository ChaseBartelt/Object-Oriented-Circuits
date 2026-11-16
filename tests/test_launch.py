"""Check both supported launch commands with the project interpreter."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LaunchTest(unittest.TestCase):
    def test_editor_window_exits_with_the_ide_pipe_still_open(self):
        script = """
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QTimer
from circuitlab.__main__ import main
from circuitlab.viewer.window import ViewerWindow
original = ViewerWindow.show
def show(window):
    original(window)
    QTimer.singleShot(250, window.close)
ViewerWindow.show = show
raise SystemExit(main())
"""
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", script, "--editor",
             str(ROOT / "examples" / "circuit_method.ckt")],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Unlike communicate(), wait() leaves stdin open, just as VS Code does.
            code = process.wait(timeout=40)
            self.assertEqual(code, 0, process.stderr.read().decode("utf-8", errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def test_launch_as_module_or_file(self):
        for command, cwd in (
            (("-m", "circuitlab"), ROOT),
            ((str(ROOT / "circuitlab" / "__main__.py"),), ROOT.parent),
        ):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-B", *command, "--help"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Circuit source file", result.stdout)
