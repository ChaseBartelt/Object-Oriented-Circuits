"""Bulk grid alignment retains nets, saved drawings, exports and undo behavior."""
# Grid rounding is accepted only when every terminal and net still agrees with the model.
# Check bulk undo and cases where nearby contacts would otherwise create an unintended short.

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.exporters.ltspice import export_hierarchy, export_schematic
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import clear_package_wires, mark_crossings, preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.gates import default_gate_layout, gate_preview
from circuitlab.layout.geometry import Layout, opamp_body, rotate_point
from circuitlab.layout.grid import align_layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.lead_spacing import shorten_symbol_leads
from circuitlab.layout.persistence import pack_layout, read_placement, restore_layout
from circuitlab.layout.routing import fast_layout
from circuitlab.layout.shorten import refine_automatic_layout, straighten
from circuitlab.layout.wire_edit import merge_strokes
from circuitlab.math.report import component_labels
from circuitlab.viewer.schematic import SCALE, SchematicView
from tests.exporters.test_ltspice import read_connections

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class GridAlignmentTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_automatic_cleanup_prunes_a_redundant_loop_on_one_net(self):
        circuit = compile_circuit("R1(1k) between(in,link);\nR2(1k) between(link,out);", render_only=True)
        first, second = circuit.components
        drawing = Layout(
            symbols=[(first, (-4, 0), (0, 0), "+"), (second, (6, 0), (10, 0), "+")],
            wires={"link": [
                ((0, 0), (6, 0)), ((0, 0), (0, 2)), ((0, 2), (6, 2)), ((6, 2), (6, 0)),
            ]},
        )
        self.assertTrue(preserves_nets(circuit, drawing))
        snapped = align_layout(circuit, drawing)
        self.assert_grid(circuit, snapped)
        self.assertEqual(len(snapped.wires["link"]), 4)
        clean = align_layout(circuit, drawing, prune_cycles=True)
        self.assert_grid(circuit, clean)
        self.assertEqual(clean.wires["link"], [((0, 0), (6, 0))])

    def test_automatic_grid_alignment_leaves_a_clear_square_around_chip_wires(self):
        full = compile_circuit(
            'chip IC(a,b,c,d,e,f,g,h) { R1(1k) between(a,b); }\n'
            'main { IC(a,b,c,d,e,f,g,h); Rleft(1k) between(p,link); Rright(1k) between(link,q); '
            'Rtap(1k) between(u,a); }', render_only=True,
        )
        _, circuit, _, _ = next(hood_levels(full, set()))
        parts = {part.name: part for part in circuit.components}
        package = next(part for part in parts.values() if part.kind == 'H')
        drawing = Layout(
            symbols=[(package, (0, .13), (7, .13), '+'),
                     (parts['Rleft'], (-6, 2.61), (-2, 2.61), '+'),
                     (parts['Rright'], (9, 2.61), (13, 2.61), '+'),
                     (parts['Rtap'], (-6, 1.63), (-2, 1.63), '+')],
            wires={'link': [((-2, 2.61), (9, 2.61))], 'a': [((-2, 1.63), (0, 1.63))]},
        )
        self.assertTrue(preserves_nets(circuit, drawing))
        self.assertTrue(clear_package_wires(drawing))
        self.assertFalse(clear_package_wires(align_layout(circuit, drawing)))

        aligned = align_layout(circuit, drawing, keep_package_clearance=True)
        self.assert_grid(circuit, aligned)
        self.assertTrue(clear_package_wires(aligned))
        refined = refine_automatic_layout(circuit, aligned)
        self.assert_grid(circuit, refined)
        self.assertTrue(clear_package_wires(refined))
        self.assertTrue(preserves_nets(circuit, refined))

    def test_shortcut_keeps_the_required_gap_around_rotated_package_outline(self):
        full = compile_circuit(
            'chip IC(a,b,c,d,e,f,g,h) { R1(1k) between(a,b); }\n'
            'main { IC(a,b,c,d,e,f,g,h); Rleft(1k) between(p,link); Rright(1k) between(link,q); }',
            render_only=True,
        )
        _, circuit, _, _ = next(hood_levels(full, set()))
        parts = {part.name: part for part in circuit.components}
        package = next(part for part in parts.values() if part.kind == 'H')
        start, end = (0, 0), (7, 0)
        draw = Layout(symbols=[(package, start, end, '+')], pin_grid=.25)
        for turns in range(4):
            with self.subTest(turns=turns):
                draw.rotations = {package.name: turns}

                def turn(point):
                    return rotate_point(point, start, end, turns)

                lead = (turn((-2, 1.5)), turn((0, 1.5)))
                near = (turn((-2, 2.25)), turn((9, 2.25)))
                edge = (turn((-2, 2.45)), turn((9, 2.45)))
                self.assertTrue(clear_package_wires(draw, [lead, edge]))
                self.assertFalse(clear_package_wires(draw, [near]))

                route = [turn(point) for point in ((-2, 2.25), (-2, 2.5), (9, 2.5), (9, 2.25))]
                draw.symbols[1:] = [(parts['Rleft'], turn((-6, 2.25)), route[0], '+'),
                                    (parts['Rright'], route[-1], turn((13, 2.25)), '+')]
                draw.wires = {'link': list(zip(route, route[1:]))}
                self.assertTrue(preserves_nets(circuit, draw))
                self.assertTrue(clear_package_wires(draw))
                self.assertIs(straighten(circuit, draw), draw)

    def test_overlapping_wire_strokes_paint_once_and_leave_crossing_gaps(self):
        self.assertEqual(list(merge_strokes([
            ((0, 0), (2, 0)), ((1, 0), (4, 0)),
            ((0, 0), (0, 1.88)), ((0, 2.12), (0, 4)),
        ])), [((0, 0), (4, 0)), ((0, 0), (0, 1.88)), ((0, 2.12), (0, 4))])
        circuit = compile_circuit("R1(1k) between(in,link);\nR2(1k) between(link,out);", render_only=True)
        first, second = circuit.components
        drawing = Layout(
            symbols=[(first, (-4, 0), (0, 0), "+"), (second, (6, 0), (10, 0), "+")],
            wires={"link": [((0, 0), (6, 0)), ((0, 0), (3, 0)), ((2, 0), (6, 0))]},
        )
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=drawing)
        self.assertIs(view.layout, drawing)
        self.assertEqual(len(view.layout.wires["link"]), 3)
        self.assertEqual(view.paths["link"].path().elementCount(), 2)

    def assert_grid(self, circuit, layout):
        self.assertIsNotNone(layout)
        self.assertTrue(preserves_nets(circuit, layout))
        points = [p for _, p in layout.pins()] + layout.grounds + list(layout.boundary.values())
        points += [p for _, p, *_ in layout.connectors]
        points += [p for edges in layout.wires.values() for edge in edges for p in edge]
        for p in points:
            for value in p:
                self.assertAlmostEqual(value / .25, round(value / .25))

    def test_gate_families_align_without_changing_models_or_stored_templates(self):
        for gate, counts in (("not", (1,)), ("buf", (1,)), ("and", (2, 3, 8)),
                             ("nand", (2, 3, 8)), ("or", (2, 3, 8)), ("nor", (2, 3, 8)),
                             ("xor", (2,)), ("xnor", (2,))):
            for count in counts:
                with self.subTest(gate=gate, count=count):
                    circuit, boundary = gate_preview(gate, count)
                    original = default_gate_layout(gate, count) or fast_layout(circuit, component_labels(circuit))
                    saved = pack_layout(original)
                    aligned = align_layout(circuit, original)
                    self.assert_grid(circuit, aligned)
                    self.assertEqual(pack_layout(original), saved)
                    restored = restore_layout(pack_layout(aligned), circuit, boundary)
                    self.assertEqual(list(restored.pins()), list(aligned.pins()))
                    self.assert_grid(circuit, restored)

    def test_three_and_four_terminal_devices_keep_connections_after_rotation_and_export(self):
        sources = ("transQ1(b,c,0,npn);", "op1(plus,minus,out);",
                   "switch1(control,0,left,right,2.5,10,1G);")
        for source in sources:
            circuit = compile_circuit(source, render_only=True)
            original = fast_layout(circuit, component_labels(circuit))
            aligned = align_layout(circuit, original)
            self.assert_grid(circuit, aligned)
            before, _ = export_schematic(circuit, original)
            reference = read_connections(before, {"sw": ((0, 16), (0, 96), (-48, 80), (-48, 32))})
            for turns in range(4):
                with self.subTest(source=source, turns=turns):
                    moved = move_symbol(circuit, aligned, circuit.components[0].name, (2, 2), rotate=turns)
                    self.assertIsNotNone(moved)
                    self.assertTrue(preserves_nets(circuit, moved))
                    for _, pin in moved.pins():
                        self.assertTrue(all(abs(v / .25 - round(v / .25)) < 1e-6 for v in pin))
                    sheet, _ = export_schematic(circuit, moved)
                    actual = read_connections(sheet, {"sw": ((0, 16), (0, 96), (-48, 80), (-48, 32))})
                    self.assertEqual(actual, reference)

    def test_saved_555_aligns_and_exports_all_eight_physical_pins(self):
        path = FIXTURES / "refactor" / "timer555.ckt"
        circuit = compile_circuit(path.read_text(encoding="utf-8-sig"), render_only=True)
        top = next(hood_levels(circuit, set()))[1]
        mode, records = read_placement(path)
        original = restore_layout(records[None, mode], top, ())
        self.assertIsNotNone(original)
        aligned = align_layout(top, original)
        self.assert_grid(top, aligned)
        sheet, files, _ = export_hierarchy(circuit, aligned)
        offsets = {name[:-4]: tuple(tuple(map(int, line.split()[1:3]))
                                   for line in text.splitlines() if line.startswith("PIN "))
                   for name, text in files.items() if name.endswith(".asy")}
        actual = read_connections(sheet, offsets)
        self.assertEqual(actual["X_timer555"][0],
                         ("0", "timing", "out", "reset_n", "control", "timing", "discharge", "supply"))
        # The crowded internal layout must also survive grid rounding.
        _, inner, _, boundary = list(hood_levels(circuit, {"@method:0"}))[-1]
        drawing = fast_layout(inner, component_labels(inner), boundary=boundary)
        self.assert_grid(inner, align_layout(inner, drawing))

    def test_bulk_alignment_is_one_undo_step_including_open_chip_contents(self):
        circuit = compile_circuit("chip Test(a,b) { R1(1k) between(a,b); }\nmain { Test(a,b); }", render_only=True)
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit)
        view.toggle_package("@method:0")
        # Simulate a previously saved, freely positioned layout at each level.
        before = {}
        for key, level in view.levels.items():
            drawing = fast_layout(level, component_labels(level), boundary=view.level_layouts[key].boundary)
            before[key] = drawing
            view.manual_layouts[key] = drawing
        view.redraw({})
        self.assertTrue(view.align_to_grid())
        self.assertEqual(len(view.undo_history), 1)
        for key, level in view.levels.items():
            self.assert_grid(level, view.manual_layouts[key])
            offset = view.level_offsets[key]
            for value in (offset.x() / SCALE, offset.y() / SCALE):
                self.assertAlmostEqual(value / .25, round(value / .25))
        view.undo_placement()
        for key, original in before.items():
            self.assertIs(view.manual_layouts[key], original)

    def test_new_layouts_shorten_leads_and_align_pins_but_old_layouts_are_not_rewritten(self):
        circuit = compile_circuit(
            "R1(1k) between(a,b);\nC1(1n) between(c,d);\nV1(1) between(e,0);",
            render_only=True,
        )
        original = fast_layout(circuit, component_labels(circuit))
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=original)
        self.assertIs(view.layout, original)
        view.clear()
        view.draw(circuit)
        self.assert_grid(circuit, view.layout)
        for part, a, b, _ in view.layout.symbols:
            self.assertLessEqual(abs(b[0] - a[0]) + abs(b[1] - a[1]), {"R": 2.25, "C": 1, "V": 1.75}[part.kind])
        # An aligned layout is stable under repeated clicks.
        saved = pack_layout(view.layout)
        self.assertEqual(pack_layout(align_layout(circuit, view.layout)), saved)
        legacy = pack_layout(original)
        legacy.pop("pin_grid", None)
        restored = restore_layout(legacy, circuit, ())
        self.assertEqual(list(original.pins()), list(restored.pins()))

    def test_alignment_refuses_to_short_adjacent_nets_and_leaves_original_untouched(self):
        circuit = compile_circuit("R1(1k) between(a,b);\nR2(1k) between(c,d);", render_only=True)
        r1, r2 = circuit.components
        original = Layout(symbols=[(r1, (0,.1), (4,.1), "+"), (r2, (0,.11), (4,.11), "+")])
        before = pack_layout(original)
        self.assertTrue(preserves_nets(circuit, original))
        self.assertIsNone(align_layout(circuit, original))
        self.assertEqual(pack_layout(original), before)

    def test_opamp_leads_shorten_around_fixed_triangle_and_supply_contacts(self):
        circuit = compile_circuit("op1(plus,minus,out,vcc,vee);", render_only=True)
        amp, = circuit.components
        start, end = (0, 0), (4.5, 0)
        original = Layout(
            symbols=[(amp, start, end, "+")], positions={"op1": start}, pin_grid=.25,
            boundary={"plus": (-3, .25), "minus": (-3, -.25), "out": (7, 0),
                      "vcc": (2.25, 3), "vee": (2.25, -3)},
        )
        original.wires = {net: [(pin, original.boundary[net])] for net, pin in original.pins()}
        mark_crossings(original)
        shortened = shorten_symbol_leads(circuit, original)
        _, first, last, _ = shortened.symbols[0]
        self.assertEqual((first, last), ((1, 0), (3.5, 0)))
        self.assertEqual(opamp_body(start, end), opamp_body(first, last))
        self.assertEqual(dict(shortened.pins())["vcc"], dict(original.pins())["vcc"])
        self.assertEqual(dict(shortened.pins())["vee"], dict(original.pins())["vee"])
        self.assert_grid(circuit, shortened)

    def test_multiple_blocked_grounds_do_not_try_unrelated_single_ground_repairs(self):
        circuit = compile_circuit(
            "R1(1k) between(a,0);\nR2(1k) between(c,d);\n"
            "R3(1k) between(e,0);\nR4(1k) between(f,h);", render_only=True,
        )
        r1, r2, r3, r4 = circuit.components
        original = Layout(
            symbols=[(r1, (0,.1), (4,.1), "+"), (r2, (4.01,.1), (8.01,.1), "+"),
                     (r3, (0,2.1), (4,2.1), "+"), (r4, (4.01,2.1), (8.01,2.1), "+")],
            grounds=[(4,.1), (4,2.1)],
        )
        self.assertTrue(preserves_nets(circuit, original))
        before = pack_layout(original)
        with patch("circuitlab.layout.grid.move_ground") as repair:
            self.assertIsNone(align_layout(circuit, original))
            repair.assert_not_called()
        self.assertEqual(pack_layout(original), before)


if __name__ == "__main__":
    unittest.main()
