"""Verify representative Thumb encodings and sparse ROM output."""

import unittest

from circuitlab.language.thumb_assembler import assemble_thumb
from circuitlab.model.graph import CircuitError


class ThumbAssemblerTests(unittest.TestCase):
    def assemble(self, *lines):
        return assemble_thumb((text, line) for line, text in enumerate(lines, 1))

    def test_division_parallel_arithmetic_saturation_and_barriers(self):
        # Verified against Zig's Cortex-M7 assembler.
        instructions = (
            ("sdiv r0, r1, r2", 0xF0F2FB91),
            ("udiv r3, r4, r5", 0xF3F5FBB4),
            ("qadd r6, r7, r8", 0xF687FA88),
            ("qsub r9, r10, r11", 0xF9AAFA8B),
            ("qdadd r12, lr, r1", 0xFC9EFA81),
            ("qdsub r2, r3, r4", 0xF2B3FA84),
            ("sel r5, r6, r7", 0xF587FAA6),
            ("sadd16 r8, r9, r10", 0xF80AFA99),
            ("uadd8 r11, r12, lr", 0xFB4EFA8C),
            ("dsb sy", 0x8F4FF3BF),
            ("dmb", 0x8F5FF3BF),
            ("isb sy", 0x8F6FF3BF),
        )
        for instruction, word in instructions:
            with self.subTest(instruction=instruction):
                self.assertEqual(self.assemble(instruction), {0: word})

    def test_move_immediate_preserves_flags_unless_requested(self):
        self.assertEqual(
            self.assemble("mov r0, #3", "mov r1, #0x1234", "movs r2, #3"),
            {0: 0x0003F04F, 4: 0x2134F241, 8: 0x00002203},
        )

    def test_narrow_arithmetic_preserves_the_written_operand_form(self):
        self.assertEqual(self.assemble("adds r0, #3", "adds r0, r0, #3"), {0: 0x1CC03003})

    def test_sparse_origin_and_final_zero_word(self):
        self.assertEqual(self.assemble(
            ".byte 0x55",
            ".org 0x80000000",
            ".word 0x11223344",
            ".org 0xFFFFFFFC",
            ".word 0",
        ), {0: 0x55, 0x80000000: 0x11223344, 0xFFFFFFFC: 0})

    def test_data_beyond_the_last_arm_address_reports_its_line(self):
        with self.assertRaises(CircuitError) as error:
            self.assemble(".org 0xFFFFFFFF", ".word 0")
        self.assertEqual(error.exception.line, 2)
