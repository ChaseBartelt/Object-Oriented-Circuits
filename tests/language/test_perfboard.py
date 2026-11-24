"""Perfboard declarations are physical metadata, independent of the netlist."""

import unittest
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.model.perfboard import PerfBoard


class PerfboardLanguageTest(unittest.TestCase):
    def test_ranges_and_exclusions_do_not_change_electrical_components(self):
        source = """Vwavegen(0.1 Vrms) between(signal,0);
Negative(-5V) between(vminus,0);
Positive(5V) between(vplus,0);
R1(10k) between(signal,out);
R2(10k) between(out,0);
"""
        # Source names must carry their electrical type; the supplied exclusion
        # spelling is independent of whether the named parts are modeled here.
        source = source.replace("Negative(", "Vnegative(").replace("Positive(", "Vpositive(")
        plain = compile_circuit(source, render_only=True)
        circuit = compile_circuit(source + "perf[A-R,1-24] exclude[Vwavegen, Vnegative, Vpositive];", render_only=True)
        self.assertEqual(circuit.components, plain.components)
        self.assertEqual(circuit.probes, plain.probes)
        self.assertEqual(circuit.mode, plain.mode)
        board = circuit.perfboard
        self.assertEqual(board.row_count, 18)
        self.assertEqual(board.column_count, 24)
        self.assertEqual(board.rows, tuple("ABCDEFGHIJKLMNOPQR"))
        self.assertEqual(board.columns, tuple(range(1, 25)))
        self.assertEqual(board.exclusions, ("Vwavegen", "Vnegative", "Vpositive"))
        self.assertEqual(board.pitch_mm, 2.54)
        self.assertIsNone(plain.perfboard)

    def test_main_and_spreadsheet_rows_and_single_hole_ranges(self):
        circuit = compile_circuit("main { perf[z-ab,8-8]; R1(10) between(a,0); }", render_only=True)
        board = circuit.perfboard
        self.assertEqual(board.rows, ("Z", "AA", "AB"))
        self.assertEqual(board.columns, (8,))
        self.assertEqual(board.row_count, 3)
        self.assertEqual(compile_circuit("perf[A-A,1-1];", render_only=True).perfboard, PerfBoard("A", "A", 1, 1))

    def test_board_is_valid_as_the_last_statement_in_main(self):
        source = """main {
    VfpgaPower(3.3V) between(fpga_power,0);
    VdaisyLeft(0.1V) between(left,0);
    VdaisyRight(0.1V) between(right,0);
    RscarlettLeft(1k) between(left,0);
    RscarlettRight(1k) between(right,0);
    Rload(10k) between(fpga_power,0);
    measure = V fpga_power 0;
    perf[A-R,1-24] exclude[VfpgaPower, VdaisyLeft, VdaisyRight, RscarlettLeft, RscarlettRight];
}
"""
        circuit = compile_circuit(source, render_only=True)
        self.assertEqual(circuit.perfboard.exclusions,
                         ("VfpgaPower", "VdaisyLeft", "VdaisyRight", "RscarlettLeft", "RscarlettRight"))
        self.assertEqual(circuit.perfboard.line, 9)
        self.assertEqual(len(circuit.components), 6)
        self.assertEqual(circuit.probes[0].targets, ("fpga_power", "0"))

    def test_perfboard_can_share_a_physical_line_with_other_statements(self):
        for source in (
            "perf[A-R,1-24]; R1(10) between(a,0);",
            "R1(10) between(a,0); perf[A-R,1-24];",
            "perf[A-R,1-24]; chip Load(a,b) {R1(10) between(a,b);} main {Load(a,0);}",
        ):
            with self.subTest(source=source):
                circuit = compile_circuit(source, render_only=True)
                self.assertEqual(circuit.perfboard.row_count, 18)
                self.assertEqual(len(circuit.components), 1)
                self.assertEqual(circuit.components[0].value, 10)
        with self.assertRaises(CircuitError) as caught:
            compile_circuit("\nperf[A-R,1-24]; perf[A-R,1-24];", render_only=True)
        self.assertEqual(caught.exception.line, 2)

    def test_malformed_ranges_exclusions_and_oversized_boards_keep_source_line(self):
        for declaration in (
            "perf[R-A,1-24]", "perf[A-R,24-1]", "perf[A-R,0-24]",
            "perf[A-R,1-257]", "perf[A-ZZ,1-24]", "perf[A:R,1-24]",
            "perf[A-R,1-24] exclude[R1,]", "perf[A-R,1-24] exclude[R1,r1]",
        ):
            with self.subTest(declaration=declaration), self.assertRaises(CircuitError) as caught:
                compile_circuit("R1(10) between(a,0);\n" + declaration + ";", render_only=True)
            self.assertEqual(caught.exception.line, 2)

    def test_multiple_or_nested_boards_report_the_declaration(self):
        sources = (
            ("perf[A-R,1-24];\nperf[A-R,1-24];", 2),
            ("chip Part(a,b) {\nperf[A-R,1-24];\nR1(10) between(a,b);\n}\nmain {Part(a,0);}", 2),
            ("series(a,b) {\nperf[A-R,1-24];\nR1 = 10;\n}", 2),
        )
        for source, line in sources:
            with self.subTest(source=source), self.assertRaises(CircuitError) as caught:
                compile_circuit(source, render_only=True)
            self.assertEqual(caught.exception.line, line)

    def test_included_board_metadata_survives_merge_with_qualified_exclusions(self):
        path = Path.cwd() / "perf-main.ckt"
        source = 'include("power.ckt");\nR1(10) between(a,0);'
        child = "perf[A-R,1-24] exclude[V1];\nV1(5V) between(supply,0);"
        circuit = compile_circuit(source, path=path, read_source=lambda _: child, render_only=True)
        self.assertEqual(circuit.perfboard.exclusions, ("@file1.V1",))
        self.assertEqual(circuit.perfboard.path, path.with_name("power.ckt"))
        self.assertEqual(circuit.perfboard.line, 1)
        self.assertEqual(len(circuit.components), 2)

    def test_conflicting_included_board_reports_child_source(self):
        path = Path.cwd() / "perf-main.ckt"
        source = 'include("power.ckt");\nperf[A-R,1-24];\nR1(10) between(a,0);'
        child = "V1(5V) between(supply,0);\nperf[A-Z,1-30];"
        with self.assertRaises(CircuitError) as caught:
            compile_circuit(source, path=path, read_source=lambda _: child, render_only=True)
        self.assertEqual(caught.exception.path, path.with_name("power.ckt"))
        self.assertEqual(caught.exception.line, 2)

    def test_included_battery_exclusion_resolves_its_display_label(self):
        path = Path.cwd() / "perf-main.ckt"
        source = 'include("power.ckt");\nRroot(10) between(a,0);'
        child = """perf[A-R,1-24] exclude[Negative];
(battery Negative)
Vneg = 5
wire = neg
ground = 0
(end battery)
R1(1k) between(neg,0);
"""
        circuit = compile_circuit(source, path=path, read_source=lambda _: child, render_only=True)
        battery, = [part for part in circuit.components if part.is_source]
        self.assertEqual(battery.label, "power.Negative")
        self.assertEqual(circuit.perfboard.exclusions, (battery.name,))


if __name__ == "__main__":
    unittest.main()
