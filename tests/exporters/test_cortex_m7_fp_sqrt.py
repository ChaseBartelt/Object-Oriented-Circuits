"""Exercise assembled VSQRT.F32 and FPSCR through the firmware entry point."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import firmware_simulation, thumb_image


ROUND_UP = 1 << 22
ROUND_DOWN = 2 << 22
FLUSH = 1 << 24
DEFAULT_NAN = 1 << 25
INVALID = 1
INEXACT = 1 << 4
INPUT_DENORMAL = 1 << 7
RESULT_PC = 0x70
IDLE_PC = 0x78
TABLE = 0x88

# Each operand row contains the binary32 value, FPSCR and the IT skip selector.
CASES = (
    ("keep negative zero", (0x80000000, 0, 0), (0x80000000, 0)),
    ("positive infinity", (0x7F800000, 0, 0), (0x7F800000, 0)),
    ("negative infinity", (0xFF800000, 0, 0), (0x7FC00000, INVALID)),
    ("negative finite value", (0xBF800000, 0, 0), (0x7FC00000, INVALID)),
    ("exact result preserves sticky flags", (0x40800000, 0x80000002, 0), (0x40000000, 0x80000002)),
    ("square root of two to nearest", (0x40000000, 0, 0), (0x3FB504F3, INEXACT)),
    ("square root of two upward", (0x40000000, ROUND_UP, 0), (0x3FB504F4, ROUND_UP | INEXACT)),
    ("square root of two downward", (0x40000000, ROUND_DOWN, 0), (0x3FB504F3, ROUND_DOWN | INEXACT)),
    ("round across an exponent boundary", (0x407FFFFF, ROUND_UP, 0), (0x40000000, ROUND_UP | INEXACT)),
    ("smallest subnormal input", (0x00000001, 0, 0), (0x1A3504F3, INEXACT)),
    ("exact root of a subnormal", (0x00000002, 0, 0), (0x1A800000, 0)),
    ("flush negative subnormal to signed zero", (0x80000001, FLUSH, 0),
     (0x80000000, FLUSH | INPUT_DENORMAL)),
    ("quiet a signaling NaN payload", (0xFF800123, 0, 0), (0xFFC00123, INVALID)),
    ("default NaN mode", (0xFFC12345, DEFAULT_NAN, 0), (0x7FC00000, DEFAULT_NAN)),
    ("predicated off leaves destination and FPSCR", (0xBF800000, ROUND_UP, 1),
     (0xBF800000, ROUND_UP)),
)


def sqrt_image():
    # Assembled for Cortex-M7 FPv5-SP-D16 with .text beginning at 0x44.
    instructions = (
        0x4E0D, 0x4F0E, 0x6037,  # Enable CP10 and CP11 in CPACR.
        0xF3BF, 0x8F4F,          # DSB SY
        0xF3BF, 0x8F6F,          # ISB SY
        0x4E0C,                  # LDR R6, case table address
        0x2700 | len(CASES),     # MOVS R7, case count
        0xCE07,                  # LDMIA R6!, {R0-R2}
        0xEE0F, 0x0A90,          # VMOV S31, R0
        0xEE00, 0x0A10,          # VMOV S0, R0
        0xEEE1, 0x1A10,          # VMSR FPSCR, R1
        0x2A00, 0xBF08,          # CMP R2, #0 and IT EQ
        0xEEB1, 0x0AEF,          # VSQRTEQ.F32 S0, S31
        0xEE10, 0x4A10,          # VMOV R4, S0
        0xEEF1, 0x5A10,          # VMRS R5, FPSCR
        0x3F01, 0xD1EE,          # SUBS R7, #1 and BNE to the LDMIA
        0xE7FE, 0xBF00,          # B . and NOP padding before the literal pool
    )
    code = pack(f"<{len(instructions)}H", *instructions)
    literals = pack("<3I", 0xE000ED88, 0x00F00000, TABLE)
    cases = b"".join(pack("<3I", *operands) for _, operands, _ in CASES)
    return thumb_image(code + literals + cases, unused=IDLE_PC | 1)


class CortexM7FpSqrtTest(unittest.TestCase):
    def test_sqrt_result_rounding_status_and_predication(self):
        sim = firmware_simulation(sqrt_image())
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
            self.fail("The FP square root firmware did not finish.")

        self.assertEqual(len(results), len(CASES))
        for (description, _, expected), actual in zip(CASES, results, strict=True):
            with self.subTest(case=description):
                self.assertEqual(actual, expected)
