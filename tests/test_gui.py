"""Exercise the normal editor, solve, render, and file workflows through Qt."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QHelpEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip

from circuitlab.gui import MainWindow
from circuitlab.schematic import point

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class ApplicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.editor.document().setModified(False)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_solve_search_and_probe(self):
        win = self.window
        self.assertTrue(win.open_path(EXAMPLES / "voltage_divider.ckt"))
        win.results.search.setText("node out")
        self.assertEqual(win.results.table.rowCount(), 1)
        self.assertEqual(win.results.table.item(0, 2).text(), "4 V")
        win.show_analysis()
        self.assertTrue(win.analysis_window.isVisible())
        win.analysis_window.close()
        label = next(
            item
            for item in win.schematic.scene().items()
            if item.data(0) == "node" and item.data(1) == "out"
        )
        QTest.mouseClick(
            win.schematic.viewport(),
            Qt.MouseButton.LeftButton,
            pos=win.schematic.mapFromScene(label.sceneBoundingRect().center()),
        )
        self.assertIn("V(out) relative to 0 = 4 V", win.status.text())

    def test_spice_button_exports_unsaved_editor_text_without_changing_source_file(
        self,
    ):
        win = self.window
        with TemporaryDirectory() as directory:
            path = Path(directory) / "example.ckt"
            path.write_text("V1 out 0 2\nR1 out 0 1k\n", encoding="ascii")
            win.open_path(path)
            win.editor.setPlainText("I1 = 10m : wire = out;\nR1 out 0 1k")
            win.editor.document().setModified(True)
            button = next(
                b
                for b in win.findChildren(QPushButton)
                if b.text() == "Export to SPICE"
            )
            with patch(
                "circuitlab.gui.QFileDialog.getSaveFileName",
                return_value=(str(path.with_suffix("")), "SPICE netlists (*.cir)"),
            ):
                button.click()
            self.assertIn("I1 0 out DC 0.01", path.with_suffix(".cir").read_text())
            self.assertIn("V1 out 0 2", path.read_text())
            self.assertEqual(win.path, path)
            self.assertTrue(win.editor.document().isModified())

    def test_spice_export_reports_source_error_before_opening_save_dialog(self):
        win = self.window
        win.editor.setPlainText("V1 in 0 12\nR1 in 0 invalid")
        with (
            patch("circuitlab.gui.QMessageBox.warning") as warning,
            patch("circuitlab.gui.QFileDialog.getSaveFileName") as save_dialog,
        ):
            self.assertFalse(win.export_netlist())
        self.assertIn("Line 2", warning.call_args.args[2])
        self.assertIn("Line 2", win.issues.item(0).text())
        save_dialog.assert_not_called()

    def test_ltspice_button_exports_the_current_render(self):
        win = self.window
        win.editor.setPlainText("V1 = 12 : wire = in;\nR1 in out 2k\nR2 out 0 1k")
        win.render_source()
        drawing = win.schematic.layout
        button = next(
            b
            for b in win.findChildren(QPushButton)
            if b.text() == "Export LTspice .asc"
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "divider.asc"
            with patch(
                "circuitlab.gui.QFileDialog.getSaveFileName",
                return_value=(str(path), "LTspice schematics (*.asc)"),
            ):
                button.click()
            self.assertIn("SYMBOL res ", path.read_text(encoding="ascii"))
            self.assertIs(win.schematic.layout, drawing)
            self.assertIsNone(win.path)

    def test_editor_completes_unsaved_method_and_preserves_existing_arguments(self):
        editor = self.window.editor
        editor.setFocus()
        editor.setPlainText("main {\n    \n}\nseries R10K { R1 = 1k; }")
        cursor = editor.textCursor()
        cursor.setPosition(len("main {\n    "))
        editor.setTextCursor(cursor)
        QTest.keyClicks(editor, "r")
        popup = editor.completer.popup()
        self.assertTrue(popup.isVisible())
        self.assertEqual(popup.currentIndex().data(), "R10K")
        QTest.keyClick(popup, Qt.Key.Key_Tab)
        self.assertEqual(editor.textCursor().block().text(), "    R10K();")
        QTest.keyClicks(editor, "Vin, out")
        self.assertEqual(editor.textCursor().block().text(), "    R10K(Vin, out);")

        cursor = editor.textCursor()
        cursor.setPosition(len("main {\n    R"))
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(popup.isVisible())
        QTest.keyClick(popup, Qt.Key.Key_Tab)
        self.assertEqual(editor.textCursor().block().text(), "    R10K(Vin, out);")

    def test_editor_indents_nested_blocks_and_hovers_over_op_amp(self):
        editor = self.window.editor
        editor.setFocus()
        QTest.keyClicks(editor, "main {")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "parallel {")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "R1 = 1k;")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "}")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "op7(out, buffered, buffered);")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "}")
        self.assertEqual(
            editor.toPlainText(),
            "main {\n    parallel {\n        R1 = 1k;\n    }\n    op7(out, buffered, buffered);\n}",
        )
        cursor = QTextCursor(editor.document().findBlockByNumber(4))
        cursor.movePosition(
            QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, 5
        )
        pos = editor.cursorRect(cursor).center()
        QApplication.sendEvent(
            editor.viewport(),
            QHelpEvent(QEvent.Type.ToolTip, pos, editor.viewport().mapToGlobal(pos)),
        )
        self.assertIn("op7(in+, in-, out)", QToolTip.text())
        self.assertIn("negative feedback", QToolTip.text())
        QToolTip.hideText()
        original = editor.toPlainText()
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        self.assertEqual(
            editor.toPlainText(),
            "\n".join("    " + line for line in original.splitlines()),
        )
        QTest.keyClick(editor, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(editor.toPlainText(), original)

    def test_mixer_uses_routed_nets_and_mixed_solution(self):
        win = self.window
        win.open_path(EXAMPLES / "three_band_mixer.ckt")
        self.assertEqual(len(win.solution.circuit.components), 63)
        self.assertEqual(set(win.schematic.paths), set(win.solution.circuit.nodes))
        self.assertEqual(win.solution.circuit.mode, "mixed")
        win.probe("node", "outP")
        self.assertIn("AC RMS", win.status.text())
        self.assertIn("DC", win.status.text())

    def test_unsolved_circuit_renders_and_reports_error(self):
        win = self.window
        win.editor.setPlainText("V1 a 0 1\nR1 x y 1k")
        win.compile_source()
        self.assertIsNone(win.solution)
        self.assertIn("floating", win.issues.item(0).text())
        self.assertEqual(set(win.schematic.paths), {"0", "a", "x", "y"})
        win.editor.setPlainText("V1 a 0 1\nR1 a 0 made-up")
        win.compile_source()
        win.select_diagnostic(win.issues.item(0))
        self.assertEqual(win.editor.textCursor().blockNumber(), 1)

    def test_split_ground_filter_renders_even_when_the_dc_return_floats(self):
        win = self.window
        win.editor.setPlainText("""
        battery V1 { Vpk = 3; f = 900; Vdc = 4; wire = in; ground = agnd; }
        main {
            series { R1 = 1k; }
            parallel {
                C1 = 1u;
                wire = a;
                ground = bgnd;
                R2 = 10k;
                ground = agnd;
            }
            op1(a, buffered, buffered);
            measure = V buffered agnd;
        }
        """)
        win.compile_source()
        self.assertIsNone(win.solution)
        self.assertIn("Floating ground return in DC: bgnd", win.issues.item(0).text())
        self.assertIn("agnd", win.schematic.anchors)
        self.assertIn("bgnd", win.schematic.anchors)
        self.assertLess(
            win.schematic.anchors["agnd"][0], win.schematic.anchors["bgnd"][0]
        )
        self.assertLess(
            win.schematic.anchors["bgnd"][0], win.schematic.anchors["buffered"][0]
        )
        win.render_source()
        self.assertEqual(win.status.text(), "Schematic rendered.")

    def test_preserves_source_and_file_encoding(self):
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "legacy.ckt"
            original = "\ufeff# Legacy Ω\r\nV1 in 0 2\r\nR1 in 0 1k\r\n".encode()
            path.write_bytes(original)
            self.assertTrue(self.window.open_path(path))
            self.assertTrue(self.window.save())
            self.assertEqual(path.read_bytes(), original)
            editor = self.window.editor
            editor.moveCursor(editor.textCursor().MoveOperation.End)
            editor.insertPlainText("# additional comment\n")
            self.assertTrue(self.window.save())
            self.assertEqual(path.read_bytes(), original + b"# additional comment\r\n")

    def test_pin_placement_rename_search_and_reopen(self):
        win = self.window
        win.open_path(EXAMPLES / "three_way_crossover.ckt")
        pos = win.schematic.mapFromScene(point(win.schematic.anchors["midOut"]))
        QTest.mouseClick(win.pin_button, Qt.MouseButton.LeftButton)
        with patch(
            "circuitlab.gui.QInputDialog.getText", return_value=("Mid speaker", True)
        ):
            QTest.mouseClick(
                win.schematic.viewport(), Qt.MouseButton.LeftButton, pos=pos
            )
        self.assertEqual(win.pins, {"midOut": "Mid speaker"})
        self.assertIs(win.views.currentWidget(), win.results)
        self.assertEqual(win.results.search.text(), "Mid speaker")
        reading = next(
            row for row in win.results.rows if row[0] == "Mid speaker · AC voltage RMS"
        )
        self.assertIn("801.92", reading[2])
        self.assertTrue(
            any(
                "current away" in row[0]
                for row in win.results.rows
                if row[0].startswith("Mid speaker")
            )
        )

        with patch(
            "circuitlab.gui.QInputDialog.getText", return_value=("My_Speaker", True)
        ):
            win.schematic.pin_renamed.emit("midOut")
        self.assertEqual(win.pins, {"midOut": "My_Speaker"})
        win.results.search.setText("my speaker")
        self.assertGreater(win.results.table.rowCount(), 5)
        win.wire_style.setCurrentIndex(win.wire_style.findData("labeled"))
        self.assertEqual(len(win.schematic.pin_items), 2)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            path = Path(folder) / "pins.ckt"
            self.assertTrue(win.write_path(path))
            self.assertTrue(win.open_path(path))
            self.assertEqual(win.pins, {"midOut": "My_Speaker"})
            self.assertEqual(len(win.schematic.pin_items), 2)
            win.schematic.pin_removed.emit("midOut")
            self.assertEqual(win.pins, {})
            self.assertFalse(win.schematic.pin_items)

    def test_mixed_pin_has_dc_ac_and_waveform_results(self):
        win = self.window
        win.open_path(EXAMPLES / "three_band_mixer.ckt")
        win.write_pin("outP", "Speaker P")
        labels = {row[0] for row in win.results.rows}
        self.assertIn("Speaker P · DC voltage", labels)
        self.assertIn("Speaker P · AC voltage RMS", labels)
        self.assertIn("Speaker P · v(t)", labels)
