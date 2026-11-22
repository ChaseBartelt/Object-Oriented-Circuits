"""Named argument lists expand local connections before structural validation."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.digital_argument_lists import expand_argument_lists
from circuitlab.language.digital_structure import digital_library_interfaces
from circuitlab.language.syntax import source_rows
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


BUFFER = "chip Buffer(a,y) { a=input; y=output; y=a; }"
COUNTER = """chip Counter(clk, enable, value) {
    clk, enable = input; value[4] = output;
    stored[4] = 0;
    clock(clk, 10Hz) { if (enable) { stored += 1; } }
    value = stored;
} """


class DigitalArgumentListsTest(unittest.TestCase):
    def test_declared_indices_full_subranges_single_entries_and_mixed_arguments(self):
        rows = list(source_rows('''main { a,b,c,d = input; y=output;
            pins[7:4] = "a, b, c, d";
            Whole(pins[7:4]); Whole(pins);
            Sub(pins[6:4], y); Single(pins[5]);
            Mixed(y, pins[7], pins[5:4], a); }''', legacy_markers=False))[1:-1]
        normalized = expand_argument_lists(rows)
        self.assertEqual([row[0] for row in normalized[2:]], [
            "Whole(a, b, c, d)", "Whole(a, b, c, d)", "Sub(b, c, d, y)",
            "Single(c)", "Mixed(y, a, c, d, a)",
        ])
        self.assertEqual(normalized[-1][1:], rows[-1][1:])

    def test_declared_index_selection_connects_the_expected_real_hardware(self):
        source = '''chip Pack(a,b,c,y) {
            a,b,c=input; y[3]=output;
            y=(a ? 4 : 0) | (b ? 2 : 0) | c;
        }
        main {
            a,b,c,d=input; y[3]=output;
            pins[3:0]="a,b,c,d";
            Pack(pins[2:0], y);
        }'''
        sim = DigitalSimulation(source)
        for name, value in (("a", 1), ("b", 0), ("c", 1), ("d", 1)):
            sim.set_input(name, value)
        self.assertEqual(sim.values["y"], 3)  # b,c,d, rather than a,b,c.

    def test_aliases_have_local_scopes_and_preserve_independent_child_state(self):
        source = COUNTER + '''chip Wrapper(clk, enable, value) {
            clk,enable=input; value[4]=output;
            pins[7:5]="clk,enable,value";
            Counter(pins);
        }
        main {
            clk,a,b=input; first[4],second[4]=output;
            pins[2:0]="clk,a,first";
            Wrapper(pins[2:0]); Wrapper(clk,b,second);
        }'''
        sim = DigitalSimulation(source)
        sim.set_input("a", 1)
        sim.advance(3)
        self.assertEqual((sim.values["first"], sim.values["second"]), (3, 0))
        sim.set_input("a", 0)
        sim.set_input("b", 1)
        sim.advance(2)
        self.assertEqual((sim.values["first"], sim.values["second"]), (3, 2))
        counters = [part for part in sim.instances if part.name == "Counter"]
        self.assertEqual(len(counters), 2)
        self.assertNotEqual(counters[0].signal_bindings, counters[1].signal_bindings)
        self.assertFalse(any(name.endswith("pins") for name in sim.signals))
        self.assertNotIn('"', export_verilog(source))

    def test_multiline_quotes_and_grouped_ports_keep_original_lines(self):
        source = BUFFER + '''
main {
    input { a; } output { y; }
    pins[1:0] = "
        a,
        y
    ";
    Buffer(pins[1:0]);
}'''
        rows = list(source_rows(source, legacy_markers=False))
        declaration = next(row for row in rows if row[0].startswith("pins"))
        self.assertEqual(declaration[1], 4)
        call = next(row for row in rows if row[0].startswith("Buffer(pins"))
        self.assertEqual(call[1], 8)
        sim = DigitalSimulation(source)
        sim.set_input("a", 1)
        self.assertEqual(sim.values["y"], 1)
        self.assertEqual(sim.instances[0].line, 8)

    def test_loop_generated_lists_work_inside_chips_and_main(self):
        source = BUFFER + '''chip Pair(a0,a1,b0,b1) {
            a0,a1=input; b0,b1=output;
            for (int i=0; i<2; i++) {
                args${i}[1:0]="a${i},b${i}";
                Buffer(args${i});
            }
        }
        main {
            a=input;
            for (int i=0; i<2; i++) {
                y${i}=output;
                args${i}[1:0]="a,y${i}";
                Buffer(args${i}[1:0]);
            }
            b0,b1=output;
            Pair(a,a,b0,b1);
        }'''
        sim = DigitalSimulation(source)
        sim.set_input("a", 1)
        self.assertEqual(tuple(sim.values[name] for name in ("y0", "y1", "b0", "b1")), (1, 1, 1, 1))

    def test_vector_shape_and_old_numbered_pin_bank_calls_are_preserved(self):
        source = '''chip Copy(a,y) { a[4]=input; y[4]=output; y=a; }
        chip And(a,b,y) { a,b=input; y=output; y=a&b; }
        main {
            data[4],SW[1:0]=input; out[4],y=output;
            pins[1:0]="data,out";
            Copy(pins); And(SW[1:0],y);
        }'''
        sim = DigitalSimulation(source)
        sim.set_input("data", 10)
        sim.set_input("SW0", 1)
        sim.set_input("SW1", 1)
        self.assertEqual((sim.values["out"], sim.values["y"]), (10, 1))
        with self.assertRaises(CircuitError):
            DigitalSimulation(source.replace("data[4]", "data[3]"))

    def test_bad_counts_ranges_members_and_collisions_are_rejected(self):
        declarations = (
            'pins[3:0]="a,y"', 'pins[0:1]="a,y"', 'pins[1:0]="a,missing"',
            'pins[1:0]="a,a[0]"', 'pins[1:0]="a,"', 'pins[4096:4095]="a,y"',
            'pins[999999999999999999999:0]="a,y"', 'pins[-1:0]="a,y"',
            'pins[one:0]="a,y"', 'pins[1:0]="a,__import__(os)"',
            'pins[1:0]="a,y"; pins[1:0]="a,y"', 'a[1:0]="a,y"',
            'pins[2]="a,y"', 'pins="a,y"',
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration), self.assertRaises(CircuitError):
                DigitalSimulation(BUFFER + f'main {{ a=input; y=output; {declaration}; Buffer(a,y); }}')
        with self.assertRaisesRegex(CircuitError, "numbered pin bank"):
            DigitalSimulation(BUFFER + 'main { SW[1:0]=input; y=output; SW[1:0]="SW1,SW0"; Buffer(SW0,y); }')

    def test_bad_references_do_not_become_hardware_or_escape_the_declared_range(self):
        for reference in ("pins[4:0]", "pins[1:0]", "pins[5]", "pins[2:3]", "pins[-1]", "pins[a]"):
            with self.subTest(reference=reference), self.assertRaises(CircuitError):
                DigitalSimulation(BUFFER + f'main {{ a=input; y=output; pins[3:2]="a,y"; Buffer({reference}); }}')
        with self.assertRaisesRegex(CircuitError, "chip-call arguments"):
            DigitalSimulation('chip Bad(a,y) { a=input; y=output; pins[1:0]="a,y"; y=pins[1]; } main { a=input;y=output;Bad(a,y); }')
        with self.assertRaisesRegex(CircuitError, "directly inside"):
            DigitalSimulation('chip Bad(clk,a,y) { clk,a=input;y=output; clock(clk,10Hz) { pins[1:0]="a,y"; } y=a; } main { clk,a=input;y=output;Bad(clk,a,y); }')

    def test_include_errors_keep_the_original_file_and_declaration_or_call_line(self):
        entry = Path.cwd() / "argument-main.ckt"
        library = entry.with_name("argument-library.ckt")
        for definition, call, expected_line in (
            ('pins[1:0]="a,missing"', 'Buffer(a,y)', 3),
            ('pins[1:0]="a,y"', 'Buffer(pins[2:0])', 4),
        ):
            source = f'chip Wrapper(a,y) {{\na=input; y=output;\n{definition};\n{call};\n}}'
            session = CircuitSession(path=entry, source=BUFFER + '\ninclude("argument-library.ckt");\nmain {a=input;y=output;Wrapper(a,y);}',
                                     read_source={library: source}.__getitem__)
            session.compile()
            self.assertEqual(len(session.issues), 1)
            self.assertEqual((session.issues[0].path, session.issues[0].line), (library, expected_line))

    def test_comment_markers_inside_multiline_lists_are_literal_invalid_members(self):
        for marker in ("// not a signal", "/* not a signal */", "# not a signal"):
            source = BUFFER + ('\nmain {\na=input; y=output;\npins[1:0]="a,\n'
                               + marker + '\ny";\nBuffer(pins);\n}')
            with self.subTest(marker=marker), self.assertRaisesRegex(CircuitError, "plain signal names") as caught:
                DigitalSimulation(source)
            self.assertEqual(caught.exception.line, 4)

    def test_unterminated_multiline_lists_report_the_opening_line(self):
        source = BUFFER + '\nmain {\na=input; y=output;\npins[1:0]="a,y;\nBuffer(pins);\n}'
        with self.assertRaisesRegex(CircuitError, "quoted") as caught:
            DigitalSimulation(source)
        self.assertEqual(caught.exception.line, 4)

    def test_generated_structural_library_previews_and_runtime_call_exclusion(self):
        source = '''chip Generated(a,y) { a=input;y=output;
            for (int i=0; i<1; i++) {
                pins${i}[1:0]="a,y"; Missing(pins${i});
            }
        }'''
        interfaces = digital_library_interfaces(source)
        self.assertEqual(interfaces[0][0], "Generated")
        self.assertIn("Missing(a, y)", interfaces[0][2])
        self.assertFalse(any('"' in row for row in interfaces[0][2]))
        self.assertIsNone(digital_library_interfaces(source.replace("i<1", "i<0")))
        self.assertIsNone(digital_library_interfaces(
            'chip Clocked(clk,a,y) {clk,a=input;y=output; for(int i=0;i<1;i++){clock(clk,10Hz){Missing(a,y);}}}'))


if __name__ == "__main__":
    unittest.main()
