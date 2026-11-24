"""Compact captions remain inspectable, undoable, and independent of wiring."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.persistence import pack_layout
from circuitlab.math.report import component_labels
from circuitlab.viewer.schematic import SchematicView, point


SOURCE = """
f = 1k;
V1(1 Vrms) between(input,0);
R1(10k) between(input,output);
C1(100nF) between(output,0);
"""


class ValueLabelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.circuit = compile_circuit(SOURCE, render_only=True)
        self.view = SchematicView()
        self.view.resize(900, 650)
        self.view.show()
        self.view.draw(self.circuit)
        self.app.processEvents()
        self.key = None, "auto"

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def caption(self, name):
        return next(item for item in self.view.captions
                    if item.data(0) == "component" and item.data(1) == name)

    def choose_menu(self, position, text):
        actions = {}

        def choose():
            menu = self.app.activePopupWidget()
            actions.update({action.text(): action for action in menu.actions()})
            if text in actions:
                actions[text].trigger()
            menu.close()

        QTimer.singleShot(0, choose)
        self.view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position,
                                                   self.view.viewport().mapToGlobal(position)))
        self.assertIn(text, actions)

    def test_right_click_hides_only_the_selected_title_and_reduces_font(self):
        original = self.view.layout
        full = self.caption("C1")
        font_size = full.font().pixelSize()
        position = self.view.mapFromScene(full.sceneBoundingRect().center())
        self.choose_menu(position, "Values only")
        caption = self.caption("C1")
        self.assertEqual(caption.text(), "100 nF")
        self.assertLess(caption.font().pixelSize(), font_size)
        self.assertEqual(caption.toolTip(), "C1\n100 nF")
        self.assertEqual(caption.data(4), ("component", "C1"))
        self.assertIn("R1", self.caption("R1").text())
        self.assertEqual(self.view.layout.symbols, original.symbols)
        self.assertEqual(self.view.layout.wires, original.wires)
        self.assertIs(self.view.circuit, self.circuit)
        handle = next(item for item in self.view.scene().items()
                      if item.data(0) == "component" and item.data(1) == "C1" and not hasattr(item, "text"))
        self.assertEqual(handle.toolTip(), "C1\n100 nF")

    def test_background_menu_changes_all_values_and_preserves_source_details(self):
        expected = component_labels(self.view.levels[self.key])
        self.choose_menu(self.view.viewport().rect().bottomRight(), "Values only (all components)")
        for name, lines in expected.items():
            self.assertEqual(self.caption(name).text(), "\n".join(lines[1:]))
            self.assertEqual(self.caption(name).toolTip(), "\n".join(lines))
        self.assertIn("peak", self.caption("V1").text())
        self.assertIn("1 kHz", self.caption("V1").text())
        self.choose_menu(self.view.viewport().rect().bottomRight(), "Values only (all components)")
        self.assertEqual(self.caption("C1").text(), "C1\n100 nF")

    def test_title_can_be_restored_and_undone_without_changing_geometry(self):
        original = self.view.layout
        self.view.set_values_only(True, key=self.key, name="C1")
        compact = self.view.layout
        self.view.set_values_only(False, key=self.key, name="C1")
        self.assertEqual(self.caption("C1").text(), "C1\n100 nF")
        self.view.undo()
        self.assertIs(self.view.layout, compact)
        self.assertEqual(self.caption("C1").text(), "100 nF")
        self.view.undo()
        self.assertIs(self.view.layout, original)

    def test_saved_style_reopens_with_value_labels_and_movable_captions(self):
        self.view.set_values_only(True, key=self.key, name="C1")
        saved = pack_layout(self.view.layout)
        positions = dict(self.view.layout.positions)
        self.view.clear()
        self.view.placement_records[self.key] = saved
        self.view.draw(compile_circuit(SOURCE, render_only=True))
        self.assertEqual(self.caption("C1").text(), "100 nF")
        self.assertEqual(self.view.layout.positions, positions)
        self.assertEqual(self.caption("C1").data(2), self.key)

    def test_name_only_opamp_and_chip_titles_remain_visible(self):
        source = """chip Header(a,b) { R1(1k) between(a,b); }
        main { Header(a,b); op1(a,b,out); Rload(10k) between(out,0); }
        """
        self.view.draw(compile_circuit(source, render_only=True))
        self.view.set_values_only(True)
        texts = [item.text() for item in self.view.scene().items() if hasattr(item, "text")]
        self.assertIn("Header", texts)
        self.assertEqual(self.caption("op1").text(), "op1")
        self.assertEqual(self.caption("Rload").text(), "10 kΩ")


if __name__ == "__main__":
    unittest.main()
