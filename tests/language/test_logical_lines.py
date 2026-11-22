"""Continued expressions keep their start lines and leave statement boundaries intact."""

from pathlib import Path
import re
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.syntax import source_rows
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError

ROOT = Path(__file__).resolve().parents[2]
MEMORY = ROOT / "examples/advanced/CortexM7/core/thumb/memory.ckt"
FIRMWARE = ROOT / "examples/advanced/CortexM7/CortexM7_Firmware.ckt"


def rows(source):
    return [(text, line) for text, line, _ in source_rows(source, legacy_markers=False)]


def reflow_memory(source):
    # Change only whitespace and redundant outer grouping in this test input.
    # Never edit the instruction decoder to compensate for statement splitting.
    source, count = re.subn(r"(?ms)^(    kind = )\((.*?)\);", r"\1\2;", source)
    if count != 1:
        raise AssertionError("Expected the memory decoder's kind expression")
    source, count = re.subn(r"(?m)^(    unprivileged = .*? && )", r"\1\n        ", source)
    if count != 1:
        raise AssertionError("Expected the memory decoder's unprivileged expression")
    return source.replace("exclusive = exclusive_load || exclusive_store;",
                          "exclusive = exclusive_load ||\n        exclusive_store;")


class LogicalLinesTest(unittest.TestCase):
    def test_binary_operator_continuations_without_outer_parentheses(self):
        for operator in ("&&", "||", "&", "|", "^", "<<", ">>", "==", "!=", "<=", ">=", "+", "-", "*", "/"):
            with self.subTest(operator=operator):
                self.assertEqual(rows(f"out = a {operator}\n    b;\nnext = 1;"),
                                 [(f"out = a {operator} b;", 1), ("next = 1;", 3)])

    def test_ternary_arms_and_nested_ternaries_continue(self):
        self.assertEqual(rows("out = first ?\n a :\n second ? b :\n c;\nnext = 1;"),
                         [("out = first ? a : second ? b : c;", 1), ("next = 1;", 5)])

    def test_pending_ternary_question_can_precede_a_colon_on_the_next_line(self):
        self.assertEqual(rows("out = flag ? yes\n : no;\nnext=1;"),
                         [("out = flag ? yes : no;",1),("next=1;",3)])

    def test_case_default_and_arm_labels_remain_separate(self):
        source = "case 0:\ncase 1:\nout = a ?\n b :\n c;\ndefault:\nout = 0;\nreset_handler:\nNOP;"
        self.assertEqual(rows(source), [("case 0:",1), ("case 1:",2),
                         ("out = a ? b : c;",3), ("default:",6), ("out = 0;",7),
                         ("reset_handler:",8), ("NOP;",9)])

    def test_switch_behavior_with_continued_case_expressions(self):
        source = """chip Dispatch(sel,a,b,out) {
            sel[2],a,b=input;
            out[2]=output;
            switch(sel) {
                case 0:
                case 1:
                    out = a &&
                          b ?
                          1 :
                          2;
                    break;
                default:
                    out = a ||
                          b ?
                          3 :
                          0;
            }
        }"""
        sim = DigitalSimulation(source)
        for selector,a,b,expected in ((0,0,0,2),(1,1,1,1),(2,0,0,0),(3,1,0,3)):
            sim.set_input("sel",selector); sim.set_input("a",a); sim.set_input("b",b)
            self.assertEqual(sim.values["out"],expected)
        positions = dict((text,line) for text,line in rows(source) if text in {"case 0:","case 1:","default:"})
        self.assertEqual(positions,{"case 0:":5,"case 1:":6,"default:":12})

    def test_dangling_operators_do_not_consume_new_statements(self):
        for statement in ("next = 1;", "next[4] = 1;", "words[1] = 2;", "next += 1;", "R*{i}[32]=0;", "}", "case 1:", "default:"):
            with self.subTest(statement=statement):
                self.assertEqual(rows("out = a &&\n"+statement), [("out = a &&",1),(statement,2)])
        self.assertEqual(rows("count++\nnext=1;"),[("count++",1),("next=1;",2)])
        self.assertEqual(rows("count--\nnext=1;"),[("count--",1),("next=1;",2)])

    def test_comment_blank_lines_and_diagnostics_keep_original_line_numbers(self):
        source = "chip Broken(a,out) {\n a=input;\n out=output;\n out = a &&\n // continuation\n\n missing;\n other[0];\n}"
        logical = rows(source)
        self.assertIn(("out = a &&   missing",4),logical)
        self.assertIn(("other[0]",8),logical)
        with self.assertRaises(CircuitError) as caught:
            export_verilog(source)
        # Invalid state widths are checked before equations; its true line survives.
        self.assertEqual(caught.exception.line,8)
        with self.assertRaises(CircuitError) as caught:
            export_verilog(source.replace(" other[0];\n", ""))
        self.assertEqual(caught.exception.line,4)

    def test_legacy_percentages_placeholders_and_slice_colons_are_preserved(self):
        self.assertEqual(rows("eta=80%\nspeedRatio=0.9"),[("eta=80%",1),("speedRatio=0.9",2)])
        self.assertEqual(rows("R1 =\nR2 = 1k"),[("R1 =",1),("R2 = 1k",2)])
        self.assertEqual(rows("out = bus[7:0];\nnext=1;"),[("out = bus[7:0];",1),("next=1;",2)])

    def test_closed_quoted_values_are_complete_and_literal_operators_stay_hidden(self):
        self.assertEqual(rows('pins[1:0] = "a?, b:"\nBuffer(pins[1:0]);'),
                         [('pins[1:0] = "a?, b:"',1), ('Buffer(pins[1:0]);',2)])
        self.assertEqual(rows('text = "a +"\nnext=1;'), [('text = "a +"',1), ('next=1;',2)])

    def test_slice_continuations_and_unary_operators(self):
        self.assertEqual(rows("out = data[\n7:\n0];\nnext=1;"),
                         [("out = data[ 7: 0];",1),("next=1;",4)])
        for operator in ("!", "~"):
            self.assertEqual(rows(f"out = {operator}\na;"), [(f"out = {operator} a;",1)])

    def test_terminated_errors_and_unclosed_parentheses_do_not_swallow_labels(self):
        self.assertEqual(rows("out = a &&;\nnext=1;"), [("out = a &&;",1),("next=1;",2)])
        self.assertEqual(rows("out = (a &&\ncase 1:\nnext=1;"),
                         [("out = (a &&",1),("case 1:",2),("next=1;",3)])

    def test_multiline_for_headers_keep_their_inner_declaration_and_semicolons(self):
        source = """chip Generated(a,out0,out1) {
            a=input; out0,out1=output;
            for (int i=0; i<2; i++) { out*{i}=a; }
        }"""
        multiline = source.replace("for (int i=0; i<2; i++)",
                                   "for (\nint i=0;\ni<2;\ni++\n)")
        self.assertEqual(export_verilog(multiline),export_verilog(source))

    def test_thumb_memory_decoder_accepts_reflowed_logic_without_semantic_changes(self):
        source = MEMORY.read_text(encoding="utf-8")
        self.assertEqual(export_verilog(reflow_memory(source)),export_verilog(source))

    def test_firmware_entry_compiles_with_the_reflowed_memory_include(self):
        source = FIRMWARE.read_text(encoding="utf-8")
        def read(path):
            text = Path(path).read_text(encoding="utf-8")
            return reflow_memory(text) if Path(path).resolve() == MEMORY.resolve() else text
        original = export_verilog(source,path=FIRMWARE)
        reflowed = export_verilog(source,path=FIRMWARE,read_source=read)
        self.assertEqual(reflowed,original)
        self.assertIn("module CORTEX_M7_FIRMWARE",reflowed)

    def test_imported_memory_expression_errors_keep_the_file_and_assignment_line(self):
        memory = reflow_memory(MEMORY.read_text(encoding="utf-8"))
        memory = memory.replace("        exclusive_store;", "        unknown_operand;")
        expected_line = memory[:memory.index("    exclusive =")].count("\n") + 1
        def read(path):
            return memory if Path(path).resolve() == MEMORY.resolve() else Path(path).read_text(encoding="utf-8")
        with self.assertRaises(CircuitError) as caught:
            export_verilog(FIRMWARE.read_text(encoding="utf-8"),path=FIRMWARE,read_source=read)
        self.assertEqual(caught.exception.line,expected_line)
        self.assertEqual(caught.exception.path.resolve(),MEMORY.resolve())
        self.assertIn("unknown_operand",caught.exception.message)
