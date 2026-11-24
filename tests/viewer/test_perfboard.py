"""The perfboard remains a placement reference under the electrical schematic."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.geometry import ChipPackage, routed_pins
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.perfboard import valid_perfboard_layout
from circuitlab.layout.persistence import pack_layout
from circuitlab.viewer.schematic import SchematicView, point


SOURCE = """
perf[A-R,1-24] exclude[V1];
chip Amplifier(trim1, minus, plus, vn, nc, out, vp, trim2) {
    op1(plus, minus, out, vp, vn);
}
V1(5V) between(supply,0);
main {
    Rin(10k) between(supply,sense);
    Rfeedback(10k) between(out,sense);
    Amplifier(trim1,sense,0,0,nc,out,supply,trim2);
    Rload(10k) between(out,0);
}
"""


class PerfboardViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        self.view = SchematicView()
        self.view.resize(1000, 750)
        self.view.draw(self.circuit)
        self.app.processEvents()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def items(self, kind):
        return [item for item in self.view.scene().items() if item.data(0) == kind]

    def test_green_board_has_labeled_rows_columns_and_one_marker_per_hole(self):
        board, = self.items("perfboard")
        color = board.brush().color()
        self.assertGreater(color.green(), color.red())
        self.assertGreater(color.green(), color.blue())
        self.assertLess(board.zValue(), 0)
        holes, = self.items("perfboard-hole")
        self.assertEqual(holes.data(1), 18 * 24)
        self.assertTrue(holes.path().contains(point((0, 0))))
        self.assertTrue(holes.path().contains(point((23, -17))))
        labels = {item.text() for item in self.items("perfboard-label")}
        self.assertTrue({"A", "R", "1", "24"} <= labels)

    def test_hiding_the_schematic_grid_keeps_the_board_visible(self):
        self.view.set_grid_enabled(False)
        self.assertFalse(self.view.grid_enabled)
        self.assertTrue(all(item.isVisible() for item in self.items("perfboard")))
        self.assertEqual(sum(item.data(1) for item in self.items("perfboard-hole")), 18 * 24)

    def test_empty_board_declaration_still_draws_the_board(self):
        self.view.draw(compile_circuit("perf[A-C,1-4];", render_only=True))
        self.assertEqual(len(self.items("perfboard")), 1)
        holes, = self.items("perfboard-hole")
        self.assertEqual(holes.data(1), 12)

    def test_chip_pins_use_distinct_holes_and_excluded_source_stays_visible(self):
        _, circuit, _, _ = next(hood_levels(self.circuit, set()))
        self.assertEqual(len(self.view.layout.symbols), len(circuit.components))
        board, = self.items("perfboard")
        symbols = self.view.layout.symbols
        source = next(symbol for symbol in symbols if symbol[0].name == "V1")
        center = tuple((a + b) / 2 for a, b in zip(source[1], source[2]))
        self.assertFalse(board.sceneBoundingRect().contains(point(center)))
        for symbol in symbols:
            if symbol[0].kind == "R":
                pins = [at for _, at in routed_pins(symbol, self.view.layout.rotations.get(symbol[0].name, 0),
                                                   self.view.layout.pin_grid)]
                first, last = pins
                self.assertTrue(first[0] == last[0] or first[1] == last[1])
                self.assertGreaterEqual(abs(first[0] - last[0]) + abs(first[1] - last[1]), 2)
            if not isinstance(symbol[0], ChipPackage):
                continue
            pins = [at for _, at in routed_pins(symbol, self.view.layout.rotations.get(symbol[0].name, 0),
                                               self.view.layout.pin_grid)]
            self.assertEqual(len(pins), len(set(pins)))
            for x, y in pins:
                self.assertAlmostEqual(x, round(x))
                self.assertAlmostEqual(y, round(y))
                self.assertTrue(0 <= x <= 23 and -17 <= y <= 0)

    def test_drag_uses_physical_pin_anchor_and_warns_outside_board_until_fixed(self):
        source = """perf[A-R,1-24];
        chip Header(a,b,c,d,e,f,g,h) { model = unavailable; }
        main { Header(n1,n2,n3,n4,n5,n6,n7,n8); }
        """
        self.view.draw(compile_circuit(source, render_only=True))
        self.app.processEvents()
        original = self.view.layout
        part, a, b, _ = original.symbols[0]
        center = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        start = self.view.mapFromScene(point(center))
        self.view.start_drag("hood", part.name, (None, "auto"), start, only_symbol=True)
        self.assertEqual(self.view.drag.grid_step, 1)
        self.assertTrue(all(value == round(value) for value in self.view.drag.grid_anchor))
        end = self.view.mapFromScene(point((center[0] + 1, center[1] - 1)))
        self.view.preview_drag(end)
        self.assertFalse(self.view.drag.blocked)
        self.assertEqual(self.view.layout.positions[part.name], (a[0] + 1, a[1] - 1))
        accepted = self.view.layout
        end = self.view.mapFromScene(point((center[0] - 50, center[1])))
        self.view.preview_drag(end)
        self.assertFalse(self.view.drag.blocked)
        outside = self.view.layout
        self.assertNotEqual(outside, accepted)
        self.assertTrue(valid_perfboard_layout(self.view.levels[None, "auto"], accepted))
        self.view.drag = None
        self.view._commit_layout_edit((None, "auto"), outside, selected=None)
        self.assertTrue(self.view.layout_issues)
        self.view.undo()
        self.assertEqual(self.view.layout, original)
        self.assertFalse(self.view.layout_issues)

    def test_saved_board_placements_restore_with_physical_package_pitch(self):
        saved = pack_layout(self.view.layout)
        expected = self.view.layout.positions.copy()
        self.view.clear()
        self.view.placement_records[None, "auto"] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assertIn((None, "auto"), self.view.manual_layouts)
        self.assertEqual(self.view.layout.positions, expected)
        self.assertTrue(valid_perfboard_layout(self.view.levels[None, "auto"], self.view.layout))

    def test_moving_one_resistor_keeps_other_parts_and_unrelated_wires_stable(self):
        original = self.view.layout
        symbol = next(symbol for symbol in original.symbols if symbol[0].name == "Rin")
        part, a, b, _ = symbol
        center = tuple((x + y) / 2 for x, y in zip(a, b))
        start = self.view.mapFromScene(point(center))
        self.view.start_drag("component", part.name, (None, "auto"), start, only_symbol=True)
        end = self.view.mapFromScene(point((center[0] - 2, center[1] - 1)))
        self.view.preview_drag(end)
        preview = self.view.layout
        self.assertFalse(self.view.drag.blocked)
        self.assertEqual(preview.positions[part.name], (a[0] - 2, a[1] - 1))
        self.assertEqual(preview.rotations, original.rotations)
        for other in original.symbols:
            if other[0].name != part.name:
                self.assertIn(other, preview.symbols)
        for net, edges in original.wires.items():
            if net not in part.terminal_nets:
                self.assertEqual(preview.wires[net], edges)
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1]
                            for edges in preview.wires.values() for a, b in edges))

    def test_rotating_six_pin_chip_keeps_contacts_on_holes_and_roundtrips(self):
        source = """perf[A-R,1-24];
        chip Header(a,b,c,d,e,f) { model = unavailable; }
        main { Header(n1,n2,n3,n4,n5,n6); }
        """
        self.view.draw(compile_circuit(source, render_only=True))
        original = self.view.layout
        name = original.symbols[0][0].name
        for _ in range(4):
            self.view.rotate_component((None, "auto"), name)
            pins = [at for _, at in self.view.layout.pins()]
            self.assertTrue(all(value == round(value) for at in pins for value in at))
        self.assertEqual(self.view.layout.positions, original.positions)
        self.assertEqual(self.view.layout.rotations, original.rotations)
        self.assertFalse(self.view.layout_issues)

    def test_align_and_optimize_keep_board_footprints_on_the_holes(self):
        expected = self.view.layout.positions.copy()
        self.assertTrue(self.view.align_to_grid())
        self.view.optimize_wiring()
        self.assertEqual(self.view.layout.positions, expected)
        self.assertTrue(valid_perfboard_layout(self.view.levels[None, "auto"], self.view.layout))

    def test_board_fit_warning_keeps_the_complete_circuit_editable(self):
        from circuitlab.tooling.session import CircuitSession
        from circuitlab.viewer.window import ViewerWindow

        window = ViewerWindow(CircuitSession(source="perf[A-A,1-1];\nR1(10) between(a,0);"))
        try:
            self.assertTrue(window.render_source())
            self.assertIn("outside the perfboard", window.status.text())
            self.assertEqual(window.issues.count(), 1)
            self.assertEqual(len(window.schematic.layout.symbols), 1)
            self.assertEqual(window.visible_issues[0].severity, "warning")
            self.assertEqual(window.visible_issues[0].line, 1)
            window.compile_source()
            self.assertEqual(len(window.schematic.layout.symbols), 1)
            self.assertTrue(any("outside the perfboard" in issue.message and issue.severity == "warning"
                                for issue in window.visible_issues))
        finally:
            window.placement_dirty = False
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_invalid_board_placement_reopens_and_clears_warning_after_repair(self):
        from dataclasses import replace
        from circuitlab.layout.perfboard import reroute_perfboard
        from circuitlab.tooling.session import CircuitSession
        from circuitlab.viewer.window import ViewerWindow

        window = ViewerWindow(CircuitSession(source="main { R1(10) between(a,0); perf[A-A,1-3]; }"))
        try:
            self.assertTrue(window.render_source())
            view = window.schematic
            key = None, "auto"
            original = view.layout
            part, a, b, top = original.symbols[0]
            symbols = [(part, (a[0] + 4, a[1]), (b[0] + 4, b[1]), top)]
            drawing = replace(original, symbols=symbols, positions={part.name: symbols[0][1]})
            drawing = reroute_perfboard(view.levels[key], drawing, editable=True)
            view._commit_layout_edit(key, drawing, selected=None)
            self.assertTrue(window.layout_issues)
            saved = view.placements()
            view.clear()
            view.placement_records = saved
            self.assertTrue(window.render_source())
            self.assertEqual(view.layout.positions, drawing.positions)
            self.assertTrue(window.layout_issues)
            view._commit_layout_edit(key, original, selected=None)
            self.assertFalse(window.layout_issues)
            self.assertEqual(window.issues.item(0).text(), "No compiler diagnostics.")
            self.assertEqual(window.status.text(), "Schematic rendered.")
        finally:
            window.placement_dirty = False
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_standalone_editor_also_keeps_overflow_visible_with_a_warning(self):
        from circuitlab.viewer.legacy import MainWindow

        window = MainWindow()
        try:
            source = "main { R1(10) between(a,0); perf[A-A,1-1]; }"
            window.editor.setPlainText(source)
            window.compile_source()
            self.assertEqual(len(window.schematic.layout.symbols), 1)
            self.assertEqual(window.layout_issues[0].severity, "warning")
            self.assertIn("outside the perfboard", window.status.text())
            window.editor.setPlainText(source.replace("1-1", "1-3"))
            window.compile_source()
            self.assertFalse(window.layout_issues)
        finally:
            window.placement_dirty = False
            window.editor.document().setModified(False)
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
