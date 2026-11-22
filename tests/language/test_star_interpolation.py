"""Braced *{expression} is an exact alias of existing ${expression} generation."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.control_flow import expand_digital_loops
from circuitlab.language.syntax import source_rows
from circuitlab.math.analysis import analyze
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession
from circuitlab.tooling.source_info import accepts_calls, methods


class StarInterpolationTest(unittest.TestCase):
    def test_chip_names_offset_expressions_and_equations_emit_identical_hdl(self):
        source = '''chip Copy(a,y) {
            a[8]=input;y[8]=output;
            for(int i=0;i<4;i++) {
                R*{i + 1}[2]=wire;
                R*{i + 1}=a[2*i+1:2*i];
                y[2*i+1:2*i]=R*{i + 1};
            }
        }'''
        self.assertEqual(export_verilog(source), export_verilog(source.replace("*{", "${")))
        sim = DigitalSimulation(source)
        sim.set_input("a", 225)
        self.assertEqual(sim.values["y"], 225)

    def test_main_groups_and_multiline_generated_pin_lists_preserve_hdl(self):
        source = '''chip Buffer(a,y){a[4]=input;y[4]=output;y=a;}
        main {
            input {a[4];}
            for(int i=0;i<2;i++) {
                output {y*{i}[4];}
                args*{i}[1:0]="
                    a,y*{i}
                ";
                Buffer(args*{i});
            }
        }'''
        self.assertEqual(export_verilog(source), export_verilog(source.replace("*{", "${")))
        sim = DigitalSimulation(source)
        sim.set_input("a", 13)
        self.assertEqual((sim.values["y0"], sim.values["y1"]), (13, 13))

    def test_typed_electrical_parts_and_generated_connections_match_existing_syntax(self):
        source = '''circuit Stage(r,a,b){r between(a,b);}
        main {for(int i=0;i<2;i++){Stage(R*{i}(1k),input,node*{i});}}'''
        actual = compile_circuit(source)
        expected = compile_circuit(source.replace("*{", "${"))
        self.assertEqual([(part.name, part.value, part.terminal_nets) for part in actual.components],
                         [(part.name, part.value, part.terminal_nets) for part in expected.components])

    def test_braced_counter_reads_do_not_increment_or_change_left_to_right_order(self):
        source = '''main {
            i=count;
            R*{i}(1k) between(node*{i}*i,0);
            R*{i}(1k) between(node*{i}*i,0);
            Rlast(1k) between(node^i,0);
        }'''
        actual = compile_circuit(source)
        expected = compile_circuit(source.replace("*{", "${"))
        self.assertEqual([(part.name, part.terminal_nets) for part in actual.components],
                         [(part.name, part.terminal_nets) for part in expected.components])
        self.assertEqual([part.name for part in actual.components], ["R0", "R1", "Rlast"])
        self.assertEqual(actual.components[-1].p, "node2")

    def test_static_multiplication_and_analog_math_keep_their_values(self):
        source = '''main {int base=3;
            for(int i=0;i<3;i++){R*{i}(*{base * (i+1)}) between(input,node*{i});}
        }'''
        self.assertEqual([part.value for part in compile_circuit(source).components], [3, 6, 9])
        circuit = compile_circuit('V1(2) between(input,0);analog V(out)=V(input)*10;R1(1k) between(out,0);')
        network = analyze(circuit).network
        self.assertEqual(network.voltage[network.nodes.index("out")], 20)
        rows = [("for(int i=0;i<1;i++){", 1), ("a *= 2", 2), ("y=Ri*2", 3), ("Ri*[4]=wire", 4), ("}", 5)]
        self.assertEqual(expand_digital_loops(rows), [("a *= 2", 2), ("y=Ri*2", 3), ("Ri*[4]=wire", 4)])

    def test_comments_and_quoted_text_keep_existing_source_row_behavior(self):
        source = '''chip Copy(a,y){a=input;y=output;
            // R*{missing} is only a comment.
            y=a;
        }
        include("path*{literal}.ckt");'''
        actual = list(source_rows(source, legacy_markers=False))
        expected = list(source_rows(source.replace("*{", "${"), legacy_markers=False))
        self.assertEqual([(text.replace("*{", "${"), line) for text, line, _ in actual],
                         [(text, line) for text, line, _ in expected])
        self.assertTrue(any('"path*{literal}.ckt"' in row[0] for row in actual))

    def test_multiline_generated_metadata_still_rejects_literal_comment_markers(self):
        for opener in ("${", "*{"):
            source = ('chip Buffer(a,y){a=input;y=output;y=a;}\nmain{a=input;y=output;\n'
                      'for(int i=0;i<1;i++){\nargs' + opener + 'i}[1:0]="a,\n// invalid member\ny";\nBuffer(a,y);}}')
            with self.subTest(opener=opener), self.assertRaisesRegex(CircuitError, "plain signal names") as caught:
                DigitalSimulation(source)
            self.assertEqual(caught.exception.line, 4)

    def test_included_bad_expressions_and_unclosed_markers_keep_original_locations(self):
        entry = Path.cwd() / "star-main.ckt"
        library = entry.with_name("star-library.ckt")
        for token in ("*{missing}", "*{call(i)}", "*{i +}", "*{i"):
            content = ('chip Bad(a,y){\na=input;y=output;\nfor(int i=0;i<1;i++){\nR'
                       + token + '[4]=wire;\n}\ny=a;}')
            session = CircuitSession(path=entry, source='include("star-library.ckt");\nmain{a=input;y=output;Bad(a,y);}',
                                     read_source={library: content}.__getitem__)
            session.compile()
            self.assertEqual(len(session.issues), 1)
            self.assertEqual((session.issues[0].path, session.issues[0].line), (library, 4))

    def test_source_inspection_retains_method_and_call_context(self):
        source = 'circuit Stage(a,b){R1(1k) between(a,b);}\nmain{for(int i=0;i<2;i++){Ri*{i}(1k) between(a,b);'
        self.assertEqual(methods(source), methods(source.replace("*{", "${")))
        self.assertTrue(accepts_calls(source))
        self.assertEqual(accepts_calls(source), accepts_calls(source.replace("*{", "${")))


if __name__ == "__main__":
    unittest.main()
