"""Bulk grid alignment retains nets, saved drawings, exports and undo behavior."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.exporters.ltspice import export_hierarchy, export_schematic
from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.gates import default_gate_layout, gate_preview
from circuitlab.layout.geometry import Layout
from circuitlab.layout.grid import align_layout
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import pack_layout, read_placement, restore_layout
from circuitlab.layout.routing import fast_layout
from circuitlab.math.report import component_labels
from circuitlab.viewer.schematic import SCALE, SchematicView
from tests.exporters.test_ltspice import read_connections

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class GridAlignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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
        circuit = compile_circuit("R1(1k) between(a,b);\nC1(1n) between(c,d);", render_only=True)
        original = fast_layout(circuit, component_labels(circuit))
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.draw(circuit, layout=original)
        self.assertIs(view.layout, original)
        view.clear()
        view.draw(circuit)
        self.assert_grid(circuit, view.layout)
        for part, a, b, _ in view.layout.symbols:
            self.assertLessEqual(abs(b[0] - a[0]), 4.3)
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
