"""Exercise MPU region priority and fault recovery through the Cortex-M7 model."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import (
    HARDFAULT, MSP_TOP, RAM_BASE, firmware_simulation, thumb_image,
)


DATA_GUARD = RAM_BASE + 0x100
CODE_GUARD = 0x100
HARDFAULT_PC = 0x94
RESUME_PC = 0x8A


def mpu_image(execute_never):
    """Overlay a 32-byte guard on general flash and SRAM regions."""
    if execute_never:
        guard = CODE_GUARD
        attributes = 0x16000009
        seed = 0xBF00
        fault = (0x3301, 0x4718)  # Add the Thumb bit and branch to the execute-never region.
        read_back = (0xBF00, 0xBF00)
    else:
        guard = DATA_GUARD
        attributes = 0x12000009
        seed = 0x6019
        fault = (0x2199, 0x6019)  # Try to overwrite the guard as unprivileged Thread.
        read_back = (0x6819, 0x60E1)

    code = pack("<50H",
                0x481C, 0x6801, 0x4C1C, 0x6161,          # RAM5 = MPU_TYPE.
                0x2100, 0x6081, 0x60C1, 0x4A1B, 0x6102,  # Region 0 is executable read-only flash.
                0x2101, 0x6081, 0x60C4, 0x4A19, 0x6102,  # Region 1 is read/write SRAM.
                0x2102, 0x6081, 0x4B18, 0x60C3, 0x4A18, 0x6102,  # Region 2 has higher priority.
                0x2105, 0x6041,                          # Enable MPU and privileged background map.
                0xF3BF, 0x8F4F, 0xF3BF, 0x8F6F,          # DSB and ISB.
                0x2111, seed,                           # Seed the data guard as privileged Thread.
                0x2101, 0xF381, 0x8814, 0xF3BF, 0x8F6F,  # CONTROL.nPRIV = 1 and ISB.
                *fault, *read_back,
                0x2155, 0x6021, 0xE7FE,                 # RAM0 = 0x55 after fault recovery.
                0x4A0E, 0x6811, 0x6061,                 # HardFault writes CFSR to RAM1.
                0x4A0E, 0x6811, 0x60A1,                 # HardFault writes MMFAR to RAM2.
                0x2100 | RESUME_PC, 0x9106, 0x4770, 0xBF00)
    constants = pack("<8I", 0xE000ED90, RAM_BASE, 0x06000017, 0x13000017,
                     guard, attributes, 0xE000ED28, 0xE000ED34)
    image = thumb_image(code.ljust(0xB8 - 0x44, b"\0") + constants,
                        unused=0xA5, hardfault=HARDFAULT_PC | 1)
    if execute_never:
        # Any retired code at the prohibited target would write to RAM4.
        image = image.ljust(CODE_GUARD, b"\0") + pack("<3H", 0x22FF, 0x6122, 0xE7FE)
    return image


class CortexM7MpuTest(unittest.TestCase):
    def test_overlapping_region_permissions_and_hardfault_recovery(self):
        for execute_never in (False, True):
            with self.subTest(execute_never=execute_never):
                self.run_guard_case(execute_never)

    def run_guard_case(self, execute_never):
        sim = firmware_simulation(mpu_image(execute_never))
        fault_pc = CODE_GUARD if execute_never else 0x88
        denied = entered = returned = False
        for _ in range(200):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0,
                             f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")
            if before["PC"] == fault_pc and not before["PRIVILEGED"]:
                if execute_never:
                    denied |= not before["FETCH_ALLOWED"]
                elif before["BUS_ADDR"] == DATA_GUARD:
                    denied |= not before["DATA_ALLOWED"]
            if not before["IPSR"] and after["IPSR"] == HARDFAULT:
                entered = True
                self.assertEqual((before["BUS_READ"], before["BUS_ADDR"]), (1, 12))
                self.assertEqual((after["PC"], after["MSP"], after["PRIVILEGED"]),
                                 (HARDFAULT_PC, MSP_TOP - 32, 1))
            if before["IPSR"] == HARDFAULT and not after["IPSR"]:
                returned = True
                self.assertEqual((after["PC"], after["MSP"], after["PRIVILEGED"]),
                                 (RESUME_PC, MSP_TOP, 0))
            if after["RAM0"] == 0x55:
                break
        else:
            self.fail(f"Thread did not resume after the MPU fault. Last PC {after['PC']:#x}.")

        self.assertTrue(denied)
        self.assertTrue(entered and returned)
        self.assertEqual(after["RAM5"] & 0xFF00, 8 << 8)
        self.assertEqual((after["HFSR"], after["RAM4"]), (1 << 30, 0))
        status, address, retained = (1, 0, 0) if execute_never else (0x82, DATA_GUARD, 0x11)
        self.assertEqual((after["CFSR"], after["MMFAR"]), (status, address))
        self.assertEqual((after["RAM1"], after["RAM2"], after["RAM3"]), (status, address, retained))
