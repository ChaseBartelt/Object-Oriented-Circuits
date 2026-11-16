"""Check that the simpler views preserve component pins and net identity."""

import unittest
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from random import Random

from circuitlab.math.analysis import solve_input
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.gates import default_gate_layout, gate_preview
from circuitlab.model.graph import Circuit, Component
from circuitlab.layout.automatic import compact, grounded_bridge
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import connected_layout, fast_layout, schematic_layout
from circuitlab.layout.editing import ground_attachment, move_ground
from circuitlab.layout.wire_edit import clean_point
from circuitlab.layout.wiring import labeled, signal_flow


def intersect(a, b, c, d):
    return all(
        max(min(a[i], b[i]), min(c[i], d[i]))
        <= min(max(a[i], b[i]), max(c[i], d[i])) + 1e-8
        for i in (0, 1)
    )


class SchematicWiring(unittest.TestCase):
    def test_wide_and_layout_adds_matching_input_rows_and_keeps_the_output_stage(self):
        self.assertIsNone(default_gate_layout("and", 2))
        original = default_gate_layout("and", 3)
        for count in (3, 4, 5, 8, 32):
            with self.subTest(count=count):
                circuit, boundary = gate_preview("and", count)
                drawing = default_gate_layout("and", count)
                self.assertEqual(set(drawing.boundary), set(boundary))
                self.assertTrue(preserves_nets(circuit, drawing))
                for net in ("vcc", "y"):
                    self.assertEqual(drawing.boundary[net], original.boundary[net])
                for part in ("RC1_and_1", "RC2_and_1", "CNode1_and_1", "CNode2_and_1", "Q2_1_and_1"):
                    self.assertEqual(drawing.positions[part], original.positions[part])
                input_positions = [drawing.boundary[pin] for pin in boundary[2:-1]]
                self.assertEqual(len(input_positions), count)
                self.assertTrue(all(x == input_positions[0][0] for x, _ in input_positions))
                rows = [drawing.positions[f"Q1_{index}_and_1"] for index in range(1, count + 1)]
                self.assertEqual([y for _, y in rows], [y for _, y in input_positions])
                row_gaps = {upper[1] - lower[1] for upper, lower in zip(rows, rows[1:])}
                self.assertEqual(len(row_gaps), 1)
                self.assertGreater(next(iter(row_gaps)), 0)
                self.assertLess(drawing.boundary["gnd"][1], rows[-1][1])

    def test_nand_layout_keeps_the_output_rail_above_a_row_for_each_input(self):
        self.assertIsNone(default_gate_layout("nand", 1))
        original = default_gate_layout("nand", 2)
        for count in (2, 3, 4, 8, 32):
            with self.subTest(count=count):
                circuit, boundary = gate_preview("nand", count)
                drawing = default_gate_layout("nand", count)
                self.assertEqual(set(drawing.boundary), set(boundary))
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertFalse(drawing.crossings)
                for net in ("vcc", "y"):
                    self.assertEqual(drawing.boundary[net], original.boundary[net])
                for name in ("RC1_nand_1", "CNode1_nand_1"):
                    self.assertEqual(drawing.positions[name], original.positions[name])
                for index, pin in enumerate(boundary[2:-1], 1):
                    _, y = drawing.boundary[pin]
                    self.assertLess(y, drawing.boundary["y"][1])
                    self.assertEqual(drawing.positions[f"RB1_{index}_nand_1"][1], y)
                    self.assertEqual(drawing.positions[f"Q1_{index}_nand_1"][1], y)
                self.assertLess(drawing.boundary["gnd"][1], drawing.boundary[boundary[-2]][1])

    def test_or_layout_adds_parallel_rows_below_the_same_inverter(self):
        self.assertIsNone(default_gate_layout("or", 1))
        original = default_gate_layout("or", 2)
        for count in (2, 3, 4, 8, 32):
            with self.subTest(count=count):
                circuit, boundary = gate_preview("or", count)
                drawing = default_gate_layout("or", count)
                self.assertEqual(set(drawing.boundary), set(boundary))
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertEqual(len(drawing.grounds), count + 3)
                for name in ("RC1_or_1", "RC2_or_1", "CNode1_or_1", "CNode2_or_1", "Q2_1_or_1"):
                    self.assertEqual(drawing.positions[name], original.positions[name])
                for index, pin in enumerate(boundary[2:-1], 1):
                    _, y = drawing.boundary[pin]
                    self.assertLess(y, drawing.positions["Q2_1_or_1"][1])
                    self.assertEqual(drawing.positions[f"RB1_{index}_or_1"][1], y)
                    self.assertEqual(drawing.positions[f"Q1_{index}_or_1"][1], y)

    def test_nor_layout_keeps_the_capacitor_above_the_shared_collector_rail(self):
        original = default_gate_layout("nor", 1)
        for count in (1, 2, 4, 8, 32):
            with self.subTest(count=count):
                circuit, boundary = gate_preview("nor", count)
                drawing = default_gate_layout("nor", count)
                self.assertEqual(set(drawing.boundary), set(boundary))
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertFalse(drawing.crossings)
                self.assertEqual(len(drawing.grounds), count + 1)
                self.assertEqual(drawing.positions["RC1_nor_1"], original.positions["RC1_nor_1"])
                _, cap_bottom, cap_top, _ = next(s for s in drawing.symbols if s[0].kind == "C")
                self.assertEqual(cap_bottom[0], cap_top[0])
                self.assertGreater(cap_top[1], drawing.boundary["y"][1])
                for index, pin in enumerate(boundary[2:-1], 1):
                    _, y = drawing.boundary[pin]
                    self.assertLess(y, drawing.boundary["y"][1])
                    self.assertEqual(drawing.positions[f"RB1_{index}_nor_1"][1], y)
                    self.assertEqual(drawing.positions[f"Q1_{index}_nor_1"][1], y)

    def test_gate_ground_moves_with_its_port_and_trims_the_capacitor_lead(self):
        circuit, boundary = gate_preview("and", 2)
        drawing = fast_layout(circuit, component_labels(circuit), boundary=boundary)
        ground = drawing.boundary["gnd"]
        capacitor_pin = next(
            clean_point(b) for part, _, b, _ in drawing.symbols if part.name == "CNode1_and_1"
        )
        self.assertEqual(ground_attachment(drawing, ground), capacitor_pin)
        offset = tuple(pin - start for pin, start in zip(capacitor_pin, ground))
        moved = move_ground(circuit, drawing, ground, offset)

        self.assertEqual(moved.boundary["gnd"], capacitor_pin)
        self.assertIn(capacitor_pin, moved.grounds)
        self.assertEqual(len(moved.wires["gnd"]), len(drawing.wires["gnd"]) - 1)
        self.assertTrue(preserves_nets(circuit, moved))

    def test_restore_trims_a_saved_stub_at_a_previous_gate_ground_position(self):
        circuit, boundary = gate_preview("and", 2)
        original = fast_layout(circuit, component_labels(circuit), boundary=boundary)
        ground = original.boundary["gnd"]
        moved = move_ground(circuit, original, ground, (2, 0))
        x, y = clean_point(ground)
        stale = (x, y + 0.6)
        saved = replace(
            moved, boundary={**moved.boundary, "gnd": stale},
            wires={**moved.wires, "gnd": [*moved.wires["gnd"], ((x, y), stale)]},
        )
        self.assertTrue(preserves_nets(circuit, saved))
        restored = restore_layout(pack_layout(saved), circuit, boundary)

        self.assertEqual(restored.grounds, moved.grounds)
        self.assertEqual(restored.boundary, moved.boundary)
        self.assertEqual(restored.wires, moved.wires)

    def test_source_rail_is_straight_above_multiple_loads(self):
        circuit = compile_circuit("""
            Vexc EXC 0 10
            Itrim 0 TRIM 250u
            R1 EXC LEFT 1k
            R2 LEFT 0 2.2k
            R3 EXC RIGHT 1.2k
            R4 RIGHT 0 1.8k
            Rcross LEFT RIGHT 3.3k
            Rtrim TRIM RIGHT 820
            Rbleed TRIM 0 15k
            Rbalance EXC RIGHT 22k
        """)
        for mode in ("auto", "routed"):
            with self.subTest(mode=mode):
                drawing = schematic_layout(circuit, component_labels(circuit), mode)
                self.assertTrue(preserves_nets(circuit, drawing))
                supply = next(a for part, a, _, _ in drawing.symbols if part.name == "Vexc")
                (start, end), *drops = drawing.wires["EXC"]
                self.assertEqual(start, supply)
                self.assertEqual(end[1], supply[1])
                self.assertGreater(end[0], supply[0])
                self.assertEqual(sum(a[0] == b[0] for a, b in drops), 3)
                for a, b in drops:
                    self.assertLessEqual(max(a[1], b[1]), supply[1])
                    self.assertGreater(min(a[0], b[0]), supply[0])
                for part, a, b, _ in drawing.symbols:
                    if part.name in {"R1", "R3", "Rbalance"}:
                        self.assertLess(max(a[1], b[1]), supply[1])
                self.assertFalse(drawing.connectors)
                self.assertLessEqual(sum(map(len, drawing.crossings.values())), 6)

    def test_series_tap_and_floating_battery_leads(self):
        circuit = compile_circuit("""
            V1 vin ret 12
            V2 bias 0 2
            Rref ret 0 100
            Rbias bias 0 2k
            main {
                series(vin, out) {
                    R1 = 1k;
                    wire = tap;
                    R2 = 2k;
                    measure = Ix;
                }
                R3 tap 0 3k;
                R4 out ret 4k;
            }
        """)
        for mode in ("auto", "routed", "labeled"):
            with self.subTest(mode=mode):
                drawing = schematic_layout(circuit, component_labels(circuit), mode)
                self.assertTrue(preserves_nets(circuit, drawing))
                parts = {p.name: (a, b) for p, a, b, _ in drawing.symbols}
                for first, second in (("R1", "R2"), ("R2", "Ix")):
                    a, b = parts[first][1], parts[second][0]
                    if a != b:
                        self.assertEqual(a[1], b[1])
                        self.assertLess(a[0], b[0])
                        if mode != "labeled":
                            self.assertTrue(any(
                                (a, b) in wires or (b, a) in wires
                                for wires in drawing.wires.values()
                            ))
                for net, terminal in zip(("vin", "ret"), parts["V1"]):
                    leads = [
                        b if a == terminal else a
                        for a, b in drawing.wires[net] if terminal in (a, b)
                    ]
                    self.assertTrue(leads)
                    self.assertTrue(all(p[0] >= terminal[0] for p in leads))

    def setUp(self):
        source = Path(__file__).parents[1] / "examples" / "three_way_crossover.ckt"
        self.circuit = compile_circuit(
            source.read_text(encoding="utf-8"), render_only=True
        )

    def test_buffered_branches_and_local_grounds(self):
        drawing = signal_flow(self.circuit)
        self.assertIsNotNone(drawing)
        self.assertCountEqual(
            [part for part, *_ in drawing.symbols], self.circuit.components
        )
        self.assertEqual(len(drawing.grounds), 11)
        self.assertEqual(
            len(
                {drawing.labels[node][1] for node in ("bassOut", "midOut", "trebleOut")}
            ),
            3,
        )
        segments = [
            (net, a, b)
            for net, wires in drawing.wires.items()
            for a, b in wires
            if a != b
        ]
        for (net, a, b), (other, c, d) in combinations(segments, 2):
            if net != other:
                self.assertFalse(intersect(a, b, c, d), (net, other, a, b, c, d))
        pins = list(drawing.pins())
        for net, pos in pins:
            grounded = net == "0" and pos in drawing.grounds
            same_pin = pins.count((net, pos)) > 1
            attached = any(
                intersect(pos, pos, a, b) for a, b in drawing.wires.get(net, ())
            )
            self.assertTrue(grounded or same_pin or attached, (net, pos))

    def test_divider_places_grounded_shunt_below_the_output(self):
        source = """
        V1 = 12 : wire = Vin;
        series test { R1 = 2k; }
        parallel test1 { R2 = 1k; }
        main { test(Vin, out); test1(out, 0); measure = V out 0; }
        port = out 0
        """
        drawing = compact(compile_circuit(source, render_only=True))
        pins = list(drawing.pins())
        self.assertEqual(len(drawing.grounds), 2)
        out = next(pos for node, pos in pins if node == "out")
        grounds = [pos for node, pos in pins if node == "0"]
        self.assertTrue(all(pos in drawing.grounds for pos in grounds))
        self.assertTrue(any(x == out[0] and y < out[1] for x, y in grounds))
        self.assertEqual(pins.count(("out", out)), 2)
        ((node, terminal, *_),) = drawing.connectors
        self.assertEqual(node, "out")
        self.assertGreater(terminal[0], out[0])
        self.assertIn((out, terminal), drawing.wires["out"])
        result = solve_input(source)
        self.assertEqual(result.readings[0][2], 4)
        self.assertAlmostEqual(result.network.equivalent["impedance"].real, 2000 / 3)

    def test_current_source_block_and_grounded_parallel_rail(self):
        source = """
        (battery amp)
            I = 10m
            wire = bus;
        (end battery)
        parallel test {
            R1 = 2k;
            R2 = 4k;
            R3 = 12k;
        }
        main {
            test(bus, 0);
            measure = V bus 0;
        }
        """
        result = solve_input(source)
        supply, *loads = result.circuit.components
        self.assertEqual((supply.kind, supply.p, supply.n), ("I", "0", "bus"))
        self.assertEqual(supply.value, 0.01)
        self.assertEqual([(p.p, p.n) for p in loads], [("bus", "0")] * 3)
        self.assertAlmostEqual(result.readings[0][2].real, 12)
        for current, expected in zip(
            result.network.branch_current, (0.01, 0.006, 0.003, 0.001)
        ):
            self.assertAlmostEqual(current.real, expected)

        for circuit in (
            result.circuit,
            compile_circuit("I1 0 bus 10m\nR1 bus 0 2k\nR2 bus 0 4k\nR3 bus 0 12k"),
        ):
            drawing = compact(circuit)
            self.assertEqual(len(drawing.grounds), 4)
            self.assertEqual(len(drawing.junctions["bus"]), 3)
            self.assertTrue(all(a[0] == b[0] for _, a, b, _ in drawing.symbols))
            _, current_from, current_to, _ = drawing.symbols[0]
            self.assertLess(current_from[1], current_to[1])
            for net, pin in drawing.pins():
                if net == "0":
                    self.assertIn(pin, drawing.grounds)
                else:
                    self.assertTrue(
                        any(pin in segment for segment in drawing.wires["bus"])
                    )

    def test_grounded_series_parallel_branches_share_placement_in_wire_views(self):
        for branch_lengths in ((1, 2), (2, 3, 2), (1, 1, 1, 1, 1)):
            with self.subTest(branch_lengths=branch_lengths):
                lines = ["V1 vin 0 18", "Rfeed vin out 1k"]
                for i, length in enumerate(branch_lengths):
                    nodes = ["out", *(f"b{i}n{j}" for j in range(length - 1)), "0"]
                    for j, (p, n) in enumerate(zip(nodes, nodes[1:])):
                        lines.append(f"R{i}_{j} {p} {n} 1k")
                circuit = compile_circuit("\n".join(lines), render_only=True)
                labels = component_labels(circuit)
                auto = schematic_layout(circuit, labels, "auto")
                all_wires = schematic_layout(circuit, labels, "routed")
                self.assertEqual(auto, all_wires)
                self.assertTrue(preserves_nets(circuit, auto))
                self.assertEqual(len(auto.grounds), 1 + len(branch_lengths))
                for part, a, b, _ in auto.symbols:
                    self.assertEqual(a[1] == b[1], part.name == "Rfeed")

    def test_nested_grounded_branch_keeps_its_intermediate_bus(self):
        circuit = compile_circuit(
            """
            V1 vin 0 18
            Rfeed vin out 1k
            R1 out tap 2k
            R2 tap 0 4k
            R3 tap mid 1k
            R4 mid 0 1k
            R5 out 0 3k
            """,
            render_only=True,
        )
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertEqual(len(drawing.grounds), 4)
        legs = {part.name: (a, b) for part, a, b, _ in drawing.symbols}
        self.assertEqual(legs["R1"][1], legs["R2"][0])
        self.assertEqual(legs["R3"][1], legs["R4"][0])
        self.assertGreater(legs["R5"][0][0], legs["R3"][0][0])

    def test_bridge_draws_both_legs_and_the_midpoint_resistor(self):
        source = """
        (battery V1)
            V = 12;
            wire = w1;
        (end battery)
        circuit full(supply, leftMid, rightMid, ground) {
            R1(1k) between(supply, leftMid);
            R2(2k) between(leftMid, ground);
            R3(2k) between(supply, rightMid);
            R4(1k) between(rightMid, ground);
            R5(1k) between(leftMid, rightMid);
        }
        main {
            full(w1, LEFT, RIGHT, 0);
            measure = V LEFT RIGHT;
        }
        """
        result = solve_input(source)
        self.assertAlmostEqual(result.readings[0][2].real, 12 / 7)
        drawing = compact(compile_circuit(source, render_only=True))
        self.assertCountEqual(
            [part for part, *_ in drawing.symbols], result.circuit.components
        )
        self.assertFalse(drawing.connectors)
        self.assertEqual(len(drawing.grounds), 3)
        left, right = drawing.labels["LEFT"], drawing.labels["RIGHT"]
        self.assertLess(left[0], right[0])
        self.assertEqual(left[1], right[1])
        bridge = next(
            (part, a, b)
            for part, a, b, _ in drawing.symbols
            if part.name == "R5_full_1"
        )
        self.assertEqual(bridge[1:], (left, right))
        pins = list(drawing.pins())
        self.assertEqual(pins.count(("LEFT", left)), 3)
        self.assertEqual(pins.count(("RIGHT", right)), 3)
        self.assertTrue(all(pos in drawing.grounds for net, pos in pins if net == "0"))
        self.assertTrue(
            all(
                any(pos in segment for segment in drawing.wires["w1"])
                for net, pos in pins
                if net == "w1"
            )
        )

    def test_bridge_layout_rejects_a_parallel_shunt_in_place_of_the_link(self):
        circuit = compile_circuit(
            "V1 TOP 0 12\nR1 TOP LEFT 1k\nR2 LEFT 0 2k\n"
            "R3 TOP RIGHT 2k\nR4 RIGHT 0 1k\nR5 LEFT 0 1k",
            render_only=True,
        )
        self.assertIsNone(grounded_bridge(circuit.components, circuit.components[0]))

    def test_labeled_connectors_match_resolved_nodes(self):
        drawing = labeled(self.circuit, component_labels(self.circuit))
        self.assertEqual(set(drawing.wires), set(self.circuit.nodes))
        labels = {}
        for net, anchor, label, *_ in drawing.connectors:
            self.assertEqual(labels.setdefault(label, net), net)
            self.assertTrue(any(anchor in (a, b) for a, b in drawing.wires[net]))
        self.assertEqual(len(drawing.grounds), 11)

    def test_buffered_rc_with_parallel_load(self):
        source = """
        (battery V1)
            Vdc = 4
            Vpk = 3
            f = 900
            wire = vin
        (end battery)

        series feed(input, output) { R1 = 1k; }
        parallel load(input, ground) { C1 = 1u; R2 = 10k; }

        main {
            feed(vin, out);
            load(out, 0);
            op1(out, buffered, buffered);
            measure = V buffered 0;
        }
        """
        circuit = compile_circuit(source, render_only=True)
        drawing = signal_flow(circuit)
        self.assertIsNotNone(drawing)
        self.assertCountEqual(
            [part for part, *_ in drawing.symbols], circuit.components
        )
        self.assertFalse(drawing.connectors)
        self.assertEqual(set(drawing.labels), {"vin", "out", "buffered"})
        shunts = [
            part for part in circuit.components if {part.p, part.n} == {"0", "out"}
        ]
        self.assertCountEqual([part.kind for part in shunts], ["R", "C"])
        for net, pin in drawing.pins():
            connected = any(
                intersect(pin, pin, a, b) for a, b in drawing.wires.get(net, ())
            )
            self.assertTrue(
                connected or net == "0" and pin in drawing.grounds, (net, pin)
            )
        result = solve_input(source)
        self.assertAlmostEqual(result.readings[0][2].real, 3.6363636)
        self.assertAlmostEqual(abs(result.readings[1][2]), 0.36822975)

    def test_auto_routes_an_unrecognized_feedback_circuit(self):
        circuit = compile_circuit(
            """
            V1 A 0 1
            V2 B 0 4
            R1 A SUM 10k
            R2 B SUM 20k
            R3 OUT SUM 10k
            R4 OUT 0 10k
            R5 A B 30k
            main { op1(0, SUM, OUT); }
            """,
            render_only=True,
        )
        self.assertIsNone(compact(circuit))
        self.assertIsNone(signal_flow(circuit))
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assertFalse(drawing.connectors)
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertTrue(
            all(a[0] == b[0] for part, a, b, _ in drawing.symbols if part.is_source)
        )

    def test_summer_puts_both_batteries_on_the_left_and_feedback_above(self):
        source = """
        battery V1 { V = 1; wire = in1; }
        battery V2 { V = 4; wire = in2; }
        main {
            R1(10k) between(in1, sum);
            R2(20k) between(in2, sum);
            R3(10k) between(out, sum);
            R4(10k) between(out, 0);
            op1(0, sum, out);
            measure = V out 0;
        }
        """
        result = solve_input(source)
        self.assertAlmostEqual(result.readings[0][2], -3)
        circuit = result.circuit
        for mode in ("auto", "routed", "labeled"):
            drawing = schematic_layout(circuit, component_labels(circuit), mode)
            self.assertTrue(preserves_nets(circuit, drawing))
            batteries = [(a, b) for part, a, b, _ in drawing.symbols if part.is_source]
            self.assertEqual(batteries[0][0][0], batteries[1][0][0])
            self.assertTrue(all(a[0] == b[0] and a[1] > b[1] for a, b in batteries))
            self.assertTrue(
                all(
                    batteries[0][0][0] < a[0]
                    for part, a, _, _ in drawing.symbols
                    if not part.is_source
                )
            )
            if mode != "labeled":
                self.assertGreater(
                    drawing.positions["R3"][1], drawing.positions["op1"][1]
                )

    def test_late_source_stays_near_its_stage(self):
        circuit = compile_circuit(
            """
        battery Vin { V = 12; wire = in; }
        main {
            R1 in first 1k;
            R2 first next 2k;
            R3 next out 1k;
            R4 out 0 1k;
            battery Vbias { V = 2; wire = out; }
        }
        """,
            render_only=True,
        )
        for mode in ("auto", "routed", "labeled"):
            drawing = schematic_layout(circuit, component_labels(circuit), mode)
            self.assertTrue(preserves_nets(circuit, drawing))
            positions = {
                part.label: (a, b)
                for part, a, b, _ in drawing.symbols
                if part.is_source
            }
            self.assertTrue(all(a[0] == b[0] for a, b in positions.values()))
            self.assertLess(positions["Vin"][0][0], positions["Vbias"][0][0])

    def test_auto_reduces_congestion_with_matching_connectors(self):
        source = Path(__file__).parents[1] / "examples" / "three_band_mixer.ckt"
        circuit = compile_circuit(source.read_text(encoding="utf-8"), render_only=True)
        labels = component_labels(circuit)
        auto = schematic_layout(circuit, labels, "auto")
        routed = schematic_layout(circuit, labels, "routed")
        self.assertTrue(auto.connectors)
        self.assertFalse(routed.connectors)
        self.assertLess(
            sum(map(len, auto.crossings.values())),
            sum(map(len, routed.crossings.values())) / 2,
        )
        self.assertTrue(preserves_nets(circuit, auto))
        self.assertTrue(preserves_nets(circuit, routed))

        net, pos, _, left, below = auto.connectors[0]
        auto.connectors[0] = (net, pos, "Different name", left, below)
        self.assertFalse(preserves_nets(circuit, auto))

    def test_generated_topologies_preserve_nets_in_all_modes(self):
        nodes = ("0", "input", "left", "right", "@g1_1", "N1", "n1", "output", "island")
        for seed in range(20):
            rng = Random(seed)
            parts = [Component("V1", "V", "input", "0", 1)]
            for i in range(18):
                kind = rng.choice("RCLIO")
                p, n, plus, minus = rng.choices(nodes, k=4)
                if kind == "O":
                    parts.append(
                        Component(f"U{i}", kind, p, "0", in_plus=plus, in_minus=minus)
                    )
                else:
                    parts.append(Component(f"{kind}{i + 1}", kind, p, n, 1))
            rng.shuffle(parts)
            circuit = Circuit("dc", components=parts)
            labels = component_labels(circuit)
            for mode, drawing in (
                ("auto", connected_layout(circuit, labels, simplify=True)),
                ("routed", connected_layout(circuit, labels)),
                ("labeled", labeled(circuit, labels)),
            ):
                with self.subTest(seed=seed, mode=mode):
                    self.assertTrue(preserves_nets(circuit, drawing))
