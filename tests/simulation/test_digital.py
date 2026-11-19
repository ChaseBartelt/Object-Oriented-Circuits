"""Digital behavior, compiler parity and bounded interpreter diagnostics."""
# Compare logical state at exact clock edges, including synchronization delay and unsigned
# wrapping. Fast/batched advancement must match individual interpreted cycles.

from pathlib import Path
import unittest
from unittest.mock import patch

from circuitlab.math.digital import DigitalSimulation, _Parser
from circuitlab.model.graph import CircuitError


ROOT = Path(__file__).resolve().parents[2]
HYBRID = (ROOT / "tests/fixtures/digital/HYBRID01.ckt").read_text()
TEN_SWITCHES = (ROOT / "tests/fixtures/digital/de10_switch_led.ckt").read_text()


class DigitalSimulationTest(unittest.TestCase):
    def test_combinational_truth_tables_and_initial_settle(self):
        sim = DigitalSimulation('''chip Logic(a, b, nand_y, xor_y, inverted) {
            a, b = input; nand_y, xor_y, inverted = output;
            nand_y = !(a & b); xor_y = a ^ b; inverted = !a;
        }''')
        self.assertEqual(sim.inputs, ("a", "b"))
        self.assertEqual(sim.clocks, ())
        self.assertIsNone(sim.frequency)
        self.assertEqual(sim.values["inverted"], 1)
        for a in (0, 1):
            for b in (0, 1):
                sim.set_input("a", a)
                sim.set_input("b", b)
                self.assertEqual(sim.values["nand_y"], int(not (a and b)))
                self.assertEqual(sim.values["xor_y"], a ^ b)
        self.assertEqual(sim.dependencies["nand_y"], ("a", "b"))
        self.assertEqual(sim.step()["xor_y"], 0)
        self.assertEqual(sim.cycles, 1)

    def test_forward_outputs_and_clock_conditions_settle_topologically(self):
        sim = DigitalSimulation('''chip Forward(clk, a, y, z, q) {
            clk, a = input; y, z, q = output; held;
            y = z; z = !a; q = held;
            clock(clk, 1kHz) { if (y == 1) { held = 1; } else { held = 0; } }
        }''')
        self.assertEqual(sim.values["y"], 1)
        sim.step()
        self.assertEqual(sim.values["q"], 1)
        sim.set_input("a", 1)
        self.assertEqual(sim.values["y"], 0)
        sim.step()
        self.assertEqual(sim.values["q"], 0)
        self.assertEqual(sim.frequency, 1000.0)

    def test_all_ten_board_switches_have_two_sample_latency(self):
        sim = DigitalSimulation(TEN_SWITCHES, "de10-lite")
        self.assertEqual(sim.inputs, tuple(f"SW{i}" for i in range(10)))
        self.assertEqual(sim.outputs, tuple(f"LEDR{i}" for i in range(10)))
        self.assertEqual(sim.clocks, ("MAX10_CLK1_50",))
        self.assertEqual(sim.frequency, 50_000_000)
        for index in range(10):
            sim.reset()
            sim.set_input(f"SW{index}", 1)
            self.assertFalse(any(sim.values[name] for name in sim.outputs))
            sim.step()
            self.assertFalse(any(sim.values[name] for name in sim.outputs))
            sim.step()
            self.assertEqual([sim.values[name] for name in sim.outputs], [int(i == index) for i in range(10)])
            sim.set_input(f"SW{index}", 0)
            sim.step()
            self.assertEqual(sim.values[f"LEDR{index}"], 1)
            sim.step()
            self.assertEqual(sim.values[f"LEDR{index}"], 0)
        self.assertEqual(sim.dependencies["LEDR9"], ("ooc_sw9_sync",))
        self.assertIn("SW9", sim.dependencies["ooc_sw9_meta"])

    def test_generic_profile_does_not_add_board_synchronization(self):
        sim = DigitalSimulation(TEN_SWITCHES)
        sim.set_input("SW9", 1)
        self.assertEqual(sim.values["LEDR9"], 1)
        self.assertFalse(any("ooc_sw" in name for name in sim.signals))

    def test_clockless_board_switches_update_directly_without_advancing_time(self):
        inputs = ",".join(f"SW{i}" for i in range(10))
        outputs = ",".join(f"LEDR{i}" for i in range(10))
        source = f"chip HYBRID01({inputs},{outputs}) {{{inputs}=input;{outputs}=output;LEDR[9:0]=SW[9:0];}}"
        sim = DigitalSimulation(source, "de10-lite")
        self.assertEqual(sim.inputs, tuple(f"SW{i}" for i in range(10)))
        self.assertEqual(sim.clocks, ())
        self.assertIsNone(sim.frequency)
        self.assertNotIn("register", sim.signal_kinds.values())
        for index in range(10):
            sim.set_input(f"SW{index}", 1)
            self.assertEqual([sim.values[name] for name in sim.outputs], [int(i == index) for i in range(10)])
            sim.set_input(f"SW{index}", 0)
            self.assertFalse(any(sim.values[name] for name in sim.outputs))
        self.assertEqual(sim.cycles, 0)
        self.assertIn("SW9", sim.dependencies["LEDR9"])
        self.assertLessEqual(set(sim.dependencies["LEDR9"]), set(sim.inputs))

    def test_clockless_priority_selector_all_switch_combinations_in_both_profiles(self):
        inputs = ",".join(f"SW{i}" for i in range(10))
        outputs = ",".join(f"LEDR{i}" for i in range(10))
        lowest = ":".join(f"SW{i}?{1 << i}" for i in range(8)) + ":0"
        highest = ":".join(f"SW{i}?{1 << i}" for i in reversed(range(8))) + ":0"
        source = f"""chip HYBRID01({inputs},{outputs}) {{
            {inputs}=input; {outputs}=output;
            requests[8],lowest[8],highest[8],winner[8]=wire;
            requests=SW[7:0]; lowest={lowest}; highest={highest};
            winner=SW8?(SW9?highest:lowest):0;
            LEDR[7:0]=winner;
            LEDR8=SW8 && (requests!=0);
            LEDR9=SW8 && ((requests & (requests-1))!=0);
        }}"""
        for profile in ("ooc", "de10-lite"):
            with self.subTest(profile=profile):
                sim = DigitalSimulation(source, profile)
                self.assertEqual(sim.clocks, ())
                self.assertIsNone(sim.frequency)
                self.assertNotIn("register", sim.signal_kinds.values())
                for switches in range(1024):
                    for bit in range(10):
                        sim.set_input(f"SW{bit}", (switches >> bit) & 1)
                    requests, enabled, highest_priority = switches & 255, switches & 256, switches & 512
                    winner = ((1 << (requests.bit_length() - 1)) if highest_priority else requests & -requests) if requests and enabled else 0
                    expected = winner | (int(bool(enabled and requests)) << 8) | (int(bool(enabled and requests.bit_count() > 1)) << 9)
                    actual = sum(sim.values[f"LEDR{i}"] << i for i in range(10))
                    self.assertEqual(actual, expected, (profile, switches))
                self.assertEqual(sim.cycles, 0)

    def test_manual_board_synchronizer_is_not_duplicated(self):
        generic, board = DigitalSimulation(HYBRID), DigitalSimulation(HYBRID, "de10-lite")
        self.assertEqual(generic.signals, board.signals)
        for value in (1, 0, 1):
            generic.set_input("SW0", value)
            board.set_input("SW0", value)
            for _ in range(4):
                self.assertEqual(generic.step(), board.step())
        self.assertFalse(any(name.startswith("ooc_sw0") for name in board.signals))

    def test_board_wrapper_inner_register_sees_old_synchronized_input(self):
        sim = DigitalSimulation('''chip HYBRID01(MAX10_CLK1_50, SW0, LEDR0) {
            MAX10_CLK1_50, SW0 = input; LEDR0 = output; held;
            clock(MAX10_CLK1_50, 50MHz) { held = SW0; }
            LEDR0 = held;
        }''', "de10-lite")
        sim.set_input("SW0", 1)
        sim.step()
        sim.step()
        self.assertEqual(sim.values["ooc_sw0_sync"], 1)
        self.assertEqual(sim.values["LEDR0"], 0)
        sim.step()
        self.assertEqual(sim.values["LEDR0"], 1)
        self.assertIn("ooc_logic.held", sim.signals)

    def test_hybrid_frequency_steps_and_switch_latency(self):
        sim = DigitalSimulation(HYBRID)
        sim.set_input("SW0", 1)
        for expected in (37796, 75592, 151183):
            sim.step()
            self.assertEqual(sim.values["phase"], expected)
        self.assertEqual(sim.signals["phase"], 32)
        self.assertFalse(any(name.startswith("ooc_expr_") for name in sim.signals))
        self.assertIn("sw_sync", sim.dependencies["phase"])

    def test_hybrid_audio_rises_at_rounded_accumulator_boundary(self):
        sim = DigitalSimulation(HYBRID)
        first_rise = ((1 << 31) + 37795) // 37796
        for _ in range(first_rise - 1):
            sim.step()
        self.assertEqual(sim.values["AUDIO_OUT"], 0)
        sim.step()
        self.assertEqual(sim.values["AUDIO_OUT"], 1)
        self.assertEqual(sim.values["phase"], first_rise * 37796)

    def test_every_first_fire_and_frequency_inside_every(self):
        sim = DigitalSimulation('''chip Timer(clk, led, audio) {
            clk = input; led, audio = output; state; phase[8];
            clock(clk, 10Hz) {
                every(500ms) { state = !state; phase += 0.5Hz; }
            }
            led = state; audio = phase[7];
        }''')
        for _ in range(4):
            sim.step()
        self.assertEqual(sim.values["state"], 0)
        self.assertEqual(sim.values["phase"], 0)
        sim.step()
        self.assertEqual(sim.values["state"], 1)
        self.assertEqual(sim.values["phase"], 64)
        for _ in range(5):
            sim.step()
        self.assertEqual(sim.values["state"], 0)
        self.assertEqual(sim.values["phase"], 128)
        self.assertEqual(sim.values["audio"], 1)

    def test_hybrid_original_blink_counter_boundary(self):
        sim = DigitalSimulation(HYBRID)
        timer = next(name for name in sim.signals if name.startswith("ooc_every_"))
        self.assertEqual(sim.signals[timer], 25)
        # Start immediately before the real 25-million-edge boundary rather
        # than making a unit test spend minutes counting the unchanged range.
        sim._values[sim._slots[timer]] = 24_999_998
        sim.step()
        self.assertEqual(sim.values["LEDR0"], 0)
        self.assertEqual(sim.values[timer], 24_999_999)
        sim.step()
        self.assertEqual(sim.values["LEDR0"], 1)
        self.assertEqual(sim.values[timer], 0)

    def test_nonblocking_updates_across_blocks_and_last_write_wins(self):
        sim = DigitalSimulation('''chip Pipeline(clk, a, y) {
            clk, a = input; y = output; first; second; count[3];
            clock(clk, 1Hz) { first = a; count += 1; count += 1; }
            clock(clk, 1Hz) { second = first; }
            y = second;
        }''')
        sim.set_input("a", 1)
        sim.step()
        self.assertEqual(sim.values["y"], 0)
        self.assertEqual(sim.values["count"], 1)
        sim.step()
        self.assertEqual(sim.values["y"], 1)
        self.assertEqual(sim.values["count"], 2)

    def test_intermediate_widths_unsigned_subtraction_and_comparisons(self):
        sim = DigitalSimulation('''chip Widths(clk, y) {
            clk = input; y = output; count[4] = 15; zero[4]; wide[8]; test;
            clock(clk, 1Hz) {
                wide = (count + 1) + 16;
                count += 1;
                zero = zero - 1;
                test = (count > zero) && !zero;
            }
            y = test;
        }''')
        sim.step()
        self.assertEqual(sim.values["count"], 0)
        self.assertEqual(sim.values["wide"], 16)  # Four-bit (15+1) wraps first.
        self.assertEqual(sim.values["zero"], 15)
        self.assertEqual(sim.values["test"], 1)

    def test_nested_conditions_else_if_and_boolean_precedence(self):
        sim = DigitalSimulation('''chip Branches(clk, a, b, y) {
            clk, a, b = input; y = output; count[4];
            clock(clk, 1Hz) {
                if (a) { if (b) { count = 3; } else { count = 4; } }
                else if (b && !a) { count = 5; }
                else { count = 6; }
            }
            y = count[0];
        }''')
        for a, b, expected in ((0, 0, 6), (0, 1, 5), (1, 0, 4), (1, 1, 3)):
            sim.set_input("a", a)
            sim.set_input("b", b)
            sim.step()
            self.assertEqual(sim.values["count"], expected)

    def test_reset_restores_initializers_constants_and_clears_inputs(self):
        sim = DigitalSimulation('''chip Initial(clk, a, y) {
            clk, a = input; y = output; count[4] = 3; const STEP[4] = 2;
            clock(clk) { if (a) { count += STEP; } }
            y = count[0];
        }''')
        self.assertIsNone(sim.frequency)
        sim.set_input("a", 1)
        sim.step()
        self.assertEqual(sim.values["count"], 5)
        sim.reset()
        self.assertEqual(sim.cycles, 0)
        self.assertEqual(sim.values["count"], 3)
        self.assertEqual(sim.values["STEP"], 2)
        self.assertEqual(sim.values["a"], 0)
        self.assertEqual(sim.values["clk"], 0)
        self.assertEqual(sim.values["y"], 1)

    def test_clock_is_high_for_edge_expressions_and_low_between_steps(self):
        sim = DigitalSimulation('''chip ClockLevel(clk, y) {
            clk = input; y = output; held;
            clock(clk, 1Hz) { held = clk; } y = held;
        }''')
        sim.step()
        self.assertEqual(sim.values["held"], 1)
        self.assertEqual(sim.values["clk"], 0)
        for name, value in (("clk", 1), ("y", 1), ("missing", 0)):
            with self.assertRaises(CircuitError):
                sim.set_input(name, value)

    def test_rejects_multiple_clocks_and_chips(self):
        with self.assertRaisesRegex(CircuitError, "one input clock"):
            DigitalSimulation('''chip Multi(a, b, y) { a, b = input; y = output; first; second;
                clock(a, 1Hz) { first = !first; } clock(b, 2Hz) { second = !second; }
                y = first ^ second;
            }''')
        with self.assertRaisesRegex(CircuitError, "one chip"):
            DigitalSimulation("chip A(a,y) { a = input; y = output; y = a; }\n"
                              "chip B(a,y) { a = input; y = output; y = a; }")

    def test_profile_and_input_value_validation(self):
        source = "chip A(a,y) { a = input; y = output; y = a; }"
        with self.assertRaisesRegex(CircuitError, "profile"):
            DigitalSimulation(source, "invalid")
        sim = DigitalSimulation(source)
        for value in (-1, 2, 0.5, "1"):
            with self.subTest(value=value), self.assertRaises(CircuitError):
                sim.set_input("a", value)

    def test_parser_fails_closed_for_unsupported_or_malformed_hdl(self):
        for text in ("", "module A (input wire a, output wire y); initial begin y = a; end endmodule",
                     "module A (input wire a, output wire y); assign y = $system(a); endmodule",
                     "module A (input wire a, output wire y); assign y = 1'bx; endmodule",
                     "module A (input wire a, output wire y); assign y = 1'b2; endmodule",
                     "module A (input wire a, output wire y); assign y = a ? a : ; endmodule",
                     "module A (input wire a, output wire y); always @(posedge a) begin"):
            with self.subTest(text=text), self.assertRaises(CircuitError):
                _Parser(text).parse()

    def test_invalid_generated_feedback_and_unknown_signals_are_rejected(self):
        source = "chip A(a,y) { a = input; y = output; y = a; }"
        for hdl in ("module A (input wire a, output wire y); assign y = y; endmodule",
                    "module A (input wire a, output wire y); assign y = missing; endmodule"):
            with patch("circuitlab.math.digital.compile_chips", return_value=[("A", (("a", "input"), ("y", "output")), (), hdl)]):
                with self.assertRaises(CircuitError):
                    DigitalSimulation(source)

    def test_deep_valid_combinational_expressions_report_limits_without_recursion_crash(self):
        for expression in (" | ".join(["a"] * 1500), "(" * 300 + "a" + ")" * 300,
                           "!" * 300 + "a"):
            with self.subTest(expression=expression[:30]), self.assertRaisesRegex(CircuitError, "limit"):
                DigitalSimulation("chip Deep(a,y) { a = input; y = output; y = " + expression + "; }")

    def test_board_metadata_describes_actual_switch_registers_and_output_connections(self):
        sim = DigitalSimulation(TEN_SWITCHES, "de10-lite")
        self.assertEqual(sum(kind == "register" for kind in sim.signal_kinds.values()), 20)
        self.assertEqual(sim.signal_kinds["SW0"], "input")
        self.assertEqual(sim.signal_kinds["LEDR0"], "output")
        self.assertEqual(sim.signal_expressions["LEDR0"], "LEDR0 = ooc_sw0_sync")
        self.assertIn("ooc_sw0_meta = SW0", sim.signal_expressions["ooc_sw0_meta"])
        self.assertIn("ooc_sw0_sync = ooc_sw0_meta", sim.signal_expressions["ooc_sw0_sync"])
        self.assertEqual(sim.signal_clocks["ooc_sw9_meta"], "MAX10_CLK1_50")

    def test_metadata_preserves_conditions_constants_initializers_and_expands_expression_wires(self):
        sim = DigitalSimulation('''chip Descriptions(clk, a, b, y, both) {
            clk, a, b = input; y, both = output; count[8] = 3; const STEP[8] = 2;
            clock(clk, 1kHz) { if (a & b) { count += STEP; } else { count = 0; } }
            y = count[0]; both = a & b;
        }''')
        self.assertEqual(sim.signal_expressions["both"], "both = (a & b)")
        self.assertEqual(sim.signal_expressions["y"], "y = count[0]")
        self.assertEqual(sim.signal_kinds["STEP"], "constant")
        self.assertEqual(sim.signal_expressions["STEP"], "STEP = 2 (constant)")
        text = sim.signal_expressions["count"]
        self.assertIn("Initial: 3", text)
        self.assertIn("if ((a & b)) count = (count + STEP)", text)
        self.assertIn("if (!((a & b))) count = 0", text)
        self.assertNotIn("ooc_expr", text)
        self.assertEqual(sim.signal_clocks["count"], "clk")

    def test_hybrid_metadata_shows_compiled_phase_steps_and_bounded_tooltips(self):
        sim = DigitalSimulation(HYBRID)
        text = sim.signal_expressions["phase"]
        self.assertIn("phase + ((sw_sync == 0) ? 37796 : 75591)", text)
        self.assertTrue(all(len(text) <= 500 for text in sim.signal_expressions.values()))
        many = " ".join("count += 1;" for _ in range(80))
        crowded = DigitalSimulation("chip Long(clk,y) { clk = input; y = output; count[8]; "
                                    "clock(clk) { " + many + " } y = count[0]; }")
        self.assertEqual(len(crowded.signal_expressions["count"]), 500)
        self.assertTrue(crowded.signal_expressions["count"].endswith("…"))

    def test_batch_hybrid_full_second_and_exact_blink_edges(self):
        sim = DigitalSimulation(HYBRID)
        samples = []
        with patch.object(sim, "step", wraps=sim.step) as ordinary:
            done = sim.advance(50_000_000, traces=("LEDR0", "AUDIO_OUT"),
                               sample=lambda cycle, values: samples.append((cycle, dict(values))),
                               sample_every=390625)
        self.assertEqual(done, 50_000_000)
        self.assertLess(ordinary.call_count, 2000)  # Proven interval skips, not 50M Python steps.
        self.assertEqual(sim.values["phase"], (50_000_000 * 37796) & 0xffffffff)
        edges, last = [], 0
        for cycle, values in samples:
            if values["LEDR0"] != last:
                edges.append(cycle)
                last = values["LEDR0"]
            self.assertEqual(values["AUDIO_OUT"], ((cycle * 37796) >> 31) & 1)
        self.assertEqual(edges, [25_000_000, 50_000_000])
        self.assertEqual(sim.values["LEDR0"], 0)

    def test_batch_matches_reference_with_switch_changes_and_two_stage_sync(self):
        for source, profile in ((HYBRID, "ooc"), (TEN_SWITCHES, "de10-lite")):
            with self.subTest(profile=profile):
                fast, reference = DigitalSimulation(source, profile), DigitalSimulation(source, profile)
                for value, cycles in ((1, 1), (1, 2), (0, 3), (1, 117321), (0, 237)):
                    fast.set_input("SW0", value)
                    reference.set_input("SW0", value)
                    fast.advance(cycles)
                    for _ in range(cycles):
                        reference.step()
                    self.assertEqual(fast.values, reference.values)
                    self.assertEqual(fast.cycles, reference.cycles)
                fast.reset()
                reference.reset()
                self.assertEqual(fast.values, reference.values)

    def test_batch_unsigned_wrap_guards_nba_and_fallback_match_reference(self):
        bodies = [
            "count += 3; other = count;",
            "count = count - 3; other = count + 1;",
            "if (count < 9) { count += 3; } else { count = 0; } other += 1;",
            "if (count <= 9) { count += 3; } else { count = 0; }",
            "if (count > 3) { count = count - 2; } else { count = 14; }",
            "if (count >= 3) { count = count - 2; } else { count = 14; }",
            "if (count == 9) { count = 0; } else { count += 3; }",
            "if (count != 9) { count += 3; } else { count = 0; }",
            "if (count) { count = count - 1; } else { count = 12; }",
            "if (!count) { count = 12; } else { count = count - 1; }",
            "if (count[2]) { other += 2; } count += 1;",
            "if (count < 5 && other != 8) { other += 1; } count += 1;",
            "other = (count + 1) + 16; count += 1;",
            "count += other; other += 1;",
            "count = (count + 1) ^ other; other += 1;",
            "every(300ms) { count += 1; } every(500ms) { other += 1; }",
            "count += 1; if (count == 3) { count = 7; } count += 2;",
            "if (clk) { count += 1; } other = clk;",
        ]
        for body in bodies:
            with self.subTest(body=body):
                source = ("chip Batch(clk,y) { clk = input; y = output; count[4] = 13; "
                          "other[8]; clock(clk,10Hz) { " + body + " } y = count[3]; }")
                fast, reference = DigitalSimulation(source), DigitalSimulation(source)
                for count in (1, 2, 7, 103, 1024):
                    fast.advance(count)
                    for _ in range(count):
                        reference.step()
                    self.assertEqual(fast.values, reference.values)

    def test_batch_sampled_edges_have_same_cycles_as_reference(self):
        source = '''chip Sample(clk, y) { clk = input; y = output; phase[8];
            clock(clk, 1kHz) { phase += 3; } y = phase[7]; }'''
        fast, reference = DigitalSimulation(source), DigitalSimulation(source)
        actual, expected = [], []
        last = 0
        def record(cycle, values):
            nonlocal last
            if last != values["y"]:
                actual.append((cycle, values["y"]))
                last = values["y"]
        fast.advance(2000, traces=("y", "phase"), sample=record, sample_every=233)
        last = 0
        for _ in range(2000):
            reference.step()
            if last != reference.values["y"]:
                expected.append((reference.cycles, reference.values["y"]))
                last = reference.values["y"]
        self.assertEqual(actual, expected)

    def test_batch_driver_assignment_truncation_is_preserved(self):
        # A deliberately generated HDL fixture exercises a narrow intermediate;
        # the OOC front end may reject vector-to-port assignments itself.
        hdl = '''module Narrow (input wire clk, output wire y);
            reg [7:0] count = 8'd0; reg [7:0] held = 8'd0;
            wire short = count;
            assign y = held[0];
            always @(posedge clk) begin
              count <= count + 8'd1;
              if (short) begin held <= held + 8'd1; end
            end
            endmodule'''
        with patch("circuitlab.math.digital.compile_chips", return_value=[("Narrow", (), (), hdl)]):
            fast, reference = DigitalSimulation("fixture"), DigitalSimulation("fixture")
        fast.advance(1001)
        for _ in range(1001):
            reference.step()
        self.assertEqual(fast.values, reference.values)

    def test_batch_budget_and_arguments(self):
        sim = DigitalSimulation(HYBRID)
        self.assertEqual(sim.advance(0), 0)
        self.assertEqual(sim.advance(100, deadline=0), 0)
        self.assertEqual(sim.cycles, 0)
        for value in (-1, 0.5):
            with self.assertRaises(CircuitError):
                sim.advance(value)
        with self.assertRaises(CircuitError):
            sim.advance(1, sample_every=0)
        with patch("circuitlab.math.digital.perf_counter", side_effect=[0, 10]):
            self.assertEqual(sim.advance(100, sample=lambda *_: None, sample_every=3, deadline=1), 3)


if __name__ == "__main__":
    unittest.main()
