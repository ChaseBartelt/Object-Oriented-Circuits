"""Chip-local int names reuse literal widths without becoming mutable state."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.digital_constants import normalize_integer_constants
from circuitlab.language.syntax import Binding
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


class DigitalIntegerConstantsTest(unittest.TestCase):
    def test_normalization_keeps_literal_width_and_source_metadata(self):
        binding = Binding(path=Path("constants.ckt"))
        rows = [("int MASK = 65_504", 12, binding), ("int ZERO = 0", 13, binding)]
        self.assertEqual(normalize_integer_constants(rows), [
            ("const MASK[16] = 65_504", 12, binding),
            ("const ZERO[1] = 0", 13, binding),
        ])

    def test_assembled_constants_are_visible_as_constants_with_correct_values(self):
        source = '''chip Masked(a,y) {
            input { a[16]; } output { y[16]; }
            int MASK = 65_504;
            y = a & MASK;
        }
        main {a[16]=input;y[16]=output;Masked(a,y);}'''
        sim = DigitalSimulation(source)
        sim.set_input("a", 65535)
        self.assertEqual(sim.values["y"], 65504)
        source_name = dict(sim.instances[0].signal_bindings)["MASK"]
        live = sim.source_bindings[source_name]
        self.assertEqual((sim.signals[live], sim.signal_kinds[live], sim.values[live]),
                         (16, "constant", 65504))
        self.assertIn("localparam [15:0]", export_verilog(source))

    def test_named_literals_preserve_intermediate_unsigned_overflow(self):
        source = '''chip Sum(a,y) { a[8]=input; y[16]=output;
            int ONE = 1; y=(a + ONE) << 1;
        } main {a[8]=input;y[16]=output;Sum(a,y);}'''
        named = DigitalSimulation(source)
        literal = DigitalSimulation(source.replace("int ONE = 1;", "").replace("a + ONE", "a + 1"))
        for value in (0, 1, 127, 128, 254, 255):
            named.set_input("a", value)
            literal.set_input("a", value)
            self.assertEqual(named.values["y"], literal.values["y"])
        self.assertEqual(named.values["y"], 0)

    def test_chip_integer_constants_are_available_to_static_loop_bounds(self):
        sim = DigitalSimulation('''chip Copy(a,y) {
            a[4]=input; y[4]=output;
            int COUNT = 4;
            for (int i=0; i<COUNT; i++) { y[i]=a[i]; }
        } main {a[4]=input;y[4]=output;Copy(a,y);}''')
        sim.set_input("a", 13)
        self.assertEqual(sim.values["y"], 13)

    def test_main_and_for_integer_variables_keep_existing_generation_semantics(self):
        source = '''chip Buffer(a,y){a=input;y=output;y=a;}
        main {
            a=input;
            int COUNT=2;
            for(int i=0; i<COUNT; i++) {
                y${i}=output;
                Buffer(a,y${i});
            }
        }'''
        sim = DigitalSimulation(source)
        sim.set_input("a", 1)
        self.assertEqual((sim.values["y0"], sim.values["y1"]), (1, 1))
        self.assertNotIn("COUNT", sim.signals)
        self.assertNotIn("i", sim.signals)

    def test_behavior_only_chips_support_constants_without_main(self):
        source = 'chip Masked(a,y){a[8]=input;y[8]=output;int MASK=240;y=a & MASK;}'
        sim = DigitalSimulation(source)
        sim.set_input("a", 255)
        self.assertEqual(sim.values["y"], 240)

    def test_existing_signals_named_int_remain_valid(self):
        sim = DigitalSimulation('''chip Legacy(int,y) {
            input {int[4];} output {y[4];} y=int;
        }''')
        sim.set_input("int", 11)
        self.assertEqual(sim.values["y"], 11)

    def test_constants_cannot_be_redeclared_or_assigned(self):
        mutations = ("int LIMIT=2;", "const LIMIT[2]=2;", "LIMIT=2;",
                     "LIMIT[0]=0;", "clock(clk,10Hz){LIMIT+=1;}")
        for mutation in mutations:
            source = ('chip Bad(clk,a,y){clk,a=input;y=output;int LIMIT=1;'
                      + mutation + 'y=a;} main {clk,a=input;y=output;Bad(clk,a,y);}')
            with self.subTest(mutation=mutation), self.assertRaises(CircuitError):
                DigitalSimulation(source)

    def test_invalid_initializers_are_source_diagnostics(self):
        declarations = ('int MASK;', 'int MASK=-1;', 'int MASK=0xff;', 'int MASK=1e3;',
                        'int MASK=1+2;', 'int MASK=OTHER;', 'int MASK=true;',
                        'int MASK="1";', 'int MASK[4]=3;', 'int wire=3;',
                        'int MASK=' + '9' * 5000 + ';',
                        'int MASK=' + str(1 << 4096) + ';')
        for declaration in declarations:
            source = ('chip Bad(a,y){\na=input;y=output;\n' + declaration
                      + '\ny=a;} main{a=input;y=output;Bad(a,y);}')
            with self.subTest(declaration=declaration[:40]), self.assertRaises(CircuitError) as caught:
                DigitalSimulation(source)
            self.assertEqual(caught.exception.line, 3)

    def test_runtime_blocks_cannot_declare_integer_constants(self):
        source = '''chip Bad(clk,a,y) {
            clk,a=input;y=output;
            clock(clk,10Hz) { int MASK=1; }
            y=a;
        } main {clk,a=input;y=output;Bad(clk,a,y);}'''
        with self.assertRaisesRegex(CircuitError, "directly inside a chip") as caught:
            DigitalSimulation(source)
        self.assertEqual(caught.exception.line, 3)

    def test_included_chip_constant_errors_keep_the_file_and_line(self):
        entry = Path.cwd() / "int-main.ckt"
        library = entry.with_name("int-chip.ckt")
        content = 'chip Bad(a,y){\na=input;y=output;\nint MASK=-1;\ny=a;}'
        session = CircuitSession(path=entry, source='include("int-chip.ckt");\nmain{a=input;y=output;Bad(a,y);}',
                                 read_source={library: content}.__getitem__)
        session.compile()
        self.assertEqual(len(session.issues), 1)
        self.assertEqual((session.issues[0].path, session.issues[0].line), (library, 3))


if __name__ == "__main__":
    unittest.main()
