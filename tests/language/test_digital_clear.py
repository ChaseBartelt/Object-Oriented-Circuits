"""Clocked clear groups preserve ordinary zero-assignment HDL and ordering."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.digital_clear import expand_clear_blocks
from circuitlab.language.syntax import Binding
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


MODEL = '''chip Stored(clk,reset,index,data,first,second,third,flags_out,count_out,active_out) {
    clk,reset=input; index[2],data[8]=input;
    first[8],second[8],third[8],flags_out[8],count_out[4],active_out=output;
    words[3][8]=17; flags[8]=165; count[4]=9; active=1;
    clock(clk,10Hz) {
        if(reset) { @RESET@ }
        else { words[index]=data; count+=1; flags=flags ^ 3; active=!active; }
    }
    first=words[0]; second=words[1]; third=words[2];
    flags_out=flags; count_out=count; active_out=active;
}'''
EXPLICIT_RESET = 'for(int i=0;i<3;i++){words[i]=0;} active=0; count=0; flags[7:4]=0;'
CLEAR_RESET = 'clear {words; active,count; flags[7:4];};'


class DigitalClearTest(unittest.TestCase):
    def test_scalar_array_and_slice_reset_emits_exactly_the_existing_hdl(self):
        explicit = MODEL.replace("@RESET@", EXPLICIT_RESET)
        shorthand = MODEL.replace("@RESET@", CLEAR_RESET)
        self.assertEqual(export_verilog(shorthand), export_verilog(explicit))
        expected, actual = DigitalSimulation(explicit), DigitalSimulation(shorthand)
        for reset, index, data in ((0, 0, 90), (0, 1, 195), (1, 2, 255),
                                   (0, 2, 36), (0, 0, 128), (1, 0, 0)):
            for sim in (expected, actual):
                sim.set_input("reset", reset)
                sim.set_input("index", index)
                sim.set_input("data", data)
                sim.step()
            self.assertEqual(actual.values, expected.values)
        self.assertEqual(tuple(actual.values[name] for name in ("first", "second", "third", "count_out", "active_out")),
                         (0, 0, 0, 0, 0))

    def test_mixed_partial_and_full_writes_keep_last_assignment_and_preedge_rules(self):
        template = '''chip Writes(clk,a,first,second,flags_out) {
            clk=input; a[8]=input; first[8],second[8],flags_out[8]=output;
            words[2][8]=255; flags[8]=165;
            clock(clk,10Hz) {
                flags=a;
                @CLEAR@
                flags[0]=a[7]; words[1]=a;
            }
            first=words[0]; second=words[1]; flags_out=flags;
        }'''
        shorthand = template.replace("@CLEAR@", "clear {flags; words;};")
        explicit = template.replace("@CLEAR@", "flags=0; words[0]=0; words[1]=0;")
        self.assertEqual(export_verilog(shorthand), export_verilog(explicit))
        sim = DigitalSimulation(shorthand)
        for value in (0, 255, 81, 128):
            sim.set_input("a", value)
            sim.step()
            self.assertEqual((sim.values["first"], sim.values["second"], sim.values["flags_out"]),
                             (0, value, value >> 7))

    def test_selected_array_words_bits_and_nested_indices_use_existing_targets(self):
        template = '''chip Selected(clk,index,y,z) {
            clk=input; index[2]=input; y[8],z[8]=output;
            words[2][8]=255; flags[8]=255;
            clock(clk,10Hz) { @CLEAR@ }
            y=words[0]; z=flags;
        }'''
        targets = "words[index[0]][7:4], flags[0]"
        explicit = "words[index[0]][7:4]=0; flags[0]=0;"
        self.assertEqual(export_verilog(template.replace("@CLEAR@", f"clear {{{targets};}};")),
                         export_verilog(template.replace("@CLEAR@", explicit)))

    def test_loop_generated_clear_targets_keep_the_existing_hdl(self):
        template = '''chip Generated(clk,y) {
            clk=input;y[4]=output;
            for(int i=0;i<2;i++) { word${i}[4]=15; }
            clock(clk,10Hz) {
                for(int i=0;i<2;i++) { @CLEAR@ }
            }
            y=word0 | word1;
        }'''
        self.assertEqual(export_verilog(template.replace("@CLEAR@", "clear {word${i};};")),
                         export_verilog(template.replace("@CLEAR@", "word${i}=0;")))

    def test_array_expansion_preserves_each_target_source_binding(self):
        origin = Binding(path=Path("clear-source.ckt"))
        rows = [("words[2][8]=0", 2, origin), ("clock(clk) {", 3, origin),
                ("clear {", 4, origin), ("words, flags[3:0]", 5, origin),
                ("}", 6, origin), ("}", 7, origin)]
        self.assertEqual(expand_clear_blocks(rows)[2:5], [
            ("words[0] = 0", 5, origin), ("words[1] = 0", 5, origin),
            ("flags[3:0] = 0", 5, origin),
        ])

    def test_clear_identifier_keeps_its_existing_state_meaning(self):
        sim = DigitalSimulation('chip Old(clk,y){clk=input;y[4]=output;clear[4]=3;clock(clk,10Hz){clear+=1;}y=clear;}')
        sim.step()
        self.assertEqual(sim.values["y"], 4)

    def test_outside_clock_empty_nested_and_malformed_targets_are_rejected(self):
        blocks = ("clear {q;}", "clock(clk,10Hz){clear{};}",
                  "clock(clk,10Hz){clear{q,,q;};}", "clock(clk,10Hz){clear{q=0;};}",
                  "clock(clk,10Hz){clear{clear{q;}};}", "clock(clk,10Hz){clear{call(q);};}",
                  "clock(clk,10Hz){clear{q[];};}")
        for block in blocks:
            source = 'chip Bad(clk,y){\nclk=input;y[4]=output;q[4]=15;\n' + block + '\ny=q;}'
            with self.subTest(block=block), self.assertRaises(CircuitError) as caught:
                DigitalSimulation(source)
            self.assertEqual(caught.exception.line, 3)

    def test_unknown_input_constant_and_out_of_bounds_targets_use_assignment_checks(self):
        for target in ("missing", "a", "MASK", "q[9]", "words[7]", "words[0][9]"):
            source = ('chip Bad(clk,a,y){clk,a=input;y[4]=output;q[4]=15;words[2][4]=15;const MASK[4]=15;\n'
                      + 'clock(clk,10Hz){clear{' + target + ';};}\ny=q;}')
            with self.subTest(target=target), self.assertRaises(CircuitError) as caught:
                DigitalSimulation(source)
            self.assertEqual(caught.exception.line, 2)

    def test_include_error_keeps_the_original_target_file_and_line(self):
        entry = Path.cwd() / "clear-main.ckt"
        library = entry.with_name("clear-library.ckt")
        content = ('chip Bad(clk,a,y){\nclk,a=input;y=output;\nclock(clk,10Hz){\nclear{\nmissing;\n};\n}\ny=a;}')
        session = CircuitSession(path=entry, source='include("clear-library.ckt");\nmain{clk,a=input;y=output;Bad(clk,a,y);}',
                                 read_source={library: content}.__getitem__)
        session.compile()
        self.assertEqual(len(session.issues), 1)
        self.assertEqual((session.issues[0].path, session.issues[0].line), (library, 5))


if __name__ == "__main__":
    unittest.main()
