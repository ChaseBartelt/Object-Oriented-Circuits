"""All eight amplifier contacts survive routing, rotation and persistence."""

import unittest
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import clear_package_wires, preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.layout.shorten import clear_of_bodies, refine_automatic_layout, wire_length
from circuitlab.math.report import component_labels


class AudioLayoutTest(unittest.TestCase):
    def assert_stacked_packages(self, circuit, drawing, count):
        packages = [(a, b) for part, a, b, _ in drawing.symbols if part.kind == "H"]
        self.assertEqual(len(packages), count)
        self.assertEqual(len({a[0] for a, _ in packages}), 1)
        self.assertEqual(len({b[0] for _, b in packages}), 1)
        self.assertEqual(len({a[1] for a, _ in packages}), count)
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertTrue(clear_package_wires(drawing))
        edges = [edge for route in drawing.wires.values() for edge in route]
        self.assertTrue(clear_of_bodies(edges, drawing))

    def assert_short_passive_leads(self, drawing):
        for part, a, b, _ in drawing.symbols:
            if part.kind not in {"R", "C"}:
                continue
            self.assertTrue(a[0] == b[0] or a[1] == b[1], part.name)
            span = abs(a[0] - b[0]) + abs(a[1] - b[1])
            self.assertLessEqual(span, 2 if part.kind == "R" else 1, part.name)

    def test_auto_layout_aligns_independent_package_feedback_stages(self):
        source = """
        chip Amp(trim1, minus, plus, vn, nc, out, vp, trim2) {
            op1(plus, minus, out, vp, vn);
        }
        Vpos(5) between(vp, 0);
        Vneg(-5) between(vn, 0);
        main {
            Vleft(1) between(il, 0);
            Vright(1) between(ir, 0);
            Ril(10k) between(il, ml);
            Rir(10k) between(ir, mr);
            Rfl(10k) between(ol, ml);
            Rfr(10k) between(oright, mr);
            Cfl(47p) between(ol, ml);
            Cfr(47p) between(oright, mr);
            Rll(10k) between(ol, 0);
            Rlr(10k) between(oright, 0);
            Amp(tl, ml, 0, vn, nl, ol, vp, tr);
            Amp(ul, mr, 0, vn, nr, oright, vp, ur);
        }
        """
        _, circuit, _, _ = next(hood_levels(compile_circuit(source, render_only=True), set()))
        labels = component_labels(circuit)
        drawing = schematic_layout(circuit, labels, "auto")
        routed = schematic_layout(circuit, labels, "routed")
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertTrue(preserves_nets(circuit, routed))
        self.assertTrue(clear_of_bodies([edge for edges in drawing.wires.values() for edge in edges], drawing))
        self.assertLess(sum(map(len, drawing.crossings.values())), sum(map(len, routed.crossings.values())))
        self.assertLess(wire_length(drawing), wire_length(routed))
        self.assertFalse(drawing.connectors)
        for supply in ("vp", "vn"):
            self.assertIn(supply, drawing.wires)
        packages = [(part, a, b) for part, a, b, _ in drawing.symbols if part.kind == "H"]
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0][1][0], packages[1][1][0])
        self.assertNotEqual(packages[0][1][1], packages[1][1][1])
        for name in ("Rll", "Rlr"):
            _, a, b, _ = next(symbol for symbol in drawing.symbols if symbol[0].name == name)
            self.assertEqual(a[0], b[0])

    def test_stereo_headphone_channels_keep_aligned_inputs_and_feedback(self):
        source = (Path(__file__).resolve().parents[2] / "examples" / "advanced" / "synth"
                  / "HeadphoneJack_DaisySeed3.ckt")
        compiled = compile_circuit(source.read_text(encoding="utf-8"), path=source, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assert_stacked_packages(circuit, drawing, 2)
        aligned = align_layout(circuit, drawing, prune_cycles=True, keep_package_clearance=True)
        self.assertIsNotNone(aligned)
        self.assert_stacked_packages(circuit, aligned, 2)
        drawing = refine_automatic_layout(circuit, aligned)
        self.assert_stacked_packages(circuit, drawing, 2)
        self.assert_short_passive_leads(drawing)
        # The example now includes parallel input capacitors and output isolation
        # resistors. Their wiring should still stay well below the generic fallback.
        self.assertLess(wire_length(drawing), 320)
        self.assertFalse(drawing.connectors)

        symbols = {part.name: (a, b) for part, a, b, _ in drawing.symbols}
        packages = [a for part, a, _, _ in drawing.symbols if part.kind == "H"]
        for index, channel in enumerate(("Left", "Right"), 1):
            suffix = f"_AudioChannel_{index}"
            signal_y = symbols[f"V{channel.lower()}"][0][1]
            self.assertTrue(all(point[1] == signal_y for point in symbols[f"Rin{suffix}"]))
            capacitors = [symbols[f"C{number}{suffix}"] for number in (1, 2, 3)]
            self.assertEqual(len({a[1] for a, _ in capacitors}), 3)
            self.assertEqual(len({(a[0], b[0]) for a, b in capacitors}), 1)
            for a, b in capacitors:
                self.assertEqual(a[1], b[1])
            a, b = symbols[f"Rfeedback{suffix}"]
            self.assertEqual(a[1], b[1])
            self.assertIn(f"Routput{suffix}", symbols)
            self.assertIn(f"Rload{channel}", symbols)
            for name, compare in (("Cpositive", self.assertGreater), ("Cnegative", self.assertLess)):
                a, b = symbols[name + suffix]
                compare(a[0], packages[0][0])
                self.assertGreater(a[1], packages[0][1])
                self.assertEqual(a[0], b[0])

    def test_three_renamed_packages_stack_with_parallel_inputs_and_series_loads(self):
        source = """
        chip AnalogCell(trim1, minus, plus, vn, nc, out, vp, trim2) {
            op1(plus, minus, out, vp, vn);
        }
        Vpos(9) between(vp,0);
        Vneg(-9) between(vn,0);
        main {
            for (int i=0; i<3; i++) {
                Vsig*{i}(1) between(signal*{i},0);
                Ca*{i}(220n) between(signal*{i},coupled*{i});
                Cb*{i}(330n) between(signal*{i},coupled*{i});
                Rin*{i}(10k) between(coupled*{i},minus*{i});
                Rfeedback*{i}(10k) between(out*{i},minus*{i});
                Cfeedback*{i}(47p) between(out*{i},minus*{i});
                Routput*{i}(100) between(out*{i},jack*{i});
                Rload*{i}(20k) between(jack*{i},0);
                AnalogCell(trimA*{i},minus*{i},0,vn,nc*{i},out*{i},vp,trimB*{i});
            }
        }
        """
        compiled = compile_circuit(source, render_only=True)
        _, circuit, _, _ = next(hood_levels(compiled, set()))
        drawing = schematic_layout(circuit, component_labels(circuit), "auto")
        self.assert_stacked_packages(circuit, drawing, 3)
        aligned = align_layout(circuit, drawing, prune_cycles=True, keep_package_clearance=True)
        self.assertIsNotNone(aligned)
        self.assert_stacked_packages(circuit, aligned, 3)
        drawing = refine_automatic_layout(circuit, aligned)
        self.assert_stacked_packages(circuit, drawing, 3)
        self.assert_short_passive_leads(drawing)
        self.assertFalse(drawing.connectors)
        self.assertEqual(len(drawing.symbols), len(circuit.components))

    def test_every_style_preserves_all_eight_pin_nets_without_model_symbols(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,gnd,g1,g8,bypass);", render_only=True)
        for style in ("auto", "routed", "labeled", "fast"):
            with self.subTest(style=style):
                labels = component_labels(circuit)
                drawing = fast_layout(circuit, labels) if style == "fast" else schematic_layout(circuit, labels, style)
                self.assertTrue(preserves_nets(circuit, drawing))
                self.assertEqual(len(drawing.symbols), 1)
                self.assertEqual(len(list(drawing.pins())), 8)
                self.assertEqual(labels, {"op1": ["op1"]})
                self.assertEqual([n for n, _ in symbol_pins(*drawing.symbols[0])],
                                 ["plus", "minus", "out", "vcc", "gnd", "g1", "g8", "bypass"])

    def test_rotation_move_and_saved_layout_preserve_each_contact(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,0,g1,g8,bypass);", render_only=True)
        drawing = align_layout(circuit, fast_layout(circuit, component_labels(circuit)))
        self.assertIsNotNone(drawing)
        for turns in range(4):
            with self.subTest(turns=turns):
                moved = move_symbol(circuit, drawing, "op1", (2, 2), rotate=turns)
                self.assertIsNotNone(moved)
                self.assertTrue(preserves_nets(circuit, moved))
                restored = restore_layout(pack_layout(moved), circuit, ())
                self.assertIsNotNone(restored)
                self.assertEqual(list(restored.pins()), list(moved.pins()))
