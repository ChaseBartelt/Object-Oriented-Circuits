"""Highlight real source spans while protecting quoted text and UTF-16 offsets."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication

from circuitlab.viewer.editor import CircuitEditor, CircuitHighlighter


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
        self.assertEqual(self.color("analog V(out)=V(in)*10;","*"),"#e6b98b")
        self.assertEqual(self.color("analog op1 { slew=2.5V/us; }","V/us"),"#dfb76c")
        self.assertEqual(self.color(source,"output.max",occurrence=1),"#7fd5e6")

    def test_strings_with_hashes_and_emoji_do_not_shift_or_leak_colors(self):
        source = 'include("😀#analog 2MHz.ckt"); analog { property gain=2; } # digital 4V'
        self.assertEqual(self.color(source,"analog"),"#aed6ff")
        self.assertEqual(self.color(source,"2MHz"),"#aed6ff")
        self.assertEqual(self.color(source,"analog",occurrence=1),"#659cd7")
        self.assertEqual(self.color(source,"property"),"#64d8cb")
        self.assertEqual(self.color(source,"digital"),"#9bc68b")
        self.assertEqual(self.color(source,"4V"),"#9bc68b")

    def test_incomplete_strings_and_keywords_still_highlight(self):
        self.assertEqual(self.color('include("analog 2V',"analog"),"#aed6ff")
        self.assertEqual(self.color("analog", "analog"),"#659cd7")
        self.assertEqual(self.color("digital", "digital"),"#659cd7")
        self.assertEqual(self.color("analogous = 1;", "analogous"),"#7fd5e6")

    def test_slash_comments_keep_block_state_and_protect_quoted_paths(self):
        source = 'include("chips//part/*file*/.ckt"); // @pin analog\n/* digital\n😀 property */ analog { }'
        self.assertEqual(self.color(source, "//part"), "#aed6ff")
        self.assertEqual(self.color(source, "/*file"), "#aed6ff")
        self.assertEqual(self.color(source, "@pin"), "#b5d49d")
        self.assertEqual(self.color(source, "analog"), "#9bc68b")
        self.assertEqual(self.color(source, "digital"), "#9bc68b")
        self.assertEqual(self.color(source, "property"), "#9bc68b")
        self.assertEqual(self.color(source, "analog", occurrence=1), "#659cd7")

    def test_comment_context_skips_completion_and_keeps_brace_indentation(self):
        editor = CircuitEditor()
        self.addCleanup(editor.close)
        for source, expected in (
            ("main {\n    /* start\n    R", None),
            ("main {\n    // R", None),
            ("main {\n    /* start\n    */ R", "R"),
        ):
            editor.setPlainText(source)
            editor.moveCursor(QTextCursor.MoveOperation.End)
            self.assertEqual(editor._completion_prefix(), expected)
        editor.setPlainText("main { // comment")
        editor.moveCursor(QTextCursor.MoveOperation.End)
        editor.insert_newline(editor.textCursor())
        self.assertTrue(editor.toPlainText().endswith("\n    "))

    def test_existing_components_gate_calls_and_wire_counts_keep_their_colors(self):
        source = "main { i=count; Rload(1k) between(in,w*i); op1(w^,0,w*i); nor(vcc,0,a,y); }"
        for token in ("Rload", "nor", "w*i", "w^"):
            with self.subTest(token=token):
                self.assertEqual(self.color(source,token),"#e3b5f5")
        self.assertEqual(self.color(source,"count"),"#659cd7")
        self.assertEqual(self.color("# @param gain is analog","@param"),"#b5d49d")
        self.assertEqual(self.color("@override analog op1 {gain=10;}","@override"),"#f6a652")

    def test_diode_names_use_component_color_and_leave_their_wires_alone(self):
        source = 'D.Schot1() between(anode, cathode);'
        self.assertEqual(self.color(source, 'D.Schot1'), '#e3b5f5')
        self.assertEqual(self.color(source, 'anode'), '#c7dce8')
        self.assertEqual(self.color(source, 'cathode'), '#c7dce8')

    def test_analog_header_targets_match_their_calls(self):
        for token, header in (("op1", "analog (op1, op2) {"),
                              ("op2", "analog (op1, op2) {"),
                              ("oocCollectorComparator", "analog oocCollectorComparator {")):
            with self.subTest(token=token):
                self.assertEqual(self.color(f"{token}(a, b, out);", token), "#e6dfa2")
                self.assertEqual(self.color(header, token), "#e6dfa2")

    def test_only_port_keywords_and_chip_header_parameters_use_direction_colors(self):
        source = ("chip Unit(a,\n b, result) {\n"
                  "input bus { a[32], b; }\n"
                  "output state { result[32]; }\n"
                  "wire internal { scratch[32], flag; }\n}\n"
                  "chip Other(a, result) { input {result;} output {a;} }")
        for token, color in (('input', '#f6a652'), ('output', '#659cd7'), ('wire', '#4ec9b0'),
                             ('a,', '#f6a652'), ('b,', '#f6a652'), ('result)', '#659cd7')):
            self.assertEqual(self.color(source, token), color, token)
        for token in ('bus', 'state', 'internal'):
            self.assertEqual(self.color(source, token), '#d4d4d4', token)
        for token in ('a[32]', 'result[32]', 'scratch', 'flag'):
            self.assertEqual(self.color(source, token), '#c7dce8', token)
        self.assertEqual(self.color(source, 'a,', occurrence=1), '#659cd7')
        self.assertEqual(self.color(source, 'result)', occurrence=1), '#f6a652')
        self.assertEqual(self.color(source, '32'), '#ffd18b')

    def test_legacy_port_keywords_and_generated_members_keep_normal_styles(self):
        legacy = 'a[32], b[32] = input; y[32] = output; temp[8] = wire; y=a;'
        for token, color in (('input', '#f6a652'), ('output', '#659cd7'), ('wire', '#4ec9b0')):
            self.assertEqual(self.color(legacy, token), color)
        for token in ('a[32]', 'b[32]', 'y[32]', 'temp'):
            self.assertEqual(self.color(legacy, token), '#c7dce8')
        self.assertEqual(self.color(legacy, '32'), '#ffd18b')
        generated = ('input interrupts {\nCLK;\nfor (int i=0; i<2; i++) {\n'
                     'irq${i}[8];\n}\nRESET;\n}\noutput status {\nDONE;\n}')
        self.assertEqual(self.color(generated, 'interrupts'), '#d4d4d4')
        for token in ('CLK', 'irq', 'RESET', 'DONE'):
            self.assertEqual(self.color(generated, token), '#c7dce8')
        self.assertEqual(self.color(generated, 'for'), '#d995d9')
        self.assertEqual(self.color(generated, '8'), '#ffd18b')

    def test_multiline_quoted_lists_have_one_string_color_through_the_closing_quote(self):
        source = ('pins = "FPU_A[32], fpu_b[32],\n'
                  '  input fake { PC[32]; }, 0x20, // literal text\n'
                  '  r0, ARM, #5";\ninput real { actual[8]; }')
        for token in ('FPU_A', '32', 'fpu_b', 'input', 'fake', 'PC', '0x20', '// literal', 'r0', '#5'):
            self.assertEqual(self.color(source, token), '#aed6ff', token)
        self.assertEqual(self.color(source, 'actual'), '#c7dce8')
        editor = CircuitEditor()
        self.addCleanup(editor.close)
        editor.setPlainText('pins = "first,\n    R')
        editor.highlighter.rehighlight()
        editor.moveCursor(QTextCursor.MoveOperation.End)
        self.assertIsNone(editor._completion_prefix())

    def test_arm_palette_is_scoped_and_keeps_immediates_and_register_lists(self):
        source = ('arm(address, word, valid) {\n.word 0x20000400, reset + 1;\n'
                  'reset: lDr R0, literal; // assembly note\n'
                  'MOVS r1, #5; @ alternate note\nADDS r0, #2;\nB reset;\n'
                  'PUSH {r4-r7, lr};\nliteral: .word 12;\n}\n'
                  'r0 = 32; // outside comment\n# outside legacy')
        for token in ('.word', 'lDr', 'MOVS', 'ADDS', 'B reset', 'PUSH'):
            self.assertEqual(self.color(source, token), '#b45a5a', token)
        for token in ('R0', 'r1', 'r4', 'r7', 'lr'):
            self.assertEqual(self.color(source, token), '#9cdcfe', token)
        for token in ('reset', 'literal'):
            self.assertEqual(self.color(source, token), '#b5cea8', token)
        for token in ('0x20000400', '#5', '#2', '12'):
            self.assertEqual(self.color(source, token), '#ce9178', token)
        for token in ('// assembly', '@ alternate'):
            self.assertEqual(self.color(source, token), '#808080', token)
        self.assertEqual(self.color(source, 'r0', occurrence=1), '#e3b5f5')
        self.assertEqual(self.color(source, '// outside'), '#9bc68b')
        self.assertEqual(self.color(source, '# outside'), '#9bc68b')

    def test_clear_header_is_blue_and_protected_occurrences_keep_their_styles(self):
        source = 'clock(clk) { if(reset) { clear { scalar, memory, flags[3:0] }; } }'
        self.assertEqual(self.color(source, 'clear'), '#659cd7')
        self.assertEqual(self.color(source, 'scalar'), '#c7dce8')
        self.assertEqual(self.color(source, '3'), '#ffd18b')
        self.assertEqual(self.color('// clear { scalar; }', 'clear'), '#9bc68b')
        self.assertEqual(self.color('pins = "first,\nclear { flags; }";', 'clear'), '#aed6ff')

    def test_operator_categories_use_distinct_colors_and_whole_longest_tokens(self):
        families = {
            '#82aaff': ('<', '>', '<=', '>=', '==', '!='),
            '#c792ea': ('&&', '||', '!'),
            '#4ec9b0': ('&', '|', '^', '~'),
            '#ffb86c': ('<<', '>>'),
            '#e6b98b': ('+', '-', '*', '/', '%', '++', '--'),
            '#9aa0a6': ('=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>='),
        }
        for expected, operators in families.items():
            for operator in operators:
                source = f'left {operator} right;'
                with self.subTest(operator=operator):
                    self.assertEqual(self.color(source, operator), expected)
                    if len(operator) > 1:
                        self.assertEqual(self.color(source, operator[-1]), expected)

    def test_ternary_colons_are_paired_across_lines_without_recoloring_ranges_or_labels(self):
        source = 'word[31:0] = (flag ?\nleft[7:0]\n: other ? x : y); label: word=0;'
        for occurrence in (0, 1, 4):
            self.assertEqual(self.color(source, ':', occurrence=occurrence), '#98a3b3')
        for occurrence in (2, 3):
            self.assertEqual(self.color(source, ':', occurrence=occurrence), '#dcdcaa')
        for occurrence in (0, 1):
            self.assertEqual(self.color(source, '?', occurrence=occurrence), '#dcdcaa')
        self.assertEqual(self.color(source, '31'), '#ffd18b')

    def test_operator_colors_preserve_comments_strings_units_and_arm_values(self):
        source = 'pins="a && b,\nx ? y : z, >= |= <<="; // && !=\n/* x?y:z */\narm(a,b) { MOVS r0, #-5; }'
        for token in ('&&', '?', ':', '>=', '|=', '<<='):
            self.assertEqual(self.color(source, token), '#aed6ff')
        self.assertEqual(self.color(source, '&&', occurrence=1), '#9bc68b')
        self.assertEqual(self.color(source, '!=', occurrence=0), '#9bc68b')
        self.assertEqual(self.color(source, '?', occurrence=1), '#9bc68b')
        self.assertEqual(self.color(source, '#-5'), '#ce9178')
        self.assertEqual(self.color('analog op1 { slew=2.5V/us; }', '/'), '#dfb76c')
        self.assertEqual(self.color('value=1e-3;', '-'), '#ffd18b')

    def test_editing_a_ternary_recolors_following_blocks_incrementally(self):
        document = QTextDocument('value = (flag ?\n left\n : right);')
        highlighter = CircuitHighlighter(document)
        highlighter.rehighlight()

        def colon_color():
            block = document.findBlockByNumber(2)
            offset = block.text().index(':')
            return next(span.format.foreground().color().name() for span in block.layout().formats()
                        if span.start <= offset < span.start + span.length)

        self.assertEqual(colon_color(), '#dcdcaa')
        cursor = QTextCursor(document)
        cursor.setPosition(document.toPlainText().index('?'))
        cursor.deleteChar()
        highlighter.rehighlightBlock(document.firstBlock())
        self.app.processEvents()
        self.assertEqual(colon_color(), '#98a3b3')


if __name__ == "__main__":
    unittest.main()
