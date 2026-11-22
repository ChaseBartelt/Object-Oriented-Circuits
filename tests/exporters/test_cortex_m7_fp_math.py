"""Exercise single-precision ARM arithmetic through the firmware entry point."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import firmware_simulation, thumb_image


ROUND_UP = 1 << 22
ROUND_DOWN = 2 << 22
ROUND_ZERO = 3 << 22
FLUSH_TO_ZERO = 1 << 24
INVALID = 1
OVERFLOW = 1 << 2
UNDERFLOW = 1 << 3
INEXACT = 1 << 4
INPUT_DENORMAL = 1 << 7

# Each case gives the two binary32 operands and FPSCR, then the result and FPSCR.
CASES = (
    ("sticky flags", (0x3F800000, 0x3F800000, 2), (0x40000000, 2)),
    ("nearest ties to even", (0x3F800000, 0x33800000, 0), (0x3F800000, INEXACT)),
    ("round a tie upward", (0x3F800000, 0x33800000, ROUND_UP), (0x3F800001, ROUND_UP | INEXACT)),
    ("signed zero after cancellation", (0x3F800000, 0xBF800000, ROUND_DOWN), (0x80000000, ROUND_DOWN)),
    ("exact subnormal", (0x00800000, 0x807FFFFF, 0), (0x00000001, 0)),
    ("flush a subnormal input", (0x00800000, 0x807FFFFF, FLUSH_TO_ZERO),
     (0x00800000, FLUSH_TO_ZERO | INPUT_DENORMAL)),
    ("flush a subnormal result", (0x00800001, 0x80800000, FLUSH_TO_ZERO),
     (0, FLUSH_TO_ZERO | UNDERFLOW)),
    ("opposite infinities", (0x7F800000, 0xFF800000, 0), (0x7FC00000, INVALID)),
    ("quiet a signaling NaN before propagating", (0x7FC12345, 0xFF800001, 0), (0xFFC00001, INVALID)),
    ("overflow under rounding toward zero", (0x7F7FFFFF, 0x7F7FFFFF, ROUND_ZERO),
     (0x7F7FFFFF, ROUND_ZERO | OVERFLOW | INEXACT)),
    ("round a negative value downward", (0xBF800000, 0xB3000000, ROUND_DOWN),
     (0xBF800001, ROUND_DOWN | INEXACT)),
)

RESULT_PC = 0x6C
IDLE_PC = 0x74
CASE_TABLE = 0x84


def arithmetic_image():
    # Assembled for Cortex-M7 FPv5-SP-D16 and linked with .text at 0x44.
    instructions = (
        0x4E0C, 0x4F0D, 0x6037,  # Load CPACR address and value, then enable the FPU.
        0xF3BF, 0x8F4F,          # DSB SY
        0xF3BF, 0x8F6F,          # ISB SY
        0x4E0B,                  # LDR R6, the pointer to the case table.
        0x2700 | len(CASES),     # MOVS R7, the case count.
        0xCE07,                  # LDMIA R6!, {R0-R2}
        0xEE00, 0x0A90,          # VMOV S1, R0
        0xEE0F, 0x1A90,          # VMOV S31, R1
        0xEEE1, 0x2A10,          # VMSR FPSCR, R2
        0xEE30, 0x0AAF,          # VADD.F32 S0, S1, S31
        0xEE10, 0x4A10,          # VMOV R4, S0
        0xEEF1, 0x5A10,          # VMRS R5, FPSCR
        0x3F01, 0xD1F0,          # SUBS R7, #1 and BNE to the LDMIA.
        0xE7FE, 0xBF00,          # B . and NOP padding for the literal pool.
    )
    code = pack(f"<{len(instructions)}H", *instructions)
    literals = pack("<3I", 0xE000ED88, 0x00F00000, CASE_TABLE)
    cases = b"".join(pack("<3I", *operands) for _, operands, _ in CASES)
    return thumb_image(code + literals + cases, unused=IDLE_PC | 1)


class CortexM7FpMathTest(unittest.TestCase):
    def test_single_precision_add_results_and_fpscr(self):
        sim = firmware_simulation(arithmetic_image())
        results = []
        for _ in range(32 * len(CASES)):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x}, IR={after['IR']:#x}")
            if after["RETIRED"] != before["RETIRED"] and after["LAST_PC"] == RESULT_PC:
                results.append((after["R4"], after["R5"]))
            if after["PC"] == IDLE_PC:
                break
        else:
            self.fail("The arithmetic firmware did not finish.")

        self.assertEqual(len(results), len(CASES))
        for (description, _, expected), actual in zip(CASES, results, strict=True):
            with self.subTest(case=description):
                self.assertEqual(actual, expected)
