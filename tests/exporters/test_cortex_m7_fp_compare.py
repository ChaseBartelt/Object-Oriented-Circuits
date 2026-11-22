"""Run Thumb floating-point comparisons and conversions through the OOC core."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import firmware_simulation, thumb_image


ROUND_DOWN = 2 << 22
FLUSH_TO_ZERO = 1 << 24
INVALID = 1
INEXACT = 1 << 4
INPUT_DENORMAL = 1 << 7
UNORDERED, EQUAL, GREATER, LESS = tuple(nzcv << 28 for nzcv in (3, 6, 2, 8))

# Assembled for Cortex-M7 FPv5-SP-D16. S31 holds the comparison's first
# operand or conversion destination. S30 holds the second/source operand.
VCMP = (0xEEF4, 0xFA4F)
VCMPE = (0xEEF4, 0xFACF)
VCMP_ZERO = (0xEEF5, 0xFA40)
VCMPE_ZERO = (0xEEF5, 0xFAC0)
SIGNED_TO_FLOAT = (0xEEF8, 0xFACF)
UNSIGNED_TO_FLOAT = (0xEEF8, 0xFA4F)
FLOAT_TO_SIGNED = (0xEEFD, 0xFACF)
FLOAT_TO_UNSIGNED = (0xEEFC, 0xFACF)
FLOAT_TO_SIGNED_ROUND = (0xEEFD, 0xFA4F)
FLOAT_TO_UNSIGNED_ROUND = (0xEEFC, 0xFA4F)

# Each case gives the encoded instruction, S31/S30/FPSCR inputs, and
# expected S31/FPSCR outputs. Comparisons leave both source registers intact.
CASES = (
    ("opposite signed zeros", VCMP, (0, 0x80000000, 2), (0, EQUAL | 2)),
    ("signed ordering", VCMP, (0xBF800000, 0xC0000000, 0), (0xBF800000, GREATER)),
    ("ignore second register when comparing with zero", VCMP_ZERO,
     (0xBF800000, 0x7F800001, 0), (0xBF800000, LESS)),
    ("quiet NaN", VCMP, (0x7FC00000, 0, 0), (0x7FC00000, UNORDERED)),
    ("signal any NaN", VCMPE, (0x7FC00000, 0, 0), (0x7FC00000, UNORDERED | INVALID)),
    ("signal NaN when comparing with zero", VCMPE_ZERO,
     (0x7FC00000, 0, 0), (0x7FC00000, UNORDERED | INVALID)),
    ("signaling NaN", VCMP, (0x7F800001, 0, 0), (0x7F800001, UNORDERED | INVALID)),
    ("flush input denormal", VCMP_ZERO,
     (0x80000001, 0, FLUSH_TO_ZERO), (0x80000001, FLUSH_TO_ZERO | INPUT_DENORMAL | EQUAL)),
    ("signed minimum", SIGNED_TO_FLOAT, (0, 0x80000000, 0), (0xCF000000, 0)),
    ("unsigned maximum rounds upward", UNSIGNED_TO_FLOAT, (0, 0xFFFFFFFF, 0), (0x4F800000, INEXACT)),
    ("VCVT uses truncation", FLOAT_TO_SIGNED, (0, 0xBFE00000, ROUND_DOWN), (0xFFFFFFFF, ROUND_DOWN | INEXACT)),
    ("VCVTR rounds to nearest even", FLOAT_TO_SIGNED_ROUND, (0, 0x3FC00000, 0), (2, INEXACT)),
    ("unsigned negative fraction truncates to zero", FLOAT_TO_UNSIGNED, (0, 0xBF400000, 0), (0, INEXACT)),
    ("unsigned round-down outside range", FLOAT_TO_UNSIGNED_ROUND, (0, 0xBF400000, ROUND_DOWN),
     (0, ROUND_DOWN | INVALID)),
    ("infinity saturates", FLOAT_TO_SIGNED, (0, 0x7F800000, 0), (0x7FFFFFFF, INVALID)),
    ("NaN casts to zero", FLOAT_TO_UNSIGNED, (0, 0x7FC00000, 0), (0, INVALID)),
)

ENTRY = 0x44
CAPTURE = 0x2701  # MOVGE R7, #1 under IT GE
IDLE = 0xE7FE


def compare_image():
    code = [
        0, 0, 0x6037,            # LDR R6, CPACR, LDR R7, enable bits, STR R7, [R6]
        0xF3BF, 0x8F4F,          # DSB SY
        0xF3BF, 0x8F6F,          # ISB SY
        0,                       # LDR R6, table address
    ]
    for _, instruction, _, _ in CASES:
        code.extend((
            0xCE07,              # LDMIA R6!, {R0-R2}
            0xEE0F, 0x0A90,      # VMOV S31, R0
            0xEE0F, 0x1A10,      # VMOV S30, R1
            0xEEE1, 0x2A10,      # VMSR FPSCR, R2
            *instruction,
            0xEE1F, 0x4A90,      # VMOV R4, S31
            0xEEF1, 0x5A10,      # VMRS R5, FPSCR
            0x2700,              # MOVS R7, #0
            0xEEF1, 0xFA10,      # VMRS APSR_NZCV, FPSCR
            0xBFA8, CAPTURE,     # IT GE; MOVGE R7, #1
        ))
    idle_pc = ENTRY + len(code) * 2
    code.append(IDLE)
    if len(code) % 2:
        code.append(0xBF00)     # Align the literal pool.
    literals = ENTRY + len(code) * 2
    for index, register, address in ((0, 6, literals), (1, 7, literals + 4), (7, 6, literals + 8)):
        pc_base = (ENTRY + index * 2 + 4) & ~3
        code[index] = 0x4800 | (register << 8) | ((address - pc_base) // 4)
    program = pack(f"<{len(code)}H3I", *code, 0xE000ED88, 0x00F00000, literals + 12)
    table = b"".join(pack("<3I", *operands) for _, _, operands, _ in CASES)
    return thumb_image(program + table, unused=idle_pc | 1), idle_pc


class CortexM7FpCompareTest(unittest.TestCase):
    def test_compare_convert_and_branch_on_fpscr(self):
        image, idle_pc = compare_image()
        sim = firmware_simulation(image)
        captures = []
        for _ in range(32 * len(CASES)):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x}, IR={after['IR']:#x}")
            if after["RETIRED"] != before["RETIRED"] and after["LAST_IR"] == CAPTURE:
                captures.append(tuple(after[name] for name in ("R4", "R5", "NZCV", "R7")))
            if after["PC"] == idle_pc:
                break
        else:
            self.fail("The comparison firmware did not finish.")

        self.assertEqual(len(captures), len(CASES))
        for (description, _, _, expected), actual in zip(CASES, captures, strict=True):
            value, status = expected
            flags = status >> 28
            ge = (flags >> 3) == (flags & 1)
            with self.subTest(case=description):
                self.assertEqual(actual, (value, status, flags, ge))
