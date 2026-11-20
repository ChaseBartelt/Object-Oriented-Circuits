"""Wire-counter help and traces use compiler identities without advancing state."""

import json
import os
from pathlib import Path
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.analysis import analyze
from circuitlab.tooling.source_info import SourceInfo
from circuitlab.tooling.trace import wire_trace


ROOT = Path(__file__).resolve().parents[2]


class WireCountToolingTest(unittest.TestCase):
    def test_counter_declaration_help_and_current_forms_do_not_execute_the_source(self):
        declaration = "main { i = count; }"
        for token in ("i =", "count"):
            help_text = SourceInfo(declaration).hover_help(0, declaration.index(token))
            self.assertIn("initially 0", help_text)
            self.assertIn("cannot be assigned manually", help_text)
        self.assertIn("initially 0", SourceInfo("main {i=COUNT;}").hover_help(0,8))
        for token, expected in (("wi*", "Advance counter wi"), ("wi^", "without advancing"),
                                ("w*i", "Advance counter i"), ("w^i", "without advancing"),
                                ("w$i", "without advancing"), ("w^", "last counter")):
            # Tolerant syntax help is still available in an incomplete main.
            source = "main {\ni=count;\nR1(1k) between(input," + token + ");"
            info = SourceInfo(source)
            column = source.splitlines()[2].index(token)
            for _ in range(3):
                self.assertIn(expected, info.hover_help(2, column))
                self.assertEqual(info.wire_at(2, column), (3,"R1",1,token))

    def test_count_help_does_not_reclassify_analog_or_digital_arithmetic(self):
        for source, token in (("analog V(out)=w*i;", "w*i"),
                              ("digital {out=a^b;}", "a^b"),
                              ("analog {local=w*i; V(out)=local;}", "w*i"),
                              ("R1(1k) between(count, wire);", "count")):
            with self.subTest(source=source):
                self.assertNotIn("Wire counter", SourceInfo(source).hover_help(0, source.index(token)))
                self.assertNotIn("Reuse counter", SourceInfo(source).hover_help(0, source.index(token)))

    def test_unicode_before_a_counter_does_not_shift_its_hover_terminal(self):
        source = 'include("😀.ckt"); R1(1k) between(input,w*i);'
        column = len(source[:source.index("w*i")].encode("utf-16-le")) // 2
        info = SourceInfo(source)
        self.assertEqual(info.wire_at(0,column), (1,"R1",1,"w*i"))
        self.assertIn("Advance counter i",info.hover_help(0,column))

    def test_method_hover_uses_the_selected_generated_argument_not_the_raw_spelling(self):
        source = ("circuit Stage(a,b) {Rinner(1k) between(a,b);}\n"
                  "main {\ni=count;\nStage(w*i,w*i);\n}\n")
        info = SourceInfo(source)
        circuit = compile_circuit(source,render_only=True)
        row = source.splitlines()[3]
        for index, token in enumerate(re.finditer(r"w\*i",row)):
            location = info.wire_at(3, token.start())
            self.assertEqual(location, (4,"Stage",index,"w*i"))
            names, nets = wire_trace(circuit,location)
            self.assertEqual(nets,{f"w{index+1}"})
            self.assertEqual(names,{"Rinner_Stage_1"})
        # Repeated hover compilation always starts fresh and leaves source intact.
        self.assertEqual(compile_circuit(source,render_only=True).components,circuit.components)

    def test_explicit_and_implicit_current_method_arguments_trace_the_actual_net(self):
        for reference in ("w^i","w$i","w^"):
            source = ("circuit Stage(a,b) {Rinner(1k) between(a,b);}\n"
                      "main {\ni=count;\nR1(1k) between(input,w*i);\n"
                      f"Stage({reference},w*i);\n}}\n")
            with self.subTest(reference=reference):
                circuit = compile_circuit(source,render_only=True)
                row = source.splitlines()[4]
                location = SourceInfo(source).wire_at(4,row.index(reference))
                names,nets = wire_trace(circuit,location)
                self.assertEqual(nets,{"w1"})
                self.assertTrue(names)

    def test_grammar_limits_count_tokens_to_connection_regions(self):
        grammar = json.loads((ROOT/"ide/vscode/syntaxes/OOC.json").read_text(encoding="utf-8"))["repository"]
        token = re.compile(grammar["wire-count-reference"]["match"])
        for reference in ("wi*","wi^","w*i","w^i","w$i","w^","net.part * step"):
            self.assertIsNotNone(token.search("R1 between(in,"+reference+");"))
        self.assertIsNone(token.search("analog V(out)=w*i;"))
        region = re.compile(grammar["wire-count-arguments"]["begin"])
        for source in ("Stage(w*i,w^i);", "R1(1k) between(in,w*i);", "op1(w^,w*i,w^i);"):
            self.assertIsNotNone(region.search(source))
        for source in ("analog V(out)=max(w*i,2);", "out=max(w*i,2);", "if(w*i) {"):
            self.assertIsNone(region.search(source))
        self.assertTrue(any(re.search(pattern.get("match","(?!)"),"i=count;")
                            for pattern in grammar["declarations"]["patterns"]))

    def test_native_highlighter_keeps_math_operators_outside_wire_count_formatting(self):
        from PySide6.QtGui import QTextDocument
        from PySide6.QtWidgets import QApplication
        from circuitlab.viewer.editor import CircuitHighlighter
        app = QApplication.instance() or QApplication([])
        source = "i=count; R1(1k) between(input,w*i); analog V(out)=w*i;"
        document = QTextDocument(source)
        highlighter = CircuitHighlighter(document)
        highlighter.rehighlight()
        app.processEvents()
        formats = document.firstBlock().layout().formats()
        color_at = lambda position: next((fmt.format.foreground().color().name() for fmt in formats
                                         if fmt.start <= position < fmt.start+fmt.length),None)
        self.assertEqual(color_at(source.index("count")),"#659cd7")
        self.assertEqual(color_at(source.index("w*i")),"#e3b5f5")
        self.assertNotEqual(color_at(source.rindex("w*i")),"#e3b5f5")

    def test_complete_wire_count_example_solves_the_follower_and_independent_branch(self):
        source = (ROOT/"examples/wire_counts.ckt").read_text(encoding="utf-8")
        circuit = compile_circuit(source)
        network = analyze(circuit).network
        self.assertAlmostEqual(network.voltage[network.nodes.index("wi3")],2)
        self.assertAlmostEqual(network.voltage[network.nodes.index("j1")],1)
        self.assertFalse(any("*" in net or "^" in net or "$" in net for net in circuit.nodes))


if __name__ == "__main__":
    unittest.main()
