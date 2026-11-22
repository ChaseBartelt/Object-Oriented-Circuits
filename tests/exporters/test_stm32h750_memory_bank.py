"""Check generated SRAM banks at their bus and viewer probe boundaries."""

from pathlib import Path
from runpy import run_path
import unittest

from circuitlab.exporters.verilog import compile_digital_design
from circuitlab.math.digital import DigitalSimulation

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "examples/advanced/CortexM7/firmware/stm32h750"
RENDER = run_path(str(TARGET / "render_target.py"))
BASE = 0x24000000
WINDOWS = tuple((BASE + i * 1024, BASE + (i + 1) * 1024) for i in range(11)) + (
    (BASE + 0x2C00, BASE + 0x2C40), (BASE + 0x2C40, BASE + 0x3000),
)
PROBES = {"TAIL": BASE + 0x2C44, "HEAD": BASE + 8, "MIDDLE": BASE + 5 * 1024 + 40}


def bank_simulation(windows, probes, audio):
    storage = ROOT / "examples/advanced/CortexM7/core/memory/storage.ckt"
    source = (storage.read_text(encoding="utf-8") + "\n"
              + (TARGET / "STM32H750ByteRAM.ckt").read_text(encoding="utf-8") + "\n"
              + RENDER["_memory_bank"](windows, probes, audio) + """
main MEMORY_BANK_TEST {
    CLK, RESET, WRITE, READY = input;
    ADDRESS[32], SIZE[2], WDATA[32] = input;
    DATA[32], MAPPED, VALID, FIRST[352] = output;
    M7FirmwareMemoryBank(CLK, RESET, WRITE, ADDRESS, SIZE, WDATA, READY,
                         DATA, MAPPED, VALID, FIRST);
}
""")
    design = compile_digital_design(source)
    return DigitalSimulation(source, design=design)


class STM32H750MemoryBankTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.simulations = {audio: bank_simulation(WINDOWS, PROBES, audio) for audio in (False, True)}

    def reset(self, sim):
        sim.reset()
        sim.set_input("READY", 1)
        sim.set_input("SIZE", 2)

    def read(self, sim, address, size=2):
        sim.set_input("ADDRESS", address)
        sim.set_input("SIZE", size)
        return sim.values["DATA"], sim.values["MAPPED"]

    def write(self, sim, address, value, size=2):
        sim.set_input("WDATA", value)
        self.read(sim, address, size)
        sim.set_input("WRITE", 1)
        sim.step()
        sim.set_input("WRITE", 0)

    def test_actual_non_audio_targets_compile_with_the_memory_bank(self):
        loader = run_path(str(TARGET / "build_firmware.py"))
        for fixture in ("stm32h750_stock_boot.elf", "stm32h750_hal_blink.elf"):
            with self.subTest(fixture=fixture):
                image = loader["load_elf"](ROOT / "tests/fixtures/digital" / fixture)
                source = RENDER["render_demo"](image.image, image.ram_regions, image.stack_top,
                                               image.main, image.probes)
                design = compile_digital_design(source)
                self.assertTrue(any(part.name == "M7FirmwareMemoryBank" for part in design.instances))

    def test_every_generated_window_has_independent_storage_and_an_inspectable_instance(self):
        for audio, sim in self.simulations.items():
            with self.subTest(audio=audio):
                self.reset(sim)
                instances = [item for item in sim.instances if item.name == "M7FirmwareMemoryWindow"]
                self.assertEqual(len(instances), 13)
                self.assertEqual(len({item.pins for item in instances}), 13)
                for index, (start, _) in enumerate(WINDOWS):
                    self.write(sim, start, 0x12340000 + index)
                for index, (start, _) in enumerate(WINDOWS):
                    self.assertEqual(self.read(sim, start), (0x12340000 + index, 1))
                self.assertEqual(self.read(sim, BASE + 0x4000), (0, 0))

    def test_probe_words_remain_in_requested_order_and_unused_slots_are_zero(self):
        values = (0xA1B2C3D4, 0x11223344, 0x55667788)
        for audio, sim in self.simulations.items():
            with self.subTest(audio=audio):
                self.reset(sim)
                for address, value in zip(PROBES.values(), values):
                    self.write(sim, address, value)
                self.assertEqual(sim.values["FIRST"], sum(value << (32 * i) for i, value in enumerate(values)))
                self.write(sim, PROBES["HEAD"] + 1, 0xAB, 0)
                self.assertEqual((sim.values["FIRST"] >> 32) & 0xFFFFFFFF, 0x1122AB44)

    def test_unaligned_reads_and_writes_combine_both_sides_of_a_split_window(self):
        sim = self.simulations[True]
        self.reset(sim)
        for boundary in (BASE + 1024, BASE + 0x2C40):
            with self.subTest(boundary=boundary):
                self.write(sim, boundary - 4, 0x11223344)
                self.write(sim, boundary, 0x55667788)
                self.assertEqual(self.read(sim, boundary - 1), (0x66778811, 1))
                self.write(sim, boundary - 1, 0xA1B2C3D4)
                self.assertEqual(self.read(sim, boundary - 4), (0xD4223344, 1))
                self.assertEqual(self.read(sim, boundary), (0x55A1B2C3, 1))

    def test_invalid_endpoint_prevents_partial_writes_without_changing_fault_decode(self):
        windows = ((BASE, BASE + 1024), (BASE + 2048, BASE + 3072))
        sim = bank_simulation(windows, {"FIRST": BASE}, True)
        self.reset(sim)
        self.write(sim, BASE + 1020, 0x11223344)
        self.assertEqual(self.read(sim, BASE + 1023), (0x11, 0))
        self.assertEqual(sim.values["VALID"], 0)
        self.write(sim, BASE + 1023, 0xFFFFFFFF)
        self.assertEqual(self.read(sim, BASE + 1020), (0x11223344, 1))
        self.assertEqual(self.read(sim, BASE + 1536), (0, 0))
        self.write(sim, BASE, 0xAABBCCDD)
        self.assertEqual(self.read(sim, BASE - 1), (0xBBCCDD00, 0))
        self.write(sim, BASE - 1, 0)
        self.assertEqual(self.read(sim, BASE), (0xAABBCCDD, 1))

    def test_external_memory_keeps_its_ready_gate_and_retains_stored_words(self):
        base = 0xC0000000
        sim = bank_simulation(((base, base + 1024),), {"FIRST": base}, True)
        self.reset(sim)
        sim.set_input("READY", 0)
        self.write(sim, base, 0xDEADBEEF)
        self.assertEqual(self.read(sim, base), (0, 0))
        self.assertEqual(sim.values["VALID"], 1)
        sim.set_input("READY", 1)
        self.assertEqual(self.read(sim, base), (0, 1))
        self.write(sim, base, 0x11223344)
        sim.set_input("READY", 0)
        self.assertEqual(self.read(sim, base), (0, 0))
        self.assertEqual(sim.values["FIRST"], 0x11223344)
        sim.set_input("READY", 1)
        self.assertEqual(self.read(sim, base), (0x11223344, 1))


if __name__ == "__main__":
    unittest.main()
