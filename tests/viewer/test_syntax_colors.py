"""Highlight real source spans while protecting quoted text and UTF-16 offsets."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from circuitlab.viewer.editor import CircuitHighlighter


class SyntaxColorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def color(self, source, token, *, occurrence=0):
        document = QTextDocument(source)
        highlighter = CircuitHighlighter(document)
        highlighter.rehighlight()
        index = -1
        for _ in range(occurrence + 1):
            index = source.index(token, index + 1)
        block = document.findBlock(len(source[:index].encode("utf-16-le")) // 2)
        position = len(source[:index].encode("utf-16-le")) // 2 - block.position()
        span = next(fmt for fmt in block.layout().formats()
                    if fmt.start <= position < fmt.start + fmt.length)
        return span.format.foreground().color().name()

    def test_behavior_blocks_properties_and_control_have_distinct_colors(self):
        source = "chip Gain(a,out) { analog { properties { gain=10; } V(out)=gain*V(a); } digital {out=1;} if(true) {} }"
        expected = {"chip":"#92bfff", "Gain":"#e6dfa2", "analog":"#659cd7",
                    "digital":"#659cd7", "properties":"#64d8cb", "gain":"#7fd5e6",
                    "if(":"#d995d9", "true":"#b5d49d"}
        for token, color in expected.items():
            with self.subTest(token=token):
                self.assertEqual(self.color(source,token), color)

    def test_math_functions_dotted_properties_values_and_units_are_readable(self):
        source = "analog {property output.max: V=12V; V(out)=min(integrate(1kHz,time),output.max);}"
        for token, color in (("property ","#64d8cb"), ("output.max","#7fd5e6"),
                             ("min(","#e6dfa2"), ("integrate(","#e6dfa2"),
                             ("12","#ffd18b"), ("kHz","#dfb76c"),
                             ("time","#b5d49d")):
            with self.subTest(token=token):
                self.assertEqual(self.color(source,token),color)
        self.assertEqual(self.color("analog V(out)=V(in)*10;","*"),"#bac3ce")
        self.assertEqual(self.color("analog op1 { slew=2.5V/us; }","V/us"),"#dfb76c")
        self.assertEqual(self.color(source,"output.max",occurrence=1),"#7fd5e6")

    def test_strings_with_hashes_and_emoji_do_not_shift_or_leak_colors(self):
        source = 'include("😀#analog 2MHz.ckt"); analog { property gain=2; } # digital 4V'
        self.assertEqual(self.color(source,"analog"),"#d6b88c")
        self.assertEqual(self.color(source,"2MHz"),"#d6b88c")
        self.assertEqual(self.color(source,"analog",occurrence=1),"#659cd7")
        self.assertEqual(self.color(source,"property"),"#64d8cb")
        self.assertEqual(self.color(source,"digital"),"#9bc68b")
        self.assertEqual(self.color(source,"4V"),"#9bc68b")

    def test_incomplete_strings_and_keywords_still_highlight(self):
        self.assertEqual(self.color('include("analog 2V',"analog"),"#d6b88c")
        self.assertEqual(self.color("analog", "analog"),"#659cd7")
        self.assertEqual(self.color("digital", "digital"),"#659cd7")
        self.assertEqual(self.color("analogous = 1;", "analogous"),"#7fd5e6")

    def test_existing_components_gate_calls_and_wire_counts_keep_their_colors(self):
        source = "main { i=count; Rload(1k) between(in,w*i); op1(w^,0,w*i); nor(vcc,0,a,y); }"
        for token in ("Rload", "op1", "nor", "w*i", "w^"):
            with self.subTest(token=token):
                self.assertEqual(self.color(source,token),"#e3b5f5")
        self.assertEqual(self.color(source,"count"),"#659cd7")
        self.assertEqual(self.color("# @param gain is analog","@param"),"#b5d49d")
        self.assertEqual(self.color("@override analog op1 {gain=10;}","@override"),"#f6a652")


if __name__ == "__main__":
    unittest.main()
