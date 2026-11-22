"""Exercise CPU-qualified accesses to the STM32H750 serial test peers."""

import unittest
from pathlib import Path
import runpy

from circuitlab.math.digital import DigitalSimulation


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "examples/advanced/CortexM7/firmware/stm32h750/STM32H750_Serial.ckt"
SPI = 0x40013000
I2C1 = 0x40005400
I2C2 = 0x40005800
START = 1 << 13
AUTOEND = 1 << 25
RELOAD = 1 << 24
READ = 1 << 10


class STM32H750SerialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = DigitalSimulation(MODEL.read_text(encoding="utf-8") + """
main SERIAL_TEST {
    CLK, RESET, READ, WRITE = input;
    ADDRESS[32], SIZE[2], WDATA[32], CLOCKS[3], RESETS[3] = input;
    DATA[32], MAPPED, IRQS[240], TRACE[384] = output;
    STM32H750Serial(CLK, RESET, READ, WRITE, ADDRESS, SIZE, WDATA, CLOCKS, RESETS,
                    DATA, MAPPED, IRQS, TRACE);
}
""")

    def setUp(self):
        for name, value in (("READ", 0), ("WRITE", 0), ("RESETS", 0), ("CLOCKS", 7), ("RESET", 1)):
            self.sim.set_input(name, value)
        self.sim.step()
        self.sim.set_input("RESET", 0)

    def select(self, address, size=2):
        self.sim.set_input("ADDRESS", address)
        self.sim.set_input("SIZE", size)

    def peek(self, address, size=2):
        self.select(address, size)
        self.assertEqual(self.sim.values["MAPPED"], 1)
        return self.sim.values["DATA"]

    def read(self, address, size=2):
        value = self.peek(address, size)
        self.sim.set_input("READ", 1)
        self.sim.step()
        self.sim.set_input("READ", 0)
        return value

    def write(self, address, value, size=2):
        self.select(address, size)
        self.assertEqual(self.sim.values["MAPPED"], 1)
        self.sim.set_input("WDATA", value)
        self.sim.set_input("WRITE", 1)
        self.sim.step()
        self.sim.set_input("WRITE", 0)

    def tick(self, count=1):
        for _ in range(count):
            self.sim.step()

    def spi_start(self, count, bits=8, interrupts=0, comm=0):
        self.write(SPI + 8, bits - 1)
        self.write(SPI + 12, (1 << 22) | (comm << 17))
        self.write(SPI + 16, interrupts)
        self.write(SPI + 4, count)
        self.write(SPI, 1 | (1 << 9))

    def i2c_start(self, base, count, *, address=0x50, reading=False, autoend=True, reload=False):
        self.write(base + 4, (address << 1) | START | (READ if reading else 0) |
                   (count << 16) | (AUTOEND if autoend else 0) | (RELOAD if reload else 0))
        self.tick()

    def i2c_send(self, base, values):
        for value in values:
            self.assertTrue(self.peek(base + 24) & 2)
            self.write(base + 40, value, size=0)
            self.tick()

    def i2c_receive(self, base, count):
        result = []
        for _ in range(count):
            self.tick()
            self.assertTrue(self.peek(base + 24) & 4)
            result.append(self.read(base + 36, size=0))
        return result

    def test_spi_packed_loopback_and_qualified_reads(self):
        self.spi_start(4)
        self.assertTrue(self.peek(SPI + 20) & 2)
        self.write(SPI + 32, 0x44332211)
        self.tick(4)
        self.assertEqual(self.peek(SPI + 20) & 0x801B, 0x8019)
        self.assertEqual(self.peek(SPI + 48), 0x44332211)
        self.tick(3)  # Viewer peeks and unrelated CPU accesses must not consume RXDR.
        self.assertEqual(self.read(SPI + 48, size=0), 0x11)
        self.assertEqual(self.read(SPI + 48, size=0), 0x22)
        self.assertEqual(self.read(SPI + 48, size=1), 0x4433)
        self.assertEqual(self.peek(SPI + 20) & 1, 0)
        trace = self.sim.values["TRACE"] & ((1 << 128) - 1)
        self.assertEqual(trace & 0xFFFFFFFF, 0x44332211)
        self.assertEqual((trace >> 64) & 0xFFFFFFFF, 4)
        self.write(SPI + 24, 24)
        self.assertEqual(self.peek(SPI + 20) & 24, 0)

    def test_spi_frame_widths_irq_overrun_and_clock_gate(self):
        self.spi_start(2, bits=16, interrupts=1 | 8)
        self.write(SPI + 32, 0xABCD1234)
        self.tick(2)
        self.assertTrue(self.sim.values["IRQS"] & (1 << 35))
        self.assertEqual(self.read(SPI + 48, size=1), 0x1234)
        self.assertEqual(self.read(SPI + 48, size=1), 0xABCD)
        self.write(SPI + 24, 24)
        self.assertEqual(self.sim.values["IRQS"], 0)
        self.write(SPI, 0)
        self.spi_start(5, bits=32, interrupts=64)
        for value in range(5):
            self.write(SPI + 32, 0xDEAD0000 + value)
            self.tick()
        self.assertTrue(self.peek(SPI + 20) & 64)
        self.assertTrue(self.sim.values["IRQS"] & (1 << 35))
        self.assertEqual(self.read(SPI + 48), 0xDEAD0000)
        self.sim.set_input("CLOCKS", 6)
        self.assertEqual(self.peek(SPI + 20), 0)
        self.assertEqual(self.sim.values["IRQS"], 0)
        self.write(SPI + 24, 64)
        self.sim.set_input("CLOCKS", 7)
        self.assertTrue(self.peek(SPI + 20) & 64)
        self.write(SPI + 24, 64)
        self.assertEqual(self.peek(SPI + 20) & 64, 0)

    def test_spi_tx_fifo_can_be_preloaded_before_master_start(self):
        self.write(SPI + 4, 4)
        self.write(SPI, 1)
        self.assertTrue(self.peek(SPI + 20) & 2)
        self.write(SPI + 32, 0x88776655)
        self.tick(3)
        self.assertEqual(self.peek(SPI + 20) & 9, 0)
        self.assertEqual((self.sim.values["TRACE"] >> 64) & 0xFFFFFFFF, 0)
        self.write(SPI, 1 | (1 << 9))
        self.tick(4)
        self.assertTrue(self.peek(SPI + 20) & 8)
        self.assertEqual(self.read(SPI + 48), 0x88776655)

    def test_i2c_mem_write_repeated_start_read_and_independent_buses(self):
        for base in (I2C1, I2C2):
            self.write(base, 1)
        self.i2c_start(I2C1, 4)
        self.i2c_send(I2C1, [0x20, 0x91, 0x82, 0x73])
        self.assertEqual(self.peek(I2C1 + 24) & 0x8020, 0x20)
        self.write(I2C1 + 28, 32)
        self.i2c_start(I2C1, 1, autoend=False)
        self.i2c_send(I2C1, [0x20])
        self.assertEqual(self.peek(I2C1 + 24) & 0x8040, 0x8040)
        self.i2c_start(I2C1, 3, reading=True)
        self.assertEqual(self.i2c_receive(I2C1, 3), [0x91, 0x82, 0x73])
        self.assertTrue(self.peek(I2C1 + 24) & 32)
        self.i2c_start(I2C2, 1, reading=True)
        self.assertEqual(self.i2c_receive(I2C2, 1), [0x42])

    def test_i2c_preloaded_tx_reload_and_pointer_wrap(self):
        self.write(I2C1, 1)
        self.write(I2C1 + 40, 0xFF, size=0)  # HAL can preload before START.
        self.i2c_start(I2C1, 1, autoend=False, reload=True)
        self.tick()
        self.assertTrue(self.peek(I2C1 + 24) & 128)
        self.write(I2C1 + 4, (0x50 << 1) | (2 << 16) | AUTOEND)
        self.i2c_send(I2C1, [0x11, 0x22])
        self.write(I2C1 + 28, 32)
        self.i2c_start(I2C1, 1, autoend=False)
        self.i2c_send(I2C1, [0xFF])
        self.i2c_start(I2C1, 2, reading=True)
        self.assertEqual(self.i2c_receive(I2C1, 2), [0x11, 0x22])

    def test_i2c_nack_w1c_and_recovery(self):
        self.write(I2C1, 1 | (1 << 4) | (1 << 5))
        self.i2c_start(I2C1, 1, address=0x51)
        self.assertEqual(self.peek(I2C1 + 24) & 0x8030, 0x30)
        self.assertTrue(self.sim.values["IRQS"] & (1 << 31))
        self.write(I2C1 + 28, 16)
        self.assertTrue(self.sim.values["IRQS"] & (1 << 31))
        self.write(I2C1 + 28, 32)
        self.assertEqual(self.sim.values["IRQS"], 0)
        self.i2c_start(I2C1, 1, reading=True)
        self.assertEqual(self.i2c_receive(I2C1, 1), [0x42])

    def test_i2c_rx_read_gate_and_rcc_reset_preserve_external_device(self):
        self.write(I2C1, 1 | (1 << 2))
        self.i2c_start(I2C1, 2)
        self.i2c_send(I2C1, [0x40, 0xAC])
        self.sim.set_input("RESETS", 2)
        self.tick()
        self.sim.set_input("RESETS", 0)
        self.assertEqual(self.peek(I2C1), 0)
        self.write(I2C1, 1 | (1 << 2))
        self.i2c_start(I2C1, 1, autoend=False)
        self.i2c_send(I2C1, [0x40])
        self.i2c_start(I2C1, 1, reading=True)
        self.tick()
        self.assertTrue(self.sim.values["IRQS"] & (1 << 31))
        self.assertEqual(self.peek(I2C1 + 36, size=0), 0xAC)
        self.tick(5)
        self.assertEqual(self.peek(I2C1 + 36, size=0), 0xAC)
        self.assertTrue(self.peek(I2C1 + 24) & 4)
        self.assertEqual(self.read(I2C1 + 36, size=0), 0xAC)
        self.assertEqual(self.peek(I2C1 + 24) & 4, 0)


