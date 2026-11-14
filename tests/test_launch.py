"""Check both supported launch commands with the project interpreter."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LaunchTest(unittest.TestCase):
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
