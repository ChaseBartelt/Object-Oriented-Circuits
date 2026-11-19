"""Resource sharing preserves fixed-width state and rising-edge semantics."""
# Exercise both sides of resource-sharing eligibility: equivalent self-increments should
# share arithmetic, while unsupported update shapes must retain their original semantics.

from pathlib import Path
import random
import re
import unittest
from unittest.mock import patch

from circuitlab.exporters.clocked import Expressions
from circuitlab.exporters.verilog import compile_chips
from circuitlab.math.digital import DigitalSimulation


ROOT = Path(__file__).resolve().parents[2]


def original_simulation(source, profile="ooc"):
    # The original lowering remains the fallback; suppress only sharing to
    # compare the two independent Verilog shapes in the same interpreter.
    with patch.object(Expressions, "select_increment", return_value=None):
        return DigitalSimulation(source, profile)


def set_state(sim, **values):
    for name, value in values.items():
        sim._values[sim._slots[name]] = value


class ClockedOptimizationTest(unittest.TestCase):
    def test_shared_helpers_follow_all_operand_dependencies(self):
        source = '''chip Ordered(clk,a,b,y) {
            clk,a,b=input; y=output; q[8]; step[6];
            clock(clk) {
                if (a) { if (b) { q += step + 31; } else { q += step - 1; } }
                else { q += (step + 1) & 3; }
            }
            y=q[7];
        }'''
        hdl = compile_chips(source)[0][-1]
        declared = set()
        for name, expression in re.findall(r"wire \[\d+:0\] (ooc_expr_\d+) = ([^;]+);", hdl):
            self.assertLessEqual(set(re.findall(r"ooc_expr_\d+", expression)), declared)
            declared.add(name)

    def test_self_increment_shares_one_adder_without_registers_or_syntax_changes(self):
        source = '''chip Accumulator(clk, select, y) {
            clk, select = input; y = output; phase[32];
            clock(clk, 50MHz) {
                if (select) { phase += 880Hz; }
                else { phase += 440Hz; }
            }
            y = phase[31];
        }'''
        hdl = compile_chips(source)[0][-1]
        self.assertEqual(hdl.count("phase +"), 1)
        self.assertEqual(len(re.findall(r"\breg\b", hdl)), 1)
        self.assertIn("select ? 17'd75591 : 16'd37796", hdl)
        sim = DigitalSimulation(source)
        self.assertEqual(set(sim.dependencies["phase"]), {"phase", "select", "clk"})
        self.assertNotIn("ooc_expr", sim.signal_expressions["phase"])

    def test_exhaustive_small_state_and_unequal_dynamic_increment_widths(self):
        source = '''chip Widths(clk, select, y) {
            clk, select = input; y = output; q[4]; a[3]; b[5];
            clock(clk) { if (select) { q += a; } else { q += b; } }
            y = q[3];
        }'''
        sim = DigitalSimulation(source)
        reference = original_simulation(source)
        for q in range(16):
            for a in range(8):
                for b in range(32):
                    for select in (0, 1):
                        for current in (sim, reference):
                            set_state(current, q=q, a=a, b=b)
                            current.set_input("select", select)
                            current.step()
                        expected = (q + (a if select else b)) & 15
                        self.assertEqual(sim.values["q"], expected)
                        self.assertEqual(sim.values, reference.values)

    def test_dynamic_condition_and_operands_read_old_state(self):
        source = '''chip Moving(clk, a, y) {
            clk, a = input; y = output; q[8]; step[6]; held;
            clock(clk) {
                held = a; step += 3;
                if ((held & a) || q > 125) { q += step + 31; }
                else { q += step - 1; }
            }
            y = q[7];
        }'''
        sim, reference = DigitalSimulation(source), original_simulation(source)
        rng = random.Random(20261116)
        for _ in range(300):
            value = rng.randrange(2)
            sim.set_input("a", value)
            reference.set_input("a", value)
            old = dict(sim.values)
            increment = ((old["step"] + 31) if ((old["held"] & value) or old["q"] > 125)
                         else (old["step"] - 1)) & 63
            self.assertEqual(sim.step(), reference.step())
            self.assertEqual(sim.values["q"], (old["q"] + increment) & 255)
            self.assertEqual(sim.values["held"], value)

    def test_missing_else_multistatement_and_nonself_addition_are_unchanged(self):
        bodies = (
            "if (a) { q += 1; }",  # Hold the state when a is low.
            "if (a) { q += 1; held = a; } else { q += 2; }",
            "if (a) { q = narrow + 1; } else { q = narrow + 16; }",
            "if (a) { q += 1; } else if (held) { q += 2; } else { q += 3; }",
        )
        for body in bodies:
            source = ("chip Guard(clk,a,y) { clk,a=input; y=output; "
                      "q[8]=250; narrow[4]=15; held; clock(clk) { " + body + " } y=q[7]; }")
            with self.subTest(body=body):
                hdl = compile_chips(source)[0][-1]
                with patch.object(Expressions, "select_increment", return_value=None):
                    self.assertEqual(hdl, compile_chips(source)[0][-1])
                sim, reference = DigitalSimulation(source), original_simulation(source)
                for a in (0, 1, 1, 0, 0):
                    sim.set_input("a", a)
                    reference.set_input("a", a)
                    self.assertEqual(sim.step(), reference.step())
                if "narrow +" in body:
                    # Preserve the original four-bit overflow before assignment
                    # to the eight-bit state; general factoring would change it.
                    sim.set_input("a", 1)
                    self.assertEqual(sim.step()["q"], 0)

    def test_nested_sharing_and_last_nonblocking_write_wins(self):
        for tail in ("", "q += 7;", "if (b) { q = 99; }"):
            source = '''chip Nested(clk,a,b,y) {
                clk,a,b=input; y=output; q[8]=253;
                clock(clk) {
                    q += 11;
                    if (a) { if (b) { q += 1; } else { q += 2; } }
                    else { q += 3; }
                    ''' + tail + '''
                } y=q[7];
            }'''
            sim, reference = DigitalSimulation(source), original_simulation(source)
            for a, b in ((0, 0), (1, 0), (1, 1), (0, 1)) * 10:
                for current in (sim, reference):
                    current.set_input("a", a)
                    current.set_input("b", b)
                self.assertEqual(sim.step(), reference.step())

    def test_hybrid_cycle_equivalence_switches_wrap_and_blink_boundaries(self):
        source = (ROOT / "tests/fixtures/digital/hybrid_assembly.ckt").read_text()
        sim, reference = DigitalSimulation(source, "de10-lite"), original_simulation(source, "de10-lite")
        rng = random.Random(440880)
        for initial in (0, (1 << 31) - 37796, (1 << 32) - 37796, (1 << 32) - 75591, 0xffffffff):
            sim.reset()
            reference.reset()
            for current in (sim, reference):
                set_state(current, **{"ooc_logic.Tone_1_phase": initial,
                                      "ooc_logic.ooc_every_2": 24_999_998})
            phase, meta, sync, timer, led = initial, 0, 0, 24_999_998, 0
            for _ in range(200):
                switch = rng.randrange(2)
                phase = (phase + (75591 if sync else 37796)) & 0xffffffff
                meta, sync = switch, meta
                if timer == 24_999_999:
                    timer, led = 0, led ^ 1
                else:
                    timer += 1
                for current in (sim, reference):
                    current.set_input("SW0", switch)
                    current.step()
                self.assertEqual(sim.values, reference.values)
                self.assertEqual(sim.values["ooc_logic.Tone_1_phase"], phase)
                self.assertEqual(sim.values["AUDIO_OUT"], phase >> 31)
                self.assertEqual(sim.values["LEDR0"], led)
                self.assertEqual(sim.values["ooc_sw0_meta"], meta)
                self.assertEqual(sim.values["ooc_sw0_sync"], sync)
                self.assertEqual(sim.values["ooc_logic.ooc_every_2"], timer)

    def test_hybrid_batched_full_second_preserves_edges_and_switch_latency(self):
        source = (ROOT / "tests/fixtures/digital/hybrid_assembly.ckt").read_text()
        sim, reference = DigitalSimulation(source, "de10-lite"), original_simulation(source, "de10-lite")
        # Include switch changes just before/after LED edges and accumulated
        # phase wraps. Both implementations skip only proven quiet intervals.
        for switch, cycles in ((1, 1), (0, 1), (1, 24_999_997), (0, 1),
                               (1, 1), (0, 24_999_999)):
            traces = []
            for current in (sim, reference):
                samples = []
                current.set_input("SW0", switch)
                self.assertEqual(current.advance(cycles, traces=("LEDR0", "AUDIO_OUT"),
                                                  sample=lambda cycle, values: samples.append((cycle, dict(values))),
                                                  sample_every=390625), cycles)
                traces.append(samples)
            self.assertEqual(sim.values, reference.values)
            self.assertEqual(traces[0], traces[1])
        self.assertEqual(sim.cycles, 50_000_000)
        self.assertEqual(sim.values["LEDR0"], 0)


if __name__ == "__main__":
    unittest.main()