class STM32H750HALSerialTests(unittest.TestCase):
    def test_stock_hal_polling_and_irq_transfers_boot_from_st_startup(self):
        builder = runpy.run_path(str(MODEL.parent / "build_firmware.py"))
        image = builder["load_elf"](ROOT / "tests/fixtures/digital/stm32h750_hal_serial.elf")
        image.probes.update(SERIAL_STAGE=image.symbols["serial_stage"],
                            SERIAL_IRQ_COMPLETIONS=image.symbols["serial_irq_completions"])
        source = builder["TARGET"]["render_demo"](
            image.image, image.ram_regions, image.stack_top, image.main, image.probes,
        )
        sim = DigitalSimulation(source)
        exceptions, stages = set(), set()
        saw_nack = False
        for _ in range(16000):
            state = sim.values
            stages.add(state["SERIAL_STAGE"])
            exceptions.add(state["IPSR"])
            saw_nack |= bool(state["I2C1_STATUS"] & 16)
            self.assertEqual(state["FAULT"], 0,
                             f"PC={state['PC']:#x}, IR={state['IR']:#x}, CFSR={state['CFSR']:#x}")
            if state["BOOT_SIGNATURE"]:
                break
            sim.step()
        self.assertEqual(state["BOOT_SIGNATURE"], 0x5352494C,
                         f"stage={state['SERIAL_STAGE']}, PC={state['PC']:#x}, "
                         f"SPI={state['SPI1_STATUS']:#x}, I2C={state['I2C1_STATUS']:#x}")
        self.assertEqual(stages, set(range(8)))
        self.assertTrue({15, 47, 49, 51}.issubset(exceptions), exceptions)
        self.assertTrue(saw_nack)
        self.assertEqual(state["SERIAL_IRQ_COMPLETIONS"], 4)
        self.assertEqual((state["DATA_VALUE"], state["BSS_VALUE"]), (0x438, 0x5599))
        self.assertEqual((state["CFSR"], state["HFSR"]), (0, 0))
        self.assertEqual(state["SPI1_TRANSFERS"], 16)
        self.assertGreater(state["I2C1_TRANSFERS"], 10)
        self.assertEqual(state["I2C2_TRANSFERS"], 1)


if __name__ == "__main__":
    unittest.main()
