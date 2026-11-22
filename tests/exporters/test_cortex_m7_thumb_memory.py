"""Exercise assembled Thumb-2 memory and branch instructions in the OOC core."""

from collections import defaultdict
from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import (
    RAM_BASE, firmware_simulation, thumb_image,
)


def step_to_idle(test, image):
    sim = firmware_simulation(image)
    transfers = defaultdict(list)
    for _ in range(100):
        before = sim.values
        after = sim.step()
        test.assertEqual(after["FAULT_CODE"], 0,
                         f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")
        if before["BOOTED"] and (before["BUS_READ"] or before["BUS_WRITE"]):
            data = before["BUS_WDATA"] if before["BUS_WRITE"] else before["BUS_RDATA"]
            transfers[before["PC"]].append((before["BUS_ADDR"], data))
        if after["LAST_IR"] == 0xE7FE and not after["IPSR"]:
            return after, transfers
    test.fail(f"Firmware did not reach its idle loop. Last PC {after['PC']:#x}.")


class CortexM7ThumbMemoryTest(unittest.TestCase):
    def test_indexed_dual_multiple_and_table_addressing(self):
        words = [
            0x4C17,                    # LDR r4, =0x20000080
            0xF245, 0x6880,           # MOVW r8, #0x5680
            0xF2C1, 0x2834,           # MOVT r8, #0x1234
            0xF64C, 0x59EF,           # MOVW r9, #0xcdef
            0xF6C8, 0x19AB,           # MOVT r9, #0x89ab
            0xE9E4, 0x8902,           # STRD r8, r9, [r4, #8]!
            0xE8F4, 0xAB02,           # LDRD r10, r11, [r4], #8
            0xF914, 0xCC08,           # LDRSB r12, [r4, #-8]
            0xF934, 0x6D08,           # LDRSH r6, [r4, #-8]!
            0x2001,                    # MOVS r0, #1
            0xF854, 0x7020,           # LDR.W r7, [r4, r0, LSL #2]
            0xF204, 0x0508,           # ADDW r5, r4, #8
            0xE935, 0x0006,           # LDMDB r5!, {r1, r2}
            0x2000, 0xB100, 0xDE01,   # MOVS r0, #0; CBZ past UDF
            0xB901, 0xDE02,            # CBNZ r1 past UDF
            0xEBB7, 0x0F0B,           # CMP.W r7, r11
            0xF000, 0x8001, 0xDE03,   # BEQ.W past UDF
            0xE9D4, 0x4300,           # LDRD r4, r3, [r4], retaining the address for the second word
            0xE8DF, 0xF000,           # TBB [pc, r0]
            0x0102, 0xDE04,            # Two table offsets; wrong table entry traps
            0x4803,                    # LDR r0, =0x20000080
            0xF8C0, 0xA3C8,           # STR.W r10, [r0, #0x3c8]
            0xF8D0, 0x03C8,           # LDR.W r0, [r0, #0x3c8]
            0xE000, 0xDE00, 0xE7FE,   # Branch past the unused exception handler and idle
            0x0080, 0x2000,            # SRAM base literal
        ]
        final, transfers = step_to_idle(self, thumb_image(pack("<" + "H" * len(words), *words), unused=0xA1))
        left, right = 0x12345680, 0x89ABCDEF
        word_pair = [(RAM_BASE + 0x88, left), (RAM_BASE + 0x8C, right)]
        for address in (0x56, 0x5A, 0x70, 0x88):
            self.assertEqual(transfers[address], word_pair)
        self.assertEqual(transfers[0x8C], [(0x90, 2)])
        self.assertEqual([final[f"R{i}"] for i in (0, 1, 2, 3, 4, 5, 6, 7, 12)],
                         [left, left, right, right, left, RAM_BASE + 0x88, 0x5680, right, 0xFFFFFF80])

    def test_exclusive_stores_fail_after_clear_and_exception(self):
        words = [
            0xF04F, 0x5400,           # MOV.W r4, #0x20000000
            0x250C, 0x60E5,            # Initialize [r4, #12] to 12
            0xE854, 0x5F03,           # LDREX r5, [r4, #12]
            0x3501, 0xE844, 0x5603,   # STREX r6 succeeds and stores 13
            0x3501, 0xE844, 0x5703,   # A second STREX r7 fails
            0xE8D4, 0x5F4F,           # LDREXB r5, [r4]
            0x2580, 0xE8C4, 0x5F48,   # STREXB r8, r5, [r4]
            0xE8D4, 0x5F5F,           # LDREXH r5, [r4]
            0xF205, 0x1580,           # ADDW r5, r5, #0x180
            0xE8C4, 0x5F59,           # STREXH r9, r5, [r4]
            0xE854, 0x5F03, 0x3532,   # Reserve the word again, then give it a different value
            0xF3BF, 0x8F2F,           # CLREX
            0xE844, 0x5A03,           # STREX r10 fails after CLREX
            0xE854, 0x5F03, 0x3532,   # Reserve the word once more
            0xDF00,                    # SVC #0 clears the local reservation
            0xE844, 0x5B03,           # STREX r11 fails after SVC
            0xF3BF, 0x8F5B,           # DMB ISH
            0xE7FE, 0x4770,            # Idle; SVC handler BX LR
        ]
        code = pack("<" + "H" * len(words), *words)
        final, transfers = step_to_idle(self, thumb_image(code, unused=0x93))
        self.assertEqual((final["RAM0"], final["RAM3"]), (0x200, 13))
        self.assertEqual([final[f"R{i}"] for i in range(6, 12)], [0, 1, 0, 0, 1, 1])
        self.assertEqual(transfers[0x52], [(RAM_BASE + 12, 13)])
        for pc in (0x58, 0x7C, 0x88):
            self.assertNotIn(RAM_BASE + 12, [address for address, _ in transfers[pc]])
