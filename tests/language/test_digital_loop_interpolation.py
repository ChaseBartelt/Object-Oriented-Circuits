"""Chip-local loops generate identifiers without changing clocked behavior."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.control_flow import expand_digital_loops
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


BANKS = """chip Counter(clk, enable, word) {
    clk, enable = input;
    word[4] = output;
    count[4] = 0;
    clock(clk, 10Hz) { if (enable) { count += 1; } }
    word = count;
}
chip Banks(clk, enable0, enable1, first, second) {
    clk, enable0, enable1 = input;
    first[4], second[4] = output;
    const COUNT[3] = 2;
    for (int i = 0; i < COUNT; i++) {
        bank${i}[4] = wire;
        Counter(clk, enable${i}, bank${i});
    }
    first = bank0;
    second = bank1;
}
main BankTest {
    clk, a, b = input;
    x[4], y[4] = output;
    Banks(clk, a, b, x, y);
}
"""


class DigitalLoopInterpolationTest(unittest.TestCase):
    def test_generated_identifiers_and_selector_arithmetic_keep_template_lines(self):
        rows = [
            ("const COUNT[3] = 2", 3),
            ("for (int i = 0; i < COUNT; i++) {", 4),
            ("sram${i}_mapped = wire", 5),
            ("word${i + 1}[8] = i", 6),
            ("bytes[8 * i + 7:8 * i] = word${i + 1}", 7),
            ("}", 8),
        ]
        self.assertEqual(expand_digital_loops(rows), [
            ("const COUNT[3] = 2", 3),
            ("sram0_mapped = wire", 5), ("word1[8] = 0", 6),
            ("bytes[7:0] = word1", 7),
            ("sram1_mapped = wire", 5), ("word2[8] = 1", 6),
            ("bytes[15:8] = word2", 7),
        ])

    def test_nested_loop_scopes_interpolate_headers_and_preserve_longer_names(self):
        rows = [
            ("for (int i = 0; i < 2; i++) {", 1),
            ("for (int j = ${i}; j < ${i + 2}; j++) {", 2),
            ("cell${i}_${j} = signal + i + j", 3),
            ("}", 4),
            ("tail${i} = identifier", 5),
            ("}", 6),
        ]
        self.assertEqual(expand_digital_loops(rows), [
            ("cell0_0 = signal + 0 + 0", 3),
            ("cell0_1 = signal + 0 + 1", 3),
            ("tail0 = identifier", 5),
            ("cell1_1 = signal + 1 + 1", 3),
            ("cell1_2 = signal + 1 + 2", 3),
            ("tail1 = identifier", 5),
        ])

    def test_generated_child_chips_have_independent_state_and_viewer_bindings(self):
        session = CircuitSession(source=BANKS)
        session.compile()
        self.assertFalse(session.issues)
        parent, left, right = session.drawing.instances
        self.assertEqual((left.name, right.name), ("Counter", "Counter"))
        self.assertEqual((left.parent, right.parent), (0, 0))
        template_line = next(index for index, row in enumerate(BANKS.splitlines(), 1)
                             if "Counter(clk, enable${i}" in row)
        self.assertEqual((left.line, right.line), (template_line, template_line))
        parent_bindings = dict(parent.signal_bindings)
        self.assertIn("bank0", parent_bindings)
        self.assertIn("bank1", parent_bindings)
        left_state = dict(left.signal_bindings)["count"]
        right_state = dict(right.signal_bindings)["count"]
        self.assertNotEqual(left_state, right_state)
        sim = DigitalSimulation(BANKS, design=session.digital_design)
        sim.set_input("a", 1)
        sim.advance(3)
        self.assertEqual((sim.values["x"], sim.values["y"]), (3, 0))
        self.assertEqual((sim.values[left_state], sim.values[right_state]), (3, 0))
        sim.set_input("a", 0)
        sim.set_input("b", 1)
        sim.advance(2)
        self.assertEqual((sim.values["x"], sim.values["y"]), (3, 2))
        self.assertNotIn("${", export_verilog(BANKS))

    def test_behavior_only_loops_preserve_runtime_if_and_clock_updates(self):
        source = """chip Generated(clk, enable, first, second) {
            clk, enable = input;
            first[4], second[4] = output;
            for (int i = 0; i < 2; i++) {
                state${i}[4] = 0;
                clock(clk, 10Hz) {
                    if (enable) { state${i} += ${i + 1}; }
                }
            }
            first = state0;
            second = state1;
        }"""
        sim = DigitalSimulation(source)
        sim.step()
        self.assertEqual((sim.values["first"], sim.values["second"]), (0, 0))
        sim.set_input("enable", 1)
        sim.advance(3)
        self.assertEqual((sim.values["first"], sim.values["second"]), (3, 6))
        sim.set_input("enable", 0)
        sim.step()
        self.assertEqual((sim.values["first"], sim.values["second"]), (3, 6))

    def test_bad_interpolation_reports_original_template_line_and_include_file(self):
        entry = Path.cwd() / "generated-main.ckt"
        library = entry.with_name("generated-chips.ckt")
        for expression in ("missing", "i +", "call(i)", "__import__(os)"):
            source = ("chip Bank(a,y) {\na = input; y = output;\n"
                      "for (int i=0; i<1; i++) {\n"
                      "word${" + expression + "} = wire;\n}\ny=a;\n}")
            session = CircuitSession(path=entry,
                source='include("generated-chips.ckt");\nmain { a=input; y=output; Bank(a,y); }',
                read_source={library: source}.__getitem__)
            session.compile()
            with self.subTest(expression=expression):
                self.assertEqual(len(session.issues), 1)
                self.assertEqual((session.issues[0].line, session.issues[0].path), (4, library))

    def test_unfinished_placeholder_does_not_leak_to_generated_identifiers(self):
        with self.assertRaises(CircuitError) as caught:
            expand_digital_loops([
                ("for (int i=0; i<1; i++) {", 1),
                ("word${i = wire", 9),
                ("}", 10),
            ])
        self.assertEqual(caught.exception.line, 9)
        self.assertIn("interpolation", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
