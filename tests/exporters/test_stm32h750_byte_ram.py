"""Exercise the STM32H750 byte lanes through their memory bus interface."""

import unittest
from pathlib import Path

from circuitlab.math.digital import DigitalSimulation


SOURCE = (Path(__file__).resolve().parents[2] /
          "examples/advanced/CortexM7/firmware/stm32h750/STM32H750ByteRAM.ckt")


class STM32H750ByteRAMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = DigitalSimulation(SOURCE.read_text(encoding="utf-8") + """
main BYTE_RAM_TEST {
    CLK, RESET, WRITE = input;
    ADDRESS[11], LENGTH[11], SIZE[2], WDATA[32] = input;
    DATA[32], FIRST[352] = output;
    STM32H750ByteRAM(CLK, RESET, WRITE, ADDRESS, LENGTH, SIZE, WDATA, DATA, FIRST);
}
""")

    def setUp(self):
        self.sim.set_input("LENGTH", 1024)
        self.sim.set_input("RESET", 1)
        self.sim.step()
        self.sim.set_input("RESET", 0)

    def read(self, address, size=2):
        self.sim.set_input("ADDRESS", address % 2048)
        self.sim.set_input("SIZE", size)
        return self.sim.values["DATA"]

    def write(self, address, value, size=2):
        self.sim.set_input("WDATA", value)
        self.read(address, size)
        self.sim.set_input("WRITE", 1)
        self.sim.step()
        self.sim.set_input("WRITE", 0)

    def test_word_crosses_packed_lines(self):
        self.write(60, 0x11223344)
        self.write(64, 0x55667788)
        self.assertEqual(self.read(63), 0x66778811)
        self.write(63, 0xA1B2C3D4)
        self.assertEqual(self.read(60), 0xD4223344)
        self.assertEqual(self.read(64), 0x55A1B2C3)
        self.assertEqual(self.read(63), 0xA1B2C3D4)

    def test_halfword_crosses_words_and_keeps_probes_in_address_order(self):
        self.write(0, 0x11223344)
        self.write(4, 0x55667788)
        self.write(3, 0xABCD, size=1)
        self.assertEqual(self.read(3, size=1), 0xABCD)
        self.assertEqual(self.read(3, size=0), 0xCD)
        self.assertEqual(self.read(0), 0xCD223344)
        self.assertEqual(self.read(4), 0x556677AB)
        self.assertEqual(self.sim.values["FIRST"], 0x556677ABCD223344)

    def test_split_page_keeps_its_bytes_at_original_transfer_offsets(self):
        self.sim.set_input("LENGTH", 64)
        self.write(0, 0x11223344)
        self.write(62, 0xAABBCCDD)
        self.assertEqual(self.read(62), 0xCCDD)
        self.assertEqual(self.read(0), 0x11223344)
        self.write(-2, 0xAABBCCDD)
        self.assertEqual(self.read(-2), 0xAABB0000)
        self.assertEqual(self.read(0), 0x1122AABB)


if __name__ == "__main__":
    unittest.main()
