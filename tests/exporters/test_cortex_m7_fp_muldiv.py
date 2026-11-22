"""Verify the assembled VMUL.F32 and VDIV.F32 firmware instruction paths."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import firmware_simulation, thumb_image


ROUND_DOWN = 2 << 22
ROUND_ZERO = 3 << 22
FLUSH = 1 << 24
INVALID = 1
DIV_ZERO = 1 << 1
OVERFLOW = 1 << 2
UNDERFLOW = 1 << 3
INEXACT = 1 << 4
INPUT_DENORMAL = 1 << 7
FLASH_SIZE = 4096
RESET_PC = 0x44
RESULT_PC = 0x74
IDLE_PC = 0x7C

# The input row contains both binary32 operands, FPSCR, and the divide selector.
CASES = (
    ("negative product with a sticky flag", (0x3FC00000, 0xC0000000, DIV_ZERO, 0), (0xC0400000, DIV_ZERO)),
    ("sign of product zero", (0x80000000, 0x3F800000, 0, 0), (0x80000000, 0)),
    ("overflow toward zero", (0x7F7FFFFF, 0x40000000, ROUND_ZERO, 0),
     (0x7F7FFFFF, ROUND_ZERO | OVERFLOW | INEXACT)),
    ("exact subnormal product", (0x00800000, 0x3F000000, 0, 0), (0x00400000, 0)),
    ("tiny inexact product", (0x00000001, 0x3F000000, 0, 0), (0, UNDERFLOW | INEXACT)),
    ("flush subnormal product", (0x00800000, 0x3F000000, FLUSH, 0), (0, FLUSH | UNDERFLOW)),
    ("zero times infinity", (0, 0xFF800000, 0, 0), (0x7FC00000, INVALID)),
    ("signaling NaN payload", (0x7FC12345, 0xFF800001, 0, 0), (0xFFC00001, INVALID)),
    ("recurring quotient to nearest", (0x3F800000, 0x40400000, 0, 1), (0x3EAAAAAB, INEXACT)),
    ("recurring quotient downward", (0x3F800000, 0x40400000, ROUND_DOWN, 1),
     (0x3EAAAAAA, ROUND_DOWN | INEXACT)),
    ("divide by negative zero", (0x3F800000, 0x80000000, 0, 1), (0xFF800000, DIV_ZERO)),
    ("infinity divided by zero", (0x7F800000, 0, 0, 1), (0x7F800000, 0)),
    ("infinity divided by infinity", (0xFF800000, 0x7F800000, 0, 1), (0x7FC00000, INVALID)),
    ("exact subnormal quotient", (0x00800000, 0x40000000, 0, 1), (0x00400000, 0)),
    ("flush denominator to zero", (0x3F800000, 0x00000001, FLUSH, 1),
     (0x7F800000, FLUSH | INPUT_DENORMAL | DIV_ZERO)),
    ("negative zero quotient", (0, 0xBF800000, 0, 1), (0x80000000, 0)),
)


def arithmetic_image():
    # Assembled for Cortex-M7 FPv5-SP-D16. Predication selects exactly one FP op.
    instructions = (
        0x4E0E, 0x4F0F, 0x6037,  # Load CPACR address and enable the FPU.
        0xF3BF, 0x8F4F,          # DSB SY
        0xF3BF, 0x8F6F,          # ISB SY
        0x4E0D,                  # LDR R6, case table address
        0x2700 | len(CASES),     # MOVS R7, case count
        0xCE0F,                  # LDMIA R6!, {R0-R3}
        0xEE00, 0x0A90,          # VMOV S1, R0
        0xEE0F, 0x1A90,          # VMOV S31, R1
        0xEEE1, 0x2A10,          # VMSR FPSCR, R2
        0x2B00, 0xBF0C,          # CMP R3, #0 and ITE EQ
        0xEE20, 0x0AAF,          # VMULEQ.F32 S0, S1, S31
        0xEE80, 0x0AAF,          # VDIVNE.F32 S0, S1, S31
        0xEE10, 0x4A10,          # VMOV R4, S0
        0xEEF1, 0x5A10,          # VMRS R5, FPSCR
        0x3F01, 0xD1EC,          # SUBS R7, #1 and BNE to the LDMIA
        0xE7FE, 0xBF00,          # B . and NOP padding before the literal pool
    )
    cases = b"".join(pack("<4I", *operands) for _, operands, _ in CASES)
    # Place the table at the upper flash boundary to exercise the 4 KiB ROM.
    table = FLASH_SIZE - len(cases)
    literals = pack("<3I", 0xE000ED88, 0x00F00000, table)
    code = pack(f"<{len(instructions)}H", *instructions) + literals
    return thumb_image(code.ljust(table - RESET_PC, b"\0") + cases, unused=IDLE_PC | 1)


class CortexM7FpMulDivTest(unittest.TestCase):
    def test_predicated_multiply_and_divide_at_full_flash_size(self):
        image = arithmetic_image()
        self.assertEqual(len(image), FLASH_SIZE)
        sim = firmware_simulation(image)
        results = []
        last_word_read = False
        for _ in range(32 * len(CASES)):
            before = sim.values
            last_word_read |= before["BUS_READ"] and before["BUS_ADDR"] == FLASH_SIZE - 4
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x}, IR={after['IR']:#x}")
            if after["RETIRED"] != before["RETIRED"] and after["LAST_PC"] == RESULT_PC:
                results.append((after["R4"], after["R5"]))
            if after["PC"] == IDLE_PC:
                break
        else:
            self.fail("The FP multiply and divide firmware did not finish.")

        self.assertTrue(last_word_read)
        self.assertEqual(len(results), len(CASES))
        for (description, _, expected), actual in zip(CASES, results, strict=True):
            with self.subTest(case=description):
                self.assertEqual(actual, expected)
