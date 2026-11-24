"""Exercise saved circuit files through the OOC command line."""
# Run the public command line on saved source and inspect both diagnostics and exit status.
# A valid digital check must not accidentally invoke the electrical numerical solver.

import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from circuitlab.model.graph import CircuitError
from circuitlab.tooling.cli import _diagnostic, main
from circuitlab.tooling.session import issue_for

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class CliTest(unittest.TestCase):
    def test_multiline_guidance_remains_one_editor_readable_diagnostic(self):
        errors = StringIO()
        with redirect_stderr(errors):
            _diagnostic(Path("part.ckt"), "error", "Missing wire connections.\nUse R4(10) between(a,b).", 4)
        self.assertEqual(errors.getvalue(),
                         "part.ckt(4): error OOC001: Missing wire connections. Use R4(10) between(a,b).\n")

    def run_cli(self, command, path):
        return subprocess.run(
            [sys.executable, "-m", "circuitlab.tooling.cli", command, str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_solves_the_voltage_divider_from_a_path_with_spaces(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "voltage divider.ckt"
            path.write_bytes((FIXTURES / "voltage_divider.ckt").read_bytes())
            result = self.run_cli("solve", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Node out: 4 V", result.stdout)

    def test_check_compiles_without_running_the_solver(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "floating.ckt"
            path.write_text("R1(1k) between(a, b);", encoding="utf-8")
            checked = self.run_cli("check", path)
            solved = self.run_cli("solve", path)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertNotEqual(solved.returncode, 0)
        self.assertIn(f"{path}: error OOC001:", solved.stderr)

    def test_diagnostic_points_to_the_included_source_file(self):
        with TemporaryDirectory() as folder:
            entry = Path(folder) / "main.ckt"
            child = Path(folder) / "bad part.ckt"
            entry.write_text('include("bad part.ckt");\nV1(5) between(in, 0);\n', encoding="utf-8")
            child.write_text("# Invalid line below\nthis is invalid\n", encoding="utf-8")
            result = self.run_cli("check", entry)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{child}(2): error OOC001:", result.stderr)

    def test_digital_chips_can_be_checked(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "inverter.ckt"
            path.write_text(
                "chip Inverter(a, out) {\n"
                "    a = input;\n    out = output;\n    out = !a;\n"
                "}\n",
                encoding="utf-8",
            )
            checked = self.run_cli("check", path)
            solved = self.run_cli("solve", path)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Check passed (digital chip).", checked.stdout)
        self.assertNotEqual(solved.returncode, 0)
        self.assertIn("Digital chips use the Verilog compiler.", solved.stderr)

    def test_include_only_digital_file_uses_the_same_compiler_as_the_viewer(self):
        with TemporaryDirectory() as folder:
            entry = Path(folder) / "library.ckt"
            child = entry.with_name("inverter.ckt")
            entry.write_text('include("inverter.ckt");', encoding="utf-8")
            child.write_text("chip Inverter(a,y) { a=input; y=output; y=!a; }", encoding="utf-8")
            checked = self.run_cli("check", entry)
            solved = self.run_cli("solve", entry)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("Check passed (digital chip).", checked.stdout)
            self.assertIn("Digital chips use the Verilog compiler.", solved.stderr)
            child.write_text("\nchip Inverter(a,y) { a=input; y=output; y=missing; }", encoding="utf-8")
            invalid = self.run_cli("check", entry)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn(f"{child}(2): error OOC001:", invalid.stderr)

    def test_solver_error_guidance_and_locations_match_the_viewer(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "nonlinear.ckt"
            path.write_text("V1(5) between(in,0);\nR1(1k) between(in,0);", encoding="utf-8")
            for line in (None, 2):
                error = CircuitError("No operating point.", line, "NonlinearConvergence", path=path)
                output, errors = StringIO(), StringIO()
                with self.subTest(line=line), patch("circuitlab.tooling.cli.analyze", side_effect=error), \
                        redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(main(["solve", str(path)]), 1)
                location = f"{path}({line})" if line else str(path)
                self.assertEqual(errors.getvalue(), f"{location}: error OOC001: {issue_for(error).message}\n")

    def test_digital_states_can_be_checked_but_not_electrically_solved(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state bits.ckt"
            path.write_text("""chip StateBits(out) {
                out = output;
                phase[32];
                preset[8] = 100;
                led_state;
                flag = 1;
                out = phase[31] | preset[6] | led_state | flag;
            }""", encoding="utf-8")
            checked = self.run_cli("check", path)
            solved = self.run_cli("solve", path)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Check passed (digital chip).", checked.stdout)
        self.assertNotEqual(solved.returncode, 0)
        self.assertIn("Digital chips use the Verilog compiler.", solved.stderr)
        self.assertNotIn("Results (", solved.stdout)
