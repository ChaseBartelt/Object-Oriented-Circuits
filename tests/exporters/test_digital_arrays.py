"""Word arrays share the exported HDL's clock and signal semantics."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError


MEMORY = """chip Memory(clk, wr, rd, index, data, y) {
    clk, wr = input;
    rd[64], index[64], data[16] = input;
    y[12] = output;
    words[5][12] = 10;
    y = words[rd];
    clock(clk, 10kHz) {
        if (wr) { words[index] = data; }
    }
}"""


class DigitalArrayTest(unittest.TestCase):
    def test_dynamic_access_preserves_full_index_and_word_width(self):
        sim = DigitalSimulation(MEMORY)
        sim.set_input("wr", 1)
        sim.set_input("index", 1)
        sim.set_input("data", 0xA567)
        sim.set_input("rd", 1)
        self.assertEqual(sim.values["y"], 10)
        sim.step()
        self.assertEqual(sim.values["y"], 0x567)
        for invalid in (5, 9, (1 << 48) | 1):
            with self.subTest(index=invalid):
                sim.set_input("rd", invalid)
                self.assertEqual(sim.values["y"], 0)
                sim.set_input("index", invalid)
                sim.set_input("data", 13)
                sim.step()
                sim.set_input("rd", 1)
                self.assertEqual(sim.values["y"], 0x567)
        sim.reset()
        self.assertEqual(sim.values["y"], 10)

    def test_nested_indices_slices_and_old_state_write_order(self):
        source = """chip Swap(clk, en, select, y, first, second) {
            clk, en = input;
            select[3] = input;
            y[4], first[8], second[8] = output;
            words[3][8];
            keys[2][2] = 1;
            y = words[keys[select[0]]][7:4];
            first = words[0];
            second = words[1];
            clock(clk, 10kHz) {
                if (en) {
                    words[select[1:0]] = words[1];
                    words[1] = words[select[1:0]] + 1;
                    words[select[1:0]][7:4] = 3;
                }
            }
        }"""
        sim = DigitalSimulation(source)
        sim.set_input("en", 1)
        sim.step()
        self.assertEqual((sim.values["first"], sim.values["second"]), (48, 1))
        sim.step()
        self.assertEqual((sim.values["first"], sim.values["second"]), (49, 49))
        self.assertEqual(sim.values["y"], 3)
        sim.set_input("select", 1)
        sim.step()
        self.assertEqual((sim.values["first"], sim.values["second"]), (49, 50))

    def test_instances_keep_independent_array_state(self):
        source = MEMORY + """
        main DualMemory {
            CLK, load_a, load_b = input;
            rd[64], index[64], data[16] = input;
            a[12], b[12] = output;
            Memory(CLK, load_a, rd, index, data, a);
            Memory(CLK, load_b, rd, index, data, b);
        }"""
        sim = DigitalSimulation(source)
        sim.set_input("load_b", 1)
        sim.set_input("data", 33)
        sim.step()
        self.assertEqual((sim.values["a"], sim.values["b"]), (10, 33))
        self.assertEqual(len(sim.instances), 2)
        self.assertTrue(export_verilog(source).startswith("module DualMemory ("))

    def test_large_selection_and_compound_writes_use_pre_edge_index(self):
        sim = DigitalSimulation("""chip Bank(clk, y, first, last) {
            clk = input;
            y[8], first[8], last[8] = output;
            words[64][8] = 254;
            cursor[8] = 63;
            y = words[cursor];
            first = words[0];
            last = words[63];
            clock(clk, 10kHz) {
                cursor += 1;
                words[cursor] += 2;
            }
        }""")
        self.assertEqual(sim.values["y"], 254)
        sim.step()
        self.assertEqual((sim.values["cursor"], sim.values["y"], sim.values["last"]), (64, 0, 0))
        sim.step()
        self.assertEqual(sim.values["first"], 254)

    def test_lowering_respects_pin_banks_and_builtin_calls(self):
        chip = """chip Memory(y, glyph) {
            y[8], glyph[8] = output;
            mem[2][8] = 7;
            mem_word[1:0] = wire;
            mem_word[1:0] = 1;
            sevenseg[1][4] = 7;
            y = mem[mem_word[1:0]];
            glyph = sevenseg(sevenseg[0]);
        }"""
        main = "main { y[8], glyph[8] = output; Memory(y, glyph); }"
        for source in (chip, chip + "\n" + main):
            with self.subTest(assembled=source != chip):
                sim = DigitalSimulation(source)
                self.assertEqual((sim.values["y"], sim.values["glyph"]), (7, 0xF8))

    def test_invalid_array_usage_reports_the_source_line(self):
        for body in ("words[0][8];", "words[2][0];", "words[2][4] = 16;",
                     "words[2][8]; y = words[2];",
                     "words[2][8]; words[0] = 1; y = words[0];"):
            with self.subTest(body=body), self.assertRaises(CircuitError) as error:
                export_verilog(f"chip Bad(y) {{\ny[8] = output;\n{body}\n}}")
            self.assertEqual(error.exception.line, 3)

    def test_octant_registers_and_ram_are_live(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "examples" / "advanced" / "octant" / "octant_cpu.ckt"
        sim = DigitalSimulation(path.read_text(encoding="utf-8"))
        sim.set_input("TRIP2", 1)
        sim.set_input("TRIP5", 1)
        sim.advance(7)
        self.assertEqual(sim.values["R3"], 0x24)
        sim.advance(8)
        self.assertEqual(sim.values["PENDING"], 0)
        self.assertEqual(sim.values["LAST_ACK"], 0x24)
        self.assertEqual((sim.values["LOG_MASK0"], sim.values["LOG_COUNT0"]), (0x24, 2))
        self.assertEqual(sim.values["R0"], 0)
