"""Exercise saved circuit files through the OOC command line."""
# Run the public command line on saved source and inspect both diagnostics and exit status.
# A valid digital check must not accidentally invoke the electrical numerical solver.

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class CliTest(unittest.TestCase):
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
