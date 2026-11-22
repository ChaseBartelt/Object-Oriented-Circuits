"""Grouped directions lower to the existing digital port declarations."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.language.digital_declarations import normalize_port_blocks
from circuitlab.language.digital_structure import digital_library_interfaces
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession


GROUPED = """chip Grouped(CLK, RESET, IRQ_LINES, PC, MSP, IR, NZCV, BOOTED, FAULT) {
    input { CLK, RESET; IRQ_LINES[240]; }
    output { PC[32], MSP[32]; IR[16], NZCV[4]; BOOTED, FAULT; }
    count[32] = 0;
    clock(CLK, 10Hz) {
        if (RESET) { count = 0; }
        else { count += 1; }
    }
    PC = count; MSP = IRQ_LINES[31:0]; IR = count[15:0];
    NZCV = 0; BOOTED = count != 0; FAULT = RESET;
}"""
LEGACY = GROUPED.replace("input { CLK, RESET; IRQ_LINES[240]; }",
                         "CLK, RESET = input; IRQ_LINES[240] = input;").replace(
    "output { PC[32], MSP[32]; IR[16], NZCV[4]; BOOTED, FAULT; }",
    "PC[32], MSP[32] = output; IR[16], NZCV[4] = output; BOOTED, FAULT = output;")


class DigitalPortGroupsTest(unittest.TestCase):
    def test_named_wire_groups_preserve_connections_and_static_generation(self):
        sim = DigitalSimulation('''chip And(a, b, y) {
            input { a, b; } output { y; }
            wire intermediates { for (int i=0; i<2; i++) { part${i}; } }
            part0 = a; part1 = b; y = part0 & part1;
        }
        main {
            input controls { A, B; } output status { Y; }
            wire bus { a, b; }
            And(A, B, a); And(a, B, Y); And(A, A, b);
        }''')
        sim.set_input('A', 1)
        self.assertEqual(sim.values['Y'], 0)
        sim.set_input('B', 1)
        self.assertEqual(sim.values['Y'], 1)
        self.assertNotIn('intermediates', sim.signals)

    def test_optional_group_names_preserve_hdl_and_interface_previews(self):
        named = GROUPED.replace('input {', 'input controls {').replace('output {', 'output status {')
        self.assertEqual(export_verilog(named), export_verilog(GROUPED))
        parent = 'chip Parent(a,y) { input bus_addr { a[8]; } output bus_wdata { y[8]; } Child(a,y); }'
        session = CircuitSession(source=parent)
        session.compile()
        self.assertFalse(session.issues)
        self.assertTrue(session.interface_preview)

    def test_chip_groups_export_identical_hdl_and_preserve_clocked_values(self):
        self.assertEqual(export_verilog(GROUPED), export_verilog(LEGACY))
        sim = DigitalSimulation(GROUPED)
        self.assertEqual(sim.signals["IRQ_LINES"], 240)
        self.assertEqual(sim.signals["IR"], 16)
        sim.set_input("IRQ_LINES", (1 << 239) | 0x12345678)
        sim.advance(3)
        self.assertEqual((sim.values["PC"], sim.values["MSP"], sim.values["IR"],
                          sim.values["NZCV"], sim.values["BOOTED"]),
                         (3, 0x12345678, 3, 0, 1))
        sim.set_input("RESET", 1)
        sim.step()
        self.assertEqual((sim.values["PC"], sim.values["BOOTED"], sim.values["FAULT"]), (0, 0, 1))

    def test_groups_mix_with_existing_declarations(self):
        sim = DigitalSimulation("""chip Mixed(a, b, y, z) {
            input { a[8]; } b[8] = input;
            output { y[8]; } z[8] = output;
            y = a + b; z = a ^ b;
        }""")
        sim.set_input("a", 250)
        sim.set_input("b", 12)
        self.assertEqual((sim.values["y"], sim.values["z"]), (6, 246))

    def test_groups_keep_bank_ranges_distinct_from_vector_widths(self):
        sim = DigitalSimulation("""chip Bank(SW[3:0], data, LED[3:0], word) {
            input { SW[3:0]; data[4]; }
            output { LED[3:0]; word[4]; }
            for (int i = 0; i < 4; i++) { LED[i] = SW[i]; }
            word = data;
        }""")
        self.assertEqual(sim.inputs, ("SW3", "SW2", "SW1", "SW0", "data"))
        sim.set_input("SW2", 1)
        sim.set_input("data", 10)
        self.assertEqual((sim.values["LED2"], sim.values["LED0"], sim.values["word"]), (1, 0, 10))

    def test_static_loops_expand_grouped_member_names_and_widths(self):
        sim = DigitalSimulation("""chip Generated(pin0, pin1, out0, out1) {
            input { for (int i = 0; i < 2; i++) { pin${i}[8]; } }
            output { for (int i = 0; i < 2; i++) { out${i}[8]; } }
            for (int i = 0; i < 2; i++) { out${i} = pin${i} + ${i}; }
        }""")
        sim.set_input("pin0", 7)
        sim.set_input("pin1", 255)
        self.assertEqual((sim.values["out0"], sim.values["out1"]), (7, 0))

    def test_main_groups_include_chip_groups_without_changing_hdl(self):
        entry = Path.cwd() / "port-groups-main.ckt"
        library = entry.with_name("port-groups-library.ckt")
        chip = "chip Copy(a, y) { input { a[8]; } output { y[8]; } y = a; }"
        source = 'include("port-groups-library.ckt");\nmain Board { input { a[8]; } output { y[8]; } Copy(a,y); }'
        read = {library: chip}.__getitem__
        sim = DigitalSimulation(source, path=entry, read_source=read)
        sim.set_input("a", 173)
        self.assertEqual(sim.values["y"], 173)
        self.assertEqual(sim.instances[0].name, "Copy")
        legacy = source.replace("input { a[8]; }", "a[8]=input;").replace("output { y[8]; }", "y[8]=output;")
        old_chip = chip.replace("input { a[8]; }", "a[8]=input;").replace("output { y[8]; }", "y[8]=output;")
        self.assertEqual(export_verilog(source, path=entry, read_source=read),
                         export_verilog(legacy, path=entry, read_source={library: old_chip}.__getitem__))

    def test_structural_library_interfaces_preserve_directions_and_widths(self):
        source = "chip Parent(a,y) { input { a[8]; } output { y[8]; } Child(a,y); }"
        modules = digital_library_interfaces(source)
        self.assertEqual(modules[0][0:2], ("Parent", (("a", "input"), ("y", "output"))))
        self.assertIn("a[8] = input", modules[0][2])
        self.assertIn("y[8] = output", modules[0][2])
        session = CircuitSession(source=source)
        session.compile()
        self.assertFalse(session.issues)
        self.assertTrue(session.interface_preview)
        self.assertEqual(session.drawing.instances[0].pins, (("a", "a"), ("y", "y")))

    def test_normalization_retains_member_line_and_origin_objects(self):
        origin = object()
        self.assertEqual(normalize_port_blocks([
            ("input {", 2, origin), ("a[8], b", 5, origin), ("}", 8, origin),
            ("output {", 9, origin), ("y[8]", 12, origin), ("}", 14, origin),
        ]), [("a[8], b = input", 5, origin), ("y[8] = output", 12, origin)])

    def test_invalid_group_members_and_runtime_nesting_keep_error_lines(self):
        cases = (
            ("input {\na = 1;\n}", 3),
            ("input {\noutput { y; }\n}", 3),
            ("if (a) {\ninput { a; }\n}", 3),
            ("clock(a, 1Hz) {\noutput { y; }\n}", 3),
        )
        for body, line in cases:
            with self.subTest(body=body), self.assertRaises(CircuitError) as caught:
                export_verilog("chip Bad(a,y) {\n" + body + "\ny=a;\n}")
            self.assertEqual(caught.exception.line, line)
        with self.assertRaises(CircuitError) as caught:
            normalize_port_blocks([("output {", 23), ("y[8]", 24)])
        self.assertEqual(caught.exception.line, 23)

    def test_bad_group_declaration_in_include_reports_library_path_and_line(self):
        entry = Path.cwd() / "port-group-error-main.ckt"
        library = entry.with_name("port-group-error-library.ckt")
        session = CircuitSession(path=entry,
            source='include("port-group-error-library.ckt");\nmain { a=input; y=output; Bad(a,y); }',
            read_source={library: "chip Bad(a,y) {\ninput {\na = 1;\n}\noutput {y;}\ny=a;\n}"}.__getitem__)
        session.compile()
        self.assertEqual(len(session.issues), 1)
        self.assertEqual((session.issues[0].path, session.issues[0].line), (library, 3))

    def test_grouped_width_validation_keeps_the_original_member_line(self):
        for declaration in ("a[0]", "a[4097]", "a[1:3]"):
            with self.subTest(declaration=declaration), self.assertRaises(CircuitError) as caught:
                export_verilog("chip Bad(a,y) {\ninput {\n" + declaration +
                               ";\n}\noutput { y; }\ny=a;\n}")
            self.assertEqual(caught.exception.line, 3)


if __name__ == "__main__":
    unittest.main()
