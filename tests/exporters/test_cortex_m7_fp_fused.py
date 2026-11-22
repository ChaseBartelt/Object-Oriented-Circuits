"""Exercise the fused and separately rounded FP instructions through the firmware core."""

from struct import pack
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source
from tests.exporters.test_cortex_m7_freertos_blocking import FIRMWARE, firmware_simulation, thumb_image


VFMA = (0xEEEE, 0xFA8F)
VFMS = (0xEEEE, 0xFACF)
VFNMA = (0xEEDE, 0xFACF)
VFNMS = (0xEEDE, 0xFA8F)
VMLA = (0xEE4E, 0xFA8F)
VMLS = (0xEE4E, 0xFACF)
VNMLA = (0xEE5E, 0xFACF)
VNMLS = (0xEE5E, 0xFA8F)
VNMUL = (0xEE6E, 0xFACF)
ROUND_UP = 1 << 22
ROUND_DOWN = 2 << 22
FLUSH_TO_ZERO = 1 << 24
DEFAULT_NAN = 1 << 25
INVALID = 1
OVERFLOW = 1 << 2
UNDERFLOW = 1 << 3
INEXACT = 1 << 4
INPUT_DENORMAL = 1 << 7

# Assembled for Cortex-M7 FPv5-SP-D16. S31 is the accumulator and S29 and
# S30 hold the factors. Each case specifies accumulator, factors, and FPSCR.
CASES = (
    ("VFMA retains a product rounding would erase", VFMA,
     (0xBF800000, 0x3F800001, 0x3F7FFFFE, 0), (0xA8800000, 0)),
    ("VFMS subtracts the exact product", VFMS,
     (0x3F800000, 0x3F800001, 0x3F7FFFFE, 0), (0x28800000, 0)),
    ("VFNMA negates both terms", VFNMA,
     (0xBF800000, 0x3F800001, 0x3F7FFFFE, 0), (0x28800000, 0)),
    ("VFNMS subtracts the accumulator", VFNMS,
     (0x3F800000, 0x3F800001, 0x3F7FFFFE, 0), (0xA8800000, 0)),
    ("accumulator cancels product overflow", VFMA,
     (0xFF7FFFFF, 0x7F7FFFFF, 0x40000000, 0), (0x7F7FFFFF, 0)),
    ("ties round to even", VFMA,
     (0x3F800000, 0x33800000, 0x3F800000, 2), (0x3F800000, 2 | INEXACT)),
    ("ties round upward", VFMA,
     (0x3F800000, 0x33800000, 0x3F800000, ROUND_UP), (0x3F800001, ROUND_UP | INEXACT)),
    ("exact cancellation under round down", VFMA,
     (0xBF800000, 0x3F800000, 0x3F800000, ROUND_DOWN), (0x80000000, ROUND_DOWN)),
    ("exact subnormal", VFMA, (0, 0x00800000, 0x3F000000, 0), (0x00400000, 0)),
    ("flush tiny output", VFMA,
     (0, 0x00800000, 0x3F000000, FLUSH_TO_ZERO), (0, FLUSH_TO_ZERO | UNDERFLOW)),
    ("flush denormal input", VFMA,
     (0x3F800000, 1, 0x3F800000, FLUSH_TO_ZERO), (0x3F800000, FLUSH_TO_ZERO | INPUT_DENORMAL)),
    ("product underflows at final rounding", VFMA,
     (0, 1, 0x3F000000, ROUND_UP), (1, ROUND_UP | UNDERFLOW | INEXACT)),
    ("opposite infinities are invalid", VFMA,
     (0xFF800000, 0x7F800000, 0x3F800000, 0), (0x7FC00000, INVALID)),
    ("invalid product takes precedence over quiet NaN", VFMA,
     (0xFFC01234, 0, 0x7F800000, 0), (0x7FC00000, INVALID)),
    ("signaling NaN is quieted first", VFMA,
     (0x7FC01234, 0xFF800005, 0x3F800000, 0), (0xFFC00005, INVALID)),
    ("default NaN mode", VFMA,
     (0xFFC01234, 0x3F800000, 0x3F800000, DEFAULT_NAN), (0x7FC00000, DEFAULT_NAN)),
    ("VMLA rounds before cancellation", VMLA,
     (0xBF800000, 0x3F800001, 0x3F7FFFFE, 0), (0, INEXACT)),
    ("VMLA keeps an intermediate overflow", VMLA,
     (0xFF7FFFFF, 0x7F7FFFFF, 0x40000000, 0), (0x7F800000, OVERFLOW | INEXACT)),
    ("VMLA combines exceptions from both stages", VMLA,
     (0xFF800000, 0x7F7FFFFF, 0x40000000, 0), (0x7FC00000, OVERFLOW | INEXACT | INVALID)),
    ("VMLS subtracts the rounded product", VMLS,
     (0x40000000, 0x40400000, 0x40800000, 0), (0xC1200000, 0)),
    ("VNMLA negates both terms", VNMLA,
     (0x40000000, 0x40400000, 0x40800000, 0), (0xC1600000, 0)),
    ("VNMLS negates only the accumulator", VNMLS,
     (0x40000000, 0x40400000, 0x40800000, 0), (0x41200000, 0)),
    ("VNMUL negates after directed rounding", VNMUL,
     (0x40000000, 0x3F800001, 0x3F7FFFFE, ROUND_UP), (0xBF800000, ROUND_UP | INEXACT)),
)

