"""Check the Daisy Seed board memory and startup registers through their bus ports."""

import unittest
from pathlib import Path

from circuitlab.math.digital import DigitalSimulation


ROOT = Path(__file__).resolve().parents[2] / "examples/advanced/CortexM7/firmware/stm32h750"
RCC = 0x58024400
GPIO_B = 0x58020400
GPIO_D = 0x58020C00
QSPI = 0x52005000
FMC = 0x52004140


class STM32H750BoardMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("STM32H750_StartupRegisters.ckt", "STM32H750BoardMemory.ckt")
        )
        cls.sim = DigitalSimulation(source + """
main BOARD_MEMORY_TEST {
    CLK, RESET, WRITE = input;
    ADDRESS[32], SIZE[2], WDATA[32] = input;
    CLOCK_PLL, GPIO_LED, QSPI_CLOCK, FMC_CLOCK, TIM2_CLOCK, RNG_CLOCK = output;
    QSPI_RESET, FMC_RESET, DMA_CLOCK, SAI_CLOCK, CODEC_RESET_N = output;
    SERIAL_CLOCKS[3], SERIAL_RESETS[3] = output;
    SYSTEM_DATA[32], SYSTEM_MAPPED, MEMORY_DATA[32], MEMORY_MAPPED, QSPI_READY, SDRAM_READY = output;
    STM32H750StartupRegisters(CLK, RESET, ADDRESS, SIZE, WDATA, WRITE, SYSTEM_DATA,
      SYSTEM_MAPPED, GPIO_LED, CLOCK_PLL, QSPI_CLOCK, FMC_CLOCK, TIM2_CLOCK, RNG_CLOCK,
      QSPI_RESET, FMC_RESET, DMA_CLOCK, SAI_CLOCK, CODEC_RESET_N, SERIAL_CLOCKS, SERIAL_RESETS);
    STM32H750BoardMemory(CLK, RESET, ADDRESS, SIZE, WDATA, WRITE, QSPI_CLOCK, FMC_CLOCK,
      TIM2_CLOCK, RNG_CLOCK, QSPI_RESET, FMC_RESET, MEMORY_DATA, MEMORY_MAPPED, QSPI_READY, SDRAM_READY);
}
""")

    def setUp(self):
        self.sim.set_input("RESET", 1)
        self.sim.step()
        self.sim.set_input("RESET", 0)

    def write(self, address, value, size=2):
        self.sim.set_input("ADDRESS", address)
        self.sim.set_input("SIZE", size)
        self.sim.set_input("WDATA", value)
        self.sim.set_input("WRITE", 1)
        self.assertTrue(self.sim.values["SYSTEM_MAPPED"] or self.sim.values["MEMORY_MAPPED"])
        self.sim.step()
        self.sim.set_input("WRITE", 0)

    def test_serial_clock_gates_and_peripheral_resets_are_independent(self):
        self.assertEqual(self.sim.values["SERIAL_CLOCKS"], 0)
        self.assertEqual(self.sim.values["SERIAL_RESETS"], 0)
        self.write(RCC + 0xF0, 1 << 12)
        self.assertEqual(self.sim.values["SERIAL_CLOCKS"], 1)
        self.write(RCC + 0xE8, 1 << 21)
        self.assertEqual(self.sim.values["SERIAL_CLOCKS"], 3)
        self.write(RCC + 0xE8, (1 << 21) | (1 << 22))
        self.assertEqual(self.sim.values["SERIAL_CLOCKS"], 7)
        self.write(RCC + 0x98, 1 << 12)
        self.assertEqual(self.sim.values["SERIAL_RESETS"], 1)
        self.write(RCC + 0x90, 1 << 22)
        self.assertEqual(self.sim.values["SERIAL_RESETS"], 5)
        self.assertEqual(self.read(RCC + 0x90), 1 << 22)
        self.write(RCC + 0x98, 0)
        self.write(RCC + 0x90, 0)
        self.assertEqual(self.sim.values["SERIAL_RESETS"], 0)
        self.assertEqual(self.sim.values["SERIAL_CLOCKS"], 7)

    def read(self, address, size=2):
        self.sim.set_input("ADDRESS", address)
        self.sim.set_input("SIZE", size)
        return self.sim.values["SYSTEM_DATA"] | self.sim.values["MEMORY_DATA"]

    def test_gpio_deinit_can_read_reset_interrupt_routes(self):
        selectors = (0x58000408, 0x5800040C, 0x58000410, 0x58000414)
        for register in selectors:
            self.assertEqual(self.read(register), 0)
            self.assertTrue(self.sim.values["SYSTEM_MAPPED"])
        self.write(selectors[0], 5)
        self.write(RCC + 0xF4, 2)
        self.assertEqual(self.read(selectors[0]), 0)
        for index, register in enumerate(selectors, 1):
            self.write(register, index)
        self.assertEqual(tuple(map(self.read, selectors)), (1, 2, 3, 4))
        for register in (0x58000000, 0x58000004, 0x58000080, 0x58000084):
            self.write(register, 0xFFFF0100)
            self.assertEqual(self.read(register), 1 << 8)
        self.read(0x58000418)
        self.assertFalse(self.sim.values["SYSTEM_MAPPED"])

    def test_qspi_initialization_status_and_erased_window(self):
        self.write(QSPI, 1)
        self.assertEqual(self.read(QSPI), 0)
        self.write(RCC + 0xD4, 1 << 14)
        self.write(QSPI, 1 | (1 << 22))
        self.write(QSPI + 0x18, 0)
        self.write(QSPI + 0x14, 0x01000101)
        self.write(QSPI + 0x20, 0x40, size=0)
        self.write(QSPI + 0x14, 0x0D0001EB)
        self.assertEqual(self.sim.values["QSPI_READY"], 0)

        self.write(QSPI + 0x14, 0x106)
        self.assertTrue(self.read(QSPI + 0x08) & 2)
        self.write(QSPI + 0x0C, 2)
        self.write(QSPI + 0x24, 2)
        self.write(QSPI + 0x28, 2)
        self.write(QSPI + 0x14, 0x09000105)
        self.sim.step()
        self.assertEqual(self.read(QSPI + 0x08) & 40, 8)
        self.write(QSPI + 0x0C, 8)

        self.write(QSPI + 0x14, 0x01000101)
        self.assertTrue(self.read(QSPI + 0x08) & 4)
        self.write(QSPI + 0x20, 0x40, size=0)
        self.write(QSPI + 0x14, 0x0D0001EB)
        self.assertEqual(self.sim.values["QSPI_READY"], 1)
        self.assertEqual(self.read(0x90000000), 0xFFFFFFFF)
        self.assertEqual(self.sim.values["MEMORY_MAPPED"], 1)
        self.read(0x90800000)
        self.assertEqual(self.sim.values["MEMORY_MAPPED"], 0)

    def test_sdram_requires_ordered_bank_commands_and_refresh(self):
        self.write(RCC + 0xD4, 1 << 12)
        self.write(FMC, 0x1800)
        self.write(FMC + 0x14, 2054 << 1)
        self.write(FMC + 0x10, 0x14)
        self.assertEqual(self.sim.values["SDRAM_READY"], 0)
        for command in (0x11, 0x12, 0x73, 0x14):
            self.write(FMC + 0x10, command)
            self.assertTrue(self.read(FMC + 0x18) & 32)
            self.sim.step()
        self.assertEqual(self.sim.values["SDRAM_READY"], 1)
        self.write(FMC + 0x10, 0x15)
        self.sim.step()
        self.assertEqual(self.read(FMC + 0x18), 2)
        self.assertEqual(self.sim.values["SDRAM_READY"], 0)
        self.write(FMC + 0x10, 0x10)
        self.sim.step()
        self.assertEqual(self.sim.values["SDRAM_READY"], 1)
        self.write(RCC + 0x7C, 1 << 12)
        self.sim.step()
        self.assertEqual(self.sim.values["SDRAM_READY"], 0)

    def test_gpio_revision_straps_and_codec_reset(self):
        self.write(RCC + 0xE0, (1 << 1) | (1 << 3))
        self.write(GPIO_D, 0xFFFFFC3F)
        self.write(GPIO_D + 0x0C, 0x140)
        self.assertEqual(self.read(GPIO_D + 0x10) & 0x18, 0x18)
        self.write(GPIO_D + 0x20, 0xC0000000)
        self.assertEqual(self.read(GPIO_D + 0x20), 0xC0000000)

        self.write(GPIO_B, (0xFFFFFFFF & ~(3 << 22)) | (1 << 22))
        for command, expected in ((1 << 11, 1), (1 << (16 + 11), 0), (1 << 11, 1)):
            self.write(GPIO_B + 0x18, command)
            self.assertEqual(self.sim.values["CODEC_RESET_N"], expected)

    def test_rcc_secondary_pll_ready_and_clock_gates(self):
        self.write(RCC + 0x28, 2 | (1 << 12) | (6 << 20))
        self.write(RCC + 0x00, (1 << 16) | (1 << 26) | (1 << 28))
        self.assertEqual(self.read(RCC + 0x00) & 0x28020000, 0x28020000)
        self.write(RCC + 0xD8, 1)
        self.write(RCC + 0xF0, 1 << 22)
        self.assertEqual(self.sim.values["DMA_CLOCK"], 1)
        self.assertEqual(self.sim.values["SAI_CLOCK"], 1)

        self.write(RCC + 0xE8, 1)
        self.write(0x4000002C, 3)
        self.write(0x40000014, 1)
        self.write(0x40000000, 1)
        self.sim.step()
        self.assertEqual(self.read(0x40000024), 1)
        self.sim.step()
        self.sim.step()
        self.sim.step()
        self.assertEqual(self.read(0x40000024), 0)

