"""Connectivity and solver checks for reusable circuit methods."""

import unittest
from math import tau
from pathlib import Path

from circuitlab.math.analysis import solve_input
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.layout.automatic import compact
from circuitlab.layout.checks import preserves_nets
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import schematic_layout
from circuitlab.layout.wiring import signal_flow


class CircuitMethods(unittest.TestCase):
    def test_builtin_gates_expand_without_definitions(self):
        for gate, args, transistor_count in (
            ("not", "a", 1), ("buf", "a", 2), ("and", "a, b", 3),
            ("nand", "a, b", 2), ("or", "a, b", 3), ("nor", "a", 1),
            ("nor", "a, b, c", 3), ("xor", "a, b", 8), ("xnor", "a, b", 9),
        ):
            explicit = compile_circuit(f"main {{ {gate}(v, 0, {args}, y); }}")
            if gate in {"and", "nand", "or", "nor"} and "," in args:
                args = f"({args})"
            with self.subTest(gate=gate, args=args):
                circuit = compile_circuit(f"main {{ {gate}(v, {args}, y); }}")
                self.assertEqual(circuit.instances[0].kind, "gate")
                self.assertEqual(circuit.instances[0].name, gate)
                self.assertEqual(circuit.components, explicit.components)
                self.assertEqual(
                    sum(part.kind == "Q" for part in circuit.components), transistor_count
                )

    def test_omitted_gate_ground_uses_the_enclosing_pin(self):
        circuit = compile_circuit("""
        circuit logic(vcc, gnd, a, b, return_net) {
            nor(vcc, (a, b), inherited);
            nor(vcc, return_net, a, explicit);
        }
        main { logic(v, local_ground, a, b, separate_ground); }
        """)
        self.assertEqual(
            [dict(gate.pins)["gnd"] for gate in circuit.instances if gate.kind == "gate"],
            ["local_ground", "separate_ground"],
        )

    def test_override_replaces_only_the_matching_builtin_gate_calls(self):
        source = """
        nor(v, 0, a, b, first);
        circuit nested(vcc, gnd, a, b, y) {
            nor(vcc, (a, b), y);
        }
        main {
            nested(v, 0, a, b, second);
            nor(v, a, third);
        }
        @override
        gate nor(vcc, gnd, a, b, y) {
            RCustom vcc y 3.3k;
        }
        """
        circuit = compile_circuit(source)
        loads = {
            part.n: part.value for part in circuit.components
            if part.kind == "R" and part.p == "v"
        }
        self.assertEqual(loads, {"first": 3300, "second": 3300, "third": 2200})
        self.assertEqual(sum(part.kind == "Q" for part in circuit.components), 1)
        file_without_main = """
        @Override
        gate nor(vcc, gnd, a, y) { RX vcc y 10k; }
        nor(v, 0, a, out);
        """
        self.assertEqual(compile_circuit(file_without_main).components[0].value, 10000)

    def test_override_requires_a_builtin_gate_and_rejects_duplicates(self):
        gate = "@Override\ngate nor(vcc, gnd, a, y) { R1 vcc y 2k; }"
        for source, diagnostic in (
            ("@Override", "directly above a gate"),
            ("main { @Override; }", "top-level gate"),
            (gate.replace("gate nor", "circuit nor"), "directly above a gate"),
            (gate.replace("gate nor", "gate nope"), "No built-in gate"),
            (gate.replace("gate nor", "gate xor"), "Use gate xor"),
            (f"{gate}\n{gate}", "already has an override"),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(CircuitError, diagnostic):
                    compile_circuit(source)

    def test_part_parameters_build_both_twin_t_branches(self):
        source = """
        battery V1 { Vrms = 1; rad/s = 10000; wire = IN; }
        circuit tee(a, b, c, input, output, tap, gnd) {
            a between(input, tap);
            b between(tap, output);
            c between(tap, gnd);
        }
        main {
            tee(R1(1k), R2(1k), C1(200n), IN, OUT, RTAP, 0);
            tee(C2(100n), C3(100n), R3(500), IN, OUT, CTAP, 0);
            Rload(100k) between(OUT, 0);
            measure = V OUT 0;
        }
        """
        result = solve_input(source)
        self.assertAlmostEqual(result.network.frequency, 10000 / tau)
        self.assertLess(abs(result.readings[0][2]), 1e-12)
        circuit = result.circuit
        self.assertEqual(
            [(p.kind, p.p, p.n) for p in circuit.components[:6]],
            [
                ("R", "IN", "RTAP"),
                ("R", "RTAP", "OUT"),
                ("C", "RTAP", "0"),
                ("C", "IN", "CTAP"),
                ("C", "CTAP", "OUT"),
                ("R", "CTAP", "0"),
            ],
        )
        for mode in ("auto", "routed", "labeled"):
            drawing = schematic_layout(circuit, component_labels(circuit), mode)
            self.assertTrue(preserves_nets(circuit, drawing))
            if mode != "labeled":
                self.assertEqual({net for net, *_ in drawing.connectors}, {"OUT"})
                self.assertFalse(drawing.crossings)
                upper, lower = drawing.labels["RTAP"], drawing.labels["CTAP"]
                self.assertEqual(upper[0], lower[0])
                self.assertGreater(upper[1], lower[1])
                self.assertEqual(len(drawing.grounds), 4)
                for part, start, end, _ in drawing.symbols:
                    is_vertical = part.label.split("_")[0] in {
                        "V1",
                        "C1",
                        "R3",
                        "Rload",
                    }
                    self.assertEqual(start[0] == end[0], is_vertical)
        with self.assertRaisesRegex(CircuitError, 'argument "a" expects a resistor'):
            solve_input(source.replace("R1(1k)", "1k"))
        with self.assertRaisesRegex(CircuitError, 'argument "a" for R1'):
            solve_input(source.replace("R1(1k)", "R1(100nF)"))

    def test_series_part_slots_choose_type_at_the_call(self):
        circuit = compile_circuit("""
        battery V1 { Vrms = 1; f = 1k; wire = in; }
        series pair(a, b) {
            a = part;
            b = part;
        }
        main { parallel { pair(R(1k), C(100n)); pair(L(2m), R(2k)); } }
        """)
        self.assertEqual(
            [p.kind for p in circuit.components], ["V", "R", "C", "L", "R"]
        )
        self.assertEqual(len({p.name for p in circuit.components}), 5)

    def test_value_and_wire_parameters_make_independent_parallel_dividers(self):
        source = """
        battery V1 { V = 6; wire = Vin; }
        series t(a,b,c,d) {
            a = R1;
            b = R2;
            d = wire;
            c = R3;
        }
        main {
            parallel {
                t(1k,1k,2k,left);
                t(1k,1k,2k,right);
            }
            measure = V left 0;
            measure = V right 0;
        }
        """
        for resistance, expected in (("2k", 3.0), ("4k", 4.0)):
            with self.subTest(resistance=resistance):
                result = solve_input(source.replace("2k,right", f"{resistance},right"))
                circuit = result.circuit
                left, right = [reading[2].real for reading in result.readings]
                self.assertAlmostEqual(left, 3.0)
                self.assertAlmostEqual(right, expected)
                _, r1, r2, r3, r4, r5, r6 = circuit.components
                self.assertEqual((r1.p, r4.p, r3.n, r6.n), ("Vin", "Vin", "0", "0"))
                self.assertEqual(
                    (r2.n, r3.p, r5.n, r6.p), ("left", "left", "right", "right")
                )
                self.assertNotEqual(r1.n, r4.n)
                drawing = schematic_layout(circuit, component_labels(circuit), "auto")
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertEqual(
                    {net for net, *_ in drawing.connectors}, {"left", "right"}
                )

        tail_tap = source.replace(
            "d = wire;\n            c = R3;", "c = R3;\n            d = wire;"
        )
        self.assertEqual(
            [reading[2] for reading in solve_input(tail_tap).readings], [0, 0]
        )
        with self.assertRaisesRegex(CircuitError, 'argument "a" for R1'):
            solve_input(source.replace("t(1k,1k,2k,left)", "t(bad,1k,2k,left)"))

    def test_buffer_uses_the_same_named_return_as_the_battery_and_filter(self):
        result = solve_input("""
        battery V1 {
            Vpk = 3;
            f = 900;
            Vdc = 4;
            wire = in;
            ground = agnd;
        }
        main {
            series { R1 = 1k; }
            parallel { C1 = 1u; wire = a; R2 = 10k; ground = agnd; }
            op1(a, buffered, buffered);
            measure = V buffered agnd;
        }
        """)
        circuit = result.circuit
        supply, _, capacitor, load, amp = circuit.components
        self.assertEqual({supply.n, capacitor.n, load.n, amp.n}, {"agnd"})
        self.assertEqual(result.network.references["buffered"], "agnd")
        self.assertAlmostEqual(result.readings[0][2], 40 / 11)
        self.assertAlmostEqual(abs(result.readings[1][2]), 0.3682297521)
        layout = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assertEqual(layout, signal_flow(circuit))
        self.assertTrue(preserves_nets(circuit, layout))
        shunt_returns = {
            end for part, _, end, _ in layout.symbols if part.name in {"C1", "R2"}
        }
        self.assertTrue(
            any(set(wire) == shunt_returns for wire in layout.wires["agnd"])
        )

    def test_independent_buffers_keep_their_own_ground(self):
        result = solve_input("""
        battery V1 { V = 10; wire = inputA; ground = agnd; }
        battery V2 { V = 5; wire = inputB; ground = dgnd; }
        main {
            op1(inputA, outA, outA);
            op2(inputB, outB, outB);
            measure = V outA agnd;
            measure = V outB dgnd;
        }
        """)
        self.assertEqual(
            [p.n for p in result.circuit.components[-2:]], ["agnd", "dgnd"]
        )
        self.assertEqual([r[2] for r in result.readings], [10, 5])

    def test_parallel_ground_applies_to_the_preceding_branch(self):
        for second_ground in ("", "ground = agnd;"):
            with self.subTest(second_ground=second_ground):
                source = f"""
                battery V1 {{ Vrms = 1; f = 900; wire = in; ground = agnd; }}
                main {{
                    series {{ R1 = 1k; }}
                    parallel {{
                        C1 = 1u;
                        wire = a;
                        ground = bgnd;
                        R2 = 10k;
                        {second_ground}
                    }}
                    op1(a, buffered, buffered);
                    measure = V buffered agnd;
                }}
                """
                result = solve_input(source)
                circuit = result.circuit
                _, _, capacitor, resistor, amp = circuit.components
                self.assertEqual((capacitor.p, capacitor.n), ("a", "bgnd"))
                self.assertEqual((resistor.p, resistor.n), ("a", "agnd"))
                self.assertEqual(amp.n, "agnd")
                self.assertAlmostEqual(result.readings[0][2], 10 / 11)
                for mode in ("auto", "routed", "labeled"):
                    drawing = schematic_layout(circuit, component_labels(circuit), mode)
                    self.assertTrue(preserves_nets(circuit, drawing))
                    self.assertEqual(
                        set(drawing.ground_names.values()), {"agnd", "bgnd"}
                    )
                    if mode != "labeled":
                        self.assertEqual(drawing, signal_flow(circuit))

    def test_parallel_taps_follow_branch_order_without_splitting_the_rails(self):
        result = solve_input("""
        battery V1 { V = 8; wire = in; }
        main {
            series { R1 = 1k; }
            parallel {
                wire = before;
                R2 = 2k;
                wire = between;
                R3 = 2k;
                wire = after;
            }
            op1(between, buffered, buffered);
            measure = V before after;
            measure = V buffered after;
            measure = V after 0;
        }
        """)
        self.assertEqual([reading[2] for reading in result.readings], [4, 4, 0])
        self.assertEqual(
            [
                (t.name, t.net, t.component, t.terminal)
                for t in result.circuit.wire_taps
            ],
            [
                ("before", "before", "R2", 0),
                ("between", "before", "R3", 0),
                ("after", "0", "R3", 1),
            ],
        )

    def test_named_ground_returns_are_distinct_and_labeled_in_each_view(self):
        result = solve_input("""
        battery V1 { V = 10; wire = supply; ground = agnd; }
        battery V2 { V = 5; wire = logic; ground = dgnd; }
        main {
            parallel(supply, agnd) { R1 = 1k; ground = agnd; }
            parallel(logic, dgnd) { R2 = 2k; ground = dgnd; }
            measure = V supply agnd;
            measure = V logic dgnd;
        }
        """)
        circuit = result.circuit
        self.assertEqual([reading[2] for reading in result.readings], [10, 5])
        self.assertEqual(result.network.references["supply"], "agnd")
        self.assertEqual(result.network.references["logic"], "dgnd")
        self.assertEqual({p.n for p in circuit.components}, {"agnd", "dgnd"})
        for mode in ("auto", "routed", "labeled"):
            drawing = schematic_layout(circuit, component_labels(circuit), mode)
            self.assertEqual(set(drawing.ground_names.values()), {"agnd", "dgnd"})
            self.assertTrue(preserves_nets(circuit, drawing))

    def test_a_ground_name_does_not_short_a_node_to_zero(self):
        result = solve_input("""
        battery V1 { V = 10; wire = in; ground = agnd; }
        main {
            series {
                R1 = 1k;
                wire = out;
                R2 = 1k;
                ground = agnd;
            }
            R3 agnd 0 1k;
            I1 0 agnd 1m;
            measure = V agnd 0;
            measure = V out agnd;
        }
        """)
        self.assertAlmostEqual(result.readings[0][2], 1)
        self.assertAlmostEqual(result.readings[1][2], 5)

    def test_braced_battery_with_rms_and_angular_frequency(self):
        result = solve_input("""
        battery V1{
            Vrms = 2.2;
            rad/s = 1000;
            wire = in;
        }
        main{
            series{
                R1 = 22;
                wire = A;
                L1 = 10m;
            }
            parallel{
                wire = B;
                C1 = 100u;
            }
            measure = I R1;
            measure = V B 0;
        }
        """)
        circuit = result.circuit
        self.assertEqual(circuit.mode, "ac")
        self.assertEqual(
            [(part.label, part.p, part.n) for part in circuit.components],
            [("V1", "in", "0"), ("R1", "in", "A"), ("L1", "A", "B"), ("C1", "B", "0")],
        )
        self.assertAlmostEqual(result.network.frequency, 1000 / tau)
        self.assertAlmostEqual(result.readings[0][2], 0.1)
        self.assertAlmostEqual(result.readings[1][2], -1j)
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assertEqual([net for net, *_ in drawing.connectors], ["B"])
        self.assertTrue(preserves_nets(circuit, drawing))

    def test_braced_battery_reuses_dc_and_mixed_source_semantics(self):
        for values, mode, expected in (
            ("V = 4;", "dc", 4),
            ("Vrms = 2.2; Vdc = 4; rad / s = 1000 rad/s;", "mixed", 2.2),
        ):
            with self.subTest(mode=mode):
                result = solve_input(f"""
                main {{
                    battery V1 {{ {values} wire = in; wire = 0; }}
                    series {{ R1 = 1k; }}
                }}
                """)
                self.assertEqual(result.circuit.mode, mode)
                self.assertEqual(result.circuit.components[0].value, expected)
                if mode == "mixed":
                    self.assertEqual(result.circuit.components[0].dc, 4)

    def test_bare_nested_blocks_in_main_follow_the_source_and_return_to_ground(self):
        for supply in (
            "V1 = 18 : wire = Vin;",
            "(battery V1) V = 18 wire = Vin (end battery)",
        ):
            with self.subTest(supply=supply):
                source = (
                    supply
                    + """
                main{
                    series{ R1 = 1k; }
                    parallel{
                        wire = out; R2 = 4k;
                        series{ R3 = 1k; R4 = 3k; }
                    }
                    measure = V out 0;
                }
                """
                )
                result = solve_input(source)
                _, r1, r2, r3, r4 = result.circuit.components
                self.assertEqual((r1.p, r1.n), ("Vin", "out"))
                self.assertEqual((r2.p, r2.n), ("out", "0"))
                self.assertEqual((r3.p, r3.n), ("out", r4.p))
                self.assertEqual(r4.n, "0")
                self.assertAlmostEqual(result.readings[0][2].real, 12)
                circuit = compile_circuit(source, render_only=True)
                self.assertEqual(circuit.components, result.circuit.components)
                drawing = schematic_layout(circuit, component_labels(circuit), "auto")
                self.assertEqual(len(drawing.grounds), 3)
                self.assertTrue(preserves_nets(circuit, drawing))

    def test_anonymous_blocks_can_have_terminals_or_inherit_their_parent(self):
        source = """
        V1 Vin 0 18
        circuit divider(a, b, g) {
            series(a, b) { R1 = 1k; }
            parallel(b, g) {
                R2 = 4k;
                series { R3 = 1k; R4 = 3k; }
            }
        }
        main { divider(Vin, out, 0); measure = V out 0; }
        """
        result = solve_input(source)
        self.assertAlmostEqual(result.readings[0][2].real, 12)
        self.assertEqual(
            [part.name for part in result.circuit.components],
            ["V1", "R1_divider_1", "R2_divider_1", "R3_divider_1", "R4_divider_1"],
        )

        named = solve_input("""
        V1 Vin 0 18
        series feed { R1 = 1k; }
        parallel load { R2 = 4k; series { R3 = 1k; R4 = 3k; } }
        main { feed(Vin, out); load(out, 0); measure = V out 0; }
        """)
        self.assertAlmostEqual(named.readings[0][2].real, 12)

    def test_nested_brace_diagnostics(self):
        for source, message in (
            ("main {\nparallel {\nseries { R1=1k; }", 'Line 2.*Close "parallel"'),
            ("main {\nseries named { R1=1k; }\n}", "Line 2.*top level"),
            ("V1 vin 0 12\nmain { series {} }", "Line 2.*at least one"),
            (
                "V1 vin 0 12\nV2 aux 0 3\nmain { series { R1=1k; } }",
                "Line 3.*one grounded source",
            ),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(CircuitError, message):
                    compile_circuit(source)

    def test_series_and_parallel_default_to_two_terminals(self):
        source = """V1 = 12 : wire = Vin;
        series test {
            R1 = 2k;
            wire = b;
        }
        parallel test1 { R2 = 1k; }
        main {
            test(Vin, out);
            test1(out, 0);
            measure = V out 0;
        }
        port = out 0
        """
        result = solve_input(source)
        self.assertEqual(
            [(p.name, p.p, p.n) for p in result.circuit.components],
            [
                ("V1", "Vin", "0"),
                ("R1_test_1", "Vin", "out"),
                ("R2_test1_1", "out", "0"),
            ],
        )
        self.assertEqual(result.readings[0][2], 4)
        self.assertAlmostEqual(result.network.equivalent["impedance"].real, 2000 / 3)
        self.assertEqual(
            compile_circuit(source, render_only=True).components,
            result.circuit.components,
        )

    def test_circuit_uses_implicit_terminals_in_nested_calls(self):
        result = solve_input("""
        V1 vin 0 12
        circuit resistor { R1(1k) between(a, b); }
        circuit divider(input, output, ground) {
            resistor(input, output);
            resistor(output, ground);
        }
        main { divider(vin, out, 0); measure = V out 0; }
        """)
        self.assertAlmostEqual(result.readings[0][2].real, 6)

    def test_voltage_wire_preserves_ac_and_mixed_source_values(self):
        for raw, mode, voltage, dc in (
            ("12 V", "dc", 12, 12),
            ("2 Vrms", "ac", 2, 0),
            ("3 Vpk", "ac", 3 / (2**0.5), 0),
            ("4 V | 3 Vpk", "mixed", 3 / (2**0.5), 4),
        ):
            with self.subTest(raw=raw):
                source_text = f"V1 = {raw} : wire = Vin;\nR1 Vin 0 1k\n"
                if mode == "mixed":
                    source_text += "f = 60\n"
                result = solve_input(source_text)
                self.assertEqual(result.circuit.mode, mode)
                source = result.circuit.components[0]
                self.assertEqual((source.p, source.n, source.dc), ("Vin", "0", dc))
                self.assertAlmostEqual(source.value, voltage)

    def test_voltage_wire_binds_the_positive_terminal_inside_a_method(self):
        result = solve_input("""
        circuit supply {
            V1 = 12 : wire = a;
            R1(1k) between(a, b);
        }
        main { supply(Vin, 0); measure = V Vin 0; }
        """)
        self.assertEqual(result.readings[0][2], 12)
        self.assertEqual(result.circuit.components[0].name, "V1_supply_1")

    def test_current_wire_feeds_parallel_branches_from_ground(self):
        result = solve_input("""
        I1 = 10m : wire = in;
        main {
            parallel {
                R1 = 2k;
                R2 = 4k;
                R3 = 12k;
            }
            measure = V in 0;
            measure = I R1;
            measure = I R2;
            measure = I R3;
        }
        """)
        circuit = result.circuit
        source = circuit.components[0]
        self.assertEqual(
            (source.name, source.kind, source.p, source.n), ("I1", "I", "0", "in")
        )
        for row, expected in zip(
            result.readings, (12, 0.006, 0.003, 0.001), strict=True
        ):
            self.assertAlmostEqual(row[2].real, expected)
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assertTrue(preserves_nets(circuit, drawing))
        _, start, end, _ = next(
            symbol for symbol in drawing.symbols if symbol[0] == source
        )
        self.assertEqual(start[0], end[0])
        self.assertLess(start[1], end[1])

    def test_current_wire_supports_ac_and_mixed_values_in_a_method(self):
        for current, mode, expected in (
            ("10 mArms", "ac", [10]),
            ("2 mA | 10 mArms", "mixed", [2, 10]),
        ):
            with self.subTest(mode=mode):
                result = solve_input(f"""
                f = 60
                circuit supply(a) {{
                    I1 = {current} : wire = a;
                    R1(1k) between(a, 0);
                }}
                main {{ supply(bus); measure = V bus 0; }}
                """)
                self.assertEqual(result.circuit.mode, mode)
                for row, voltage in zip(result.readings, expected, strict=True):
                    self.assertAlmostEqual(row[2].real, voltage)

    def test_current_source_block_supports_ac_and_mixed_values(self):
        for current, expected_mode, expected_voltage in (
            ("10 mArms", "ac", 10),
            ("2 mA | 10 mArms", "mixed", 10),
        ):
            with self.subTest(current=current):
                result = solve_input(f"""
                (battery amp)
                    I = {current};
                    f = 60
                    wire = bus;
                (end battery)
                parallel load {{ R1 = 1k; }}
                main {{ load(bus, 0); measure = V bus 0; }}
                """)
                self.assertEqual(result.circuit.mode, expected_mode)
                self.assertAlmostEqual(result.readings[-1][2].real, expected_voltage)
                if expected_mode == "mixed":
                    self.assertAlmostEqual(result.readings[0][2].real, 2)

    def test_implicit_terminals_require_two_arguments(self):
        for kind, body, message in (
            ("series", "R1 = 1k;", "two wire names"),
            ("parallel", "R1 = 1k;", "two wire names"),
            ("circuit", "R1(1k) between(a, b);", "2 wire arguments"),
        ):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(CircuitError, f"Line 2.*{message}"):
                    compile_circuit(
                        f"{kind} leg {{ {body} }}\nmain {{ leg(in, out, 0); }}"
                    )

    def test_battery_and_three_terminal_divider(self):
        example = (
            Path(__file__).resolve().parent / "fixtures" / "refactor" / "circuit_method.ckt"
        )
        source = example.read_text(encoding="utf-8")
        result = solve_input(source)
        self.assertAlmostEqual(result.readings[0][2].real, 3.75)
        self.assertEqual(
            [(p.p, p.n) for p in result.circuit.components],
            [("vin", "out"), ("out", "0"), ("vin", "0")],
        )
        self.assertIsNotNone(compact(compile_circuit(source, render_only=True)))

    def test_nested_instances_keep_wires_and_parts_local(self):
        source = """
        V1 in 0 5
        series leg(a, b) { R1 = 1k; }
        circuit section(a, b, g) {
            leg(a, tap);
            R2 tap g 1k;
            op1(tap, b, b);
            measure = V tap g;
        }
        circuit filter(a, b, g) { section(a, b, g); }
        main { filter(in, x, 0); filter(in, y, 0); }
        """
        result = solve_input(source)
        network = result.network
        self.assertEqual(len(set(network.names)), 7)
        taps = [node for node in network.nodes if node.endswith(".tap")]
        self.assertEqual(len(taps), 2)
        for node in (*taps, "x", "y"):
            self.assertAlmostEqual(network.voltage[network.nodes.index(node)].real, 2.5)
        self.assertEqual([reading[2].real for reading in result.readings], [2.5, 2.5])

    def test_no_argument_method_and_source_units(self):
        result = solve_input("""
        circuit stage() {
            V1(5 Vpk) between(in, 0);
            R1(10) between(in, 0);
        }
        main { stage(); }
        """)
        self.assertEqual(result.circuit.mode, "ac")
        self.assertAlmostEqual(abs(result.network.voltage[1]), 5 / (2**0.5))

    def test_wrong_arity_and_recursive_call_report_source_lines(self):
        for source, message in (
            (
                "circuit load(a,b) { R1(1k) between(a,b); }\nmain { load(in); }",
                "2 wire arguments",
            ),
            ("circuit loop(a) { loop(a); }\nmain { loop(in); }", "recursively"),
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(CircuitError, f"Line [12].*{message}"):
                    compile_circuit(source)