ENTRY = 0x44
CAPTURE = 0x5A10EEF1  # VMRS R5, FPSCR


def fused_image():
    code = [
        0, 0, 0x6037,            # LDR R6, CPACR; LDR R7, enable bits; STR R7, [R6]
        0xF3BF, 0x8F4F,          # DSB SY
        0xF3BF, 0x8F6F,          # ISB SY
        0,                       # LDR R6, table address
    ]
    for _, instruction, _, _ in CASES:
        code.extend((
            0xCE0F,              # LDMIA R6!, {R0-R3}
            0xEE0F, 0x0A90,      # VMOV S31, R0
            0xEE0E, 0x1A90,      # VMOV S29, R1
            0xEE0F, 0x2A10,      # VMOV S30, R2
            0xEEE1, 0x3A10,      # VMSR FPSCR, R3
            *instruction,
            0xEE1F, 0x4A90,      # VMOV R4, S31
            0xEEF1, 0x5A10,      # VMRS R5, FPSCR
        ))
    idle_pc = ENTRY + len(code) * 2
    code.append(0xE7FE)
    if len(code) % 2:
        code.append(0xBF00)      # Align the literal pool.
    literals = ENTRY + len(code) * 2
    for index, register, address in ((0, 6, literals), (1, 7, literals + 4), (7, 6, literals + 8)):
        pc_base = (ENTRY + index * 2 + 4) & ~3
        code[index] = 0x4800 | (register << 8) | ((address - pc_base) // 4)
    program = pack(f"<{len(code)}H3I", *code, 0xE000ED88, 0x00F00000, literals + 12)
    table = b"".join(pack("<4I", *operands) for _, _, operands, _ in CASES)
    return thumb_image(program + table, unused=idle_pc | 1), idle_pc


class CortexM7FpFusedTest(unittest.TestCase):
    def test_inline_thumb_runs_sqrt_and_fma(self):
        before, begin, rest = read_model_source(FIRMWARE).partition("// BEGIN LINKED FIRMWARE")
        _, end, after = rest.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        rom = """
chip M7FirmwareRom(address, word, valid) {
    address[32] = input;
    word[32] = output;
    valid = output;
    arm(address, word, valid) {
        .word 0x20000400;
        .word reset + 1;
    reset:
        LDR r6, cpacr;
        LDR r7, fp_enable;
        STR r7, [r6];
        DSB SY;
        ISB SY;
        VMOV.F32 s0, #9.0;
        VSQRT.F32 s1, s0;
        VMOV.F32 s2, #2.0;
        VFMA.F32 s2, s1, s0;
        VMOV r0, s2;
    idle:
        B idle;
        .align 2;
    cpacr: .word 0xE000ED88;
    fp_enable: .word 0x00F00000;
    }
}
"""
        sim = DigitalSimulation(before + begin + "\n" + rom + end + after)
        for _ in range(32):
            state = sim.step()
            self.assertFalse(state["FAULT"], state["FAULT_CODE"])
        self.assertEqual(state["R0"], 0x41E80000)  # 2 + sqrt(9) * 9 = 29
        self.assertEqual(state["FPSCR"], 0)

    def test_multiply_accumulate_results_and_fpscr(self):
        image, idle_pc = fused_image()
        sim = firmware_simulation(image)
        captures = []
        for _ in range(28 * len(CASES)):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x}, IR={after['IR']:#x}")
            if after["RETIRED"] != before["RETIRED"] and after["LAST_IR"] == CAPTURE:
                captures.append((after["R4"], after["R5"]))
            if after["PC"] == idle_pc:
                break
        else:
            self.fail("The floating-point firmware did not finish.")

        self.assertEqual(len(captures), len(CASES))
        for (description, _, _, expected), actual in zip(CASES, captures, strict=True):
            with self.subTest(case=description):
                self.assertEqual(actual, expected)
