"""Exercise the typed Cortex-M7 teaching demo through the digital simulator."""

from itertools import pairwise
from pathlib import Path
import re
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


DEMO = (Path(__file__).resolve().parents[2] / "examples" / "advanced"
        / "CortexM7" / "CortexM7_ThumbDemo.ckt")


class CortexM7DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = read_model_source(DEMO)

    def trace_to(self, sim, finish):
        trace = []
        for _ in range(96):
            values = sim.step()
            trace.append(values)
            if finish(values):
                return trace
        self.fail(f"No expected state after {len(trace)} cycles, "
                  f"PC={sim.values['PC']:#x}, fault={sim.values['FAULT_CODE']}")

    def check_boot(self, sim):
        for address in (0, 4):
            self.assertEqual(sim.values["BUS_READ"], 1)
            self.assertEqual(sim.values["BUS_ADDR"], address)
            self.assertEqual(sim.values["BUS_WRITE"], 0)
            sim.step()
        self.assertEqual(sim.values["MSP"], 0x20000040)
        self.assertEqual(sim.values["PC"], 8)
        self.assertEqual(sim.values["BOOTED"], 1)
        self.assertEqual(sim.values["FAULT"], 0)

    def test_typed_ports_and_vector_boot(self):
        self.assertTrue(export_verilog(self.source).startswith("module CORTEX_M7_THUMB_DEMO ("))
        sim = DigitalSimulation(self.source)
        self.assertEqual(sim.clocks, ("CLK",))
        self.assertEqual(sim.inputs, ("RESET",))
        self.assertEqual(sim.signals["IR"], 16)
        self.assertEqual(sim.signals["LAST_IR"], 16)
        self.assertEqual(sim.signals["NZCV"], 4)
        for name in ("PC", "MSP", "LR", "LAST_PC", "BUS_ADDR", "BUS_RDATA", "BUS_WDATA", "RAM0", "RAM1",
                     *(f"R{index}" for index in range(13))):
            with self.subTest(port=name):
                self.assertEqual(sim.signals[name], 32)
        self.check_boot(sim)

    def test_thumb_program_retires_and_round_trips_a_ram_word(self):
        sim = DigitalSimulation(self.source)
        self.check_boot(sim)
        booted = sim.values
        trace = self.trace_to(sim, lambda values: values["LAST_PC"] == 0x22)
        final = trace[-1]
        retired = [values for previous, values in pairwise((booted, *trace))
                   if values["LAST_PC"] != previous["LAST_PC"]]
        self.assertEqual([values["LAST_PC"] for values in retired], [
            0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12, 0x0E, 0x10, 0x12,
            0x0E, 0x10, 0x12, 0x14, 0x16, 0x1C, 0x1E, 0x20, 0x22,
        ])
        self.assertEqual([values["LAST_IR"] for values in retired[:2]], [0x4906, 0x2005])
        self.assertEqual((final["PC"], final["R0"], final["R1"], final["R2"], final["R3"]),
                         (0x22, 11, 0x20000000, 0, 11))
        self.assertEqual((final["RAM0"], final["RAM1"], final["NZCV"]), (11, 0, 0b0110))
        self.assertFalse(any(values["FAULT"] for values in trace))
        stores = [(values["BUS_ADDR"], values["BUS_WDATA"]) for values in trace if values["BUS_WRITE"]]
        self.assertEqual(stores, [(0x20000000, 11)])
        self.assertTrue(any(values["BUS_READ"] and values["BUS_ADDR"] == 0x20000000
                            and values["BUS_RDATA"] == 11 for values in trace))
        for _ in range(2):
            stopped = sim.step()
            self.assertEqual((stopped["PC"], stopped["LAST_IR"]), (0x22, 0xE7FE))

    def test_external_reset_restarts_the_vector_and_firmware(self):
        sim = DigitalSimulation(self.source)
        self.trace_to(sim, lambda values: values["LAST_PC"] == 0x22)
        sim.set_input("RESET", 1)
        sim.step()
        self.assertEqual(sim.values["BOOTED"], 0)
        self.assertEqual(sim.values["FAULT"], 0)
        sim.set_input("RESET", 0)
        self.check_boot(sim)
        final = self.trace_to(sim, lambda values: values["LAST_PC"] == 0x22)[-1]
        self.assertEqual((final["R0"], final["R3"], final["RAM0"]), (11, 11, 11))

    def test_arm_rom_matches_the_original_firmware_words(self):
        match = re.search(r"chip M7DemoFlashWord\(.*?\n}", self.source, re.S)
        self.assertIsNotNone(match)
        rom = DigitalSimulation(match[0])
        for address, expected in enumerate((
            536870976, 9, 537217286, 805446147, 3522968065, 3489736715,
            3204456703, 1745575944, 3892231936, 536870912,
        )):
            rom.set_input("address", address * 4)
            self.assertEqual((rom.values["word"], rom.values["valid"]), (expected, 1))
        for address in (1, 38, 40, 0xFFFFFFFF):
            rom.set_input("address", address)
            self.assertEqual((rom.values["word"], rom.values["valid"]), (0, 0))

    def test_arm_rom_packs_a_wide_fp_instruction_across_words(self):
        source = (
            "chip ROM(address, word) { address[32]=input; word[32]=output; "
            "arm(address, word) { MOVS r0, #0; VSQRT.F32 s0, s31; } } # legacy comment"
        )
        sim = DigitalSimulation(source)
        self.assertEqual(sim.values["word"], 0xEEB12000)
        sim.set_input("address", 4)
        self.assertEqual(sim.values["word"], 0x0AEF)

    def test_arm_rom_reports_original_instruction_line(self):
        with self.assertRaises(CircuitError) as error:
            export_verilog("chip ROM(address, word) {\n"
                           "address[32]=input; word[32]=output;\n"
                           "arm(address, word) {\nB missing_label;\n}\n}")
        self.assertEqual(error.exception.line, 4)
        self.assertIn("missing_label", error.exception.message)

    def test_arm_rom_keeps_register_lists_and_following_immediates(self):
        source = (
            "chip ROM(address, word) { address[32]=input; word[32]=output; "
            "arm(address, word) { PUSH {r4, lr}; POP {r4, pc}; MOVS r0, #5; } }"
        )
        sim = DigitalSimulation(source)
        self.assertEqual(sim.values["word"], 0xBD10B510)
        sim.set_input("address", 4)
        self.assertEqual(sim.values["word"], 0x2005)

    def test_even_reset_vector_faults_instead_of_fetching(self):
        sim = DigitalSimulation(self.source.replace(".word reset + 1;", ".word reset;"))
        trace = self.trace_to(sim, lambda values: values["FAULT"])
        self.assertEqual(trace[-1]["FAULT_CODE"], 1)
        self.assertEqual(trace[-1]["BOOTED"], 0)
        self.assertFalse(any(values["BUS_WRITE"] for values in trace))

    def test_unsupported_thumb_opcode_faults_at_the_fetch_address(self):
        source = self.source.replace("LDR r1, ram_base;", ".hword 0xDE00;")
        sim = DigitalSimulation(source)
        self.check_boot(sim)
        trace = self.trace_to(sim, lambda values: values["FAULT"])
        final = trace[-1]
        self.assertEqual((final["FAULT_CODE"], final["PC"], final["IR"]), (3, 8, 0xDE00))
        self.assertFalse(any(values["BUS_WRITE"] for values in trace))
        self.assertEqual(sim.step()["PC"], 8)

    def test_misaligned_store_faults_without_modifying_ram(self):
        source = self.source.replace(".word 0x20000000;", ".word 0x20000001;")
        sim = DigitalSimulation(source)
        final = self.trace_to(sim, lambda values: values["FAULT"])[-1]
        self.assertEqual((final["FAULT_CODE"], final["PC"], final["R1"]), (4, 0x1C, 0x20000001))
        self.assertEqual((final["RAM0"], final["RAM1"]), (0, 0))
