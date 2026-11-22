"""Check Cortex-M7 DSP arithmetic and the associated APSR state."""

from struct import pack, pack_into
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import firmware_simulation, thumb_image


BASE_FLAGS = 0xA0090000
Q = 1 << 27
RESET_PC = 0x44
IT_EQ = 0xBF08
NOP = 0xBF00

# Assembled Thumb halfwords, R0/R1/R3 operands, and expected R2/APSR after MRS.
CASES = (
    ("SMLATT signed product", (0xFB10, 0x3231),
     (0xFFFE0000, 0x00040000, 0x12345678), 0x12345670, BASE_FLAGS),
    ("SMLAD overflow", (0xFB20, 0x3201),
     (0x80008000, 0x80008000, 0), 0x80000000, BASE_FLAGS | Q),
    # DDI 0403E.e defines Q from the exact final accumulator. Here the negative
    # addend brings the result back into signed 32-bit range.
    ("SMLAD exact final result", (0xFB20, 0x3201),
     (0x80008000, 0x80008000, 0xFFFFFFFF), 0x7FFFFFFF, BASE_FLAGS),
    ("SMLSDX exchange and overflow", (0xFB40, 0x3211),
     (0x80017FFF, 0x80008000, 0x80000000), 0x00010000, BASE_FLAGS | Q),
    ("SMLAWT signed word and halfword", (0xFB30, 0x3211),
     (0xFEDCBA98, 0x8000BEEF, 0x00010000), 0x0092A2B4, BASE_FLAGS),
    ("SMMULR rounds to the next word", (0xFB50, 0xF211),
     (0x40000000, 2, 0), 1, BASE_FLAGS),
    ("SMMLSR subtracts before rounding", (0xFB60, 0x3211),
     (0xFFFFFFFF, 0x80000000, 0x12345678), 0x12345678, BASE_FLAGS),
    ("USADA8 sums unsigned distances", (0xFB70, 0x3201),
     (0xFFFFFF00, 0x000200FF, 0x00001000), 0x000013FA, BASE_FLAGS),
    ("SADD8 records per-byte signs", (0xFA80, 0xF201),
     (0x80FF7F80, 0x7F018001, 0), 0xFF00FF81, 0xA0040000),
    ("UASX records halfword carry and borrow", (0xFAA0, 0xF241),
     (0xFFFFFFFF, 0x00010005, 0), 0x0004FFFE, 0xA00F0000),
    ("QADD16 saturates both lanes", (0xFA90, 0xF211),
     (0x7FFF8000, 0x7FFF8000, 0), 0x7FFF8000, BASE_FLAGS),
    ("UQSUB8 clips underflow", (0xFAC0, 0xF251),
     (0xFFFFFF00, 0x00020001, 0), 0xFFFDFF00, BASE_FLAGS),
    ("SHSAX signed halving", (0xFAE0, 0xF221),
     (0x80007FFE, 0x80017000, 0), 0x8800FFFF, BASE_FLAGS),
    ("UHSUB8 preserves the borrow while halving", (0xFAC0, 0xF261),
     (0xFFFFFF00, 0x00020001, 0), 0x7F7E7FFF, BASE_FLAGS),
    ("SEL reads GE", (0xFAA0, 0xF281),
     (0, 0xFFFFFFFF, 0), 0x00FFFF00, BASE_FLAGS),
    ("QDSUB clips the doubled operand", (0xFA81, 0xF2B0),
     (0x7FFFFFFF, 0x7FFFFFFF, 0), 0, BASE_FLAGS | Q),
    ("SSAT shifts the word first", (0xF300, 0x3218),
     (0x10000123, 0, 0), 0x00123000, BASE_FLAGS),
    ("USAT clips negative arithmetic shift", (0xF3A0, 0x72DF),
     (0x80000001, 0, 0), 0, BASE_FLAGS | Q),
    ("USAT16 clips each signed halfword", (0xF3A0, 0x020E),
     (0x7FFF8000, 0, 0), 0x3FFF0000, BASE_FLAGS | Q),
    ("PKHBT shifted top", (0xEAC0, 0x3201),
     (0x12345678, 0x9ABCDEF0, 0), 0xCDEF5678, BASE_FLAGS),
    ("PKHTB shift by 32", (0xEAC0, 0x0221),
     (0x12345678, 0x9ABCDEF0, 0), 0x1234FFFF, BASE_FLAGS),
    ("SXTAB rotates and extends a signed byte", (0xFA40, 0xF2B1),
     (0x00000201, 0x80005678, 0), 0x00000181, BASE_FLAGS),
    ("SXTB16 uses no accumulator register", (0xFA2F, 0xF2B1),
     (0x12345678, 0x80015678, 0), 0x0056FF80, BASE_FLAGS),
    ("USAT skipped in IT", (0xF380, 0x0200), (1, 0, 0), 99, BASE_FLAGS),
)

# Each case seeds R0 through R4 and LR. The register pair is named explicitly
# because the long instruction can write LR and alias its multiplicands.
LONG_CASES = (
    ("UMULL unsigned high word", (0xFBA0, 0x2301), (0xFFFFFFFF, 2, 3, 4, 0, 0),
     ("R2", "R3"), (0xFFFFFFFE, 1)),
    ("SMULL signed high word", (0xFB80, 0x2301), (0x80000000, 2, 3, 4, 0, 0),
     ("R2", "R3"), (0, 0xFFFFFFFF)),
    ("UMLAL 64-bit wraparound", (0xFBE0, 0x2301), (1, 1, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0),
     ("R2", "R3"), (0, 0)),
    ("SMLAL negative product", (0xFBC0, 0x2301), (0xFFFFFFFF, 2, 4, 3, 0, 0),
     ("R2", "R3"), (2, 3)),
    ("UMAAL two unsigned accumulator terms", (0xFBE0, 0x2361),
     (0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0),
     ("R2", "R3"), (0xFFFFFFFF, 0xFFFFFFFF)),
    ("SMLALTB borrows from the high word", (0xFBC0, 0x23A1),
     (0xFFFE0001, 0xFFFF0003, 3, 5, 0, 0), ("R2", "R3"), (0xFFFFFFFD, 4)),
    ("SMLALD preserves the positive sum above signed 32-bit range", (0xFBC0, 0x23C1),
     (0x80008000, 0x80008000, 0xFFFFFFFF, 1, 0, 0), ("R2", "R3"), (0x7FFFFFFF, 2)),
    ("SMLSLDX sign-extends the exchanged difference", (0xFBD0, 0x23D1),
     (0x80017FFF, 0x80008000, 0, 0, 0, 0), ("R2", "R3"), (0x80010000, 0xFFFFFFFF)),
    ("UMULL reads LR as Rn", (0xFBAE, 0x2304), (0, 0, 4, 5, 3, 0x80000000),
     ("R2", "R3"), (0x80000000, 1)),
    ("SMULL reads LR as Rm", (0xFB80, 0x230E), (3, 0, 4, 5, 0, 0x80000000),
     ("R2", "R3"), (0x80000000, 0xFFFFFFFE)),
    ("SMLALDX reads and writes LR as RdLo", (0xFBC0, 0xE3D1),
     (0x80008000, 0x80008000, 0, 3, 0, 0xFFFFFFFF), ("LR", "R3"), (0x7FFFFFFF, 4)),
    ("UMLAL reads and writes LR as RdHi", (0xFBE0, 0x2E01),
     (0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFF0, 0, 0, 1), ("R2", "LR"), (0xFFFFFFF1, 0xFFFFFFFF)),
    ("UMAAL destinations alias both sources", (0xFBE0, 0x0161), (5, 7, 0, 0, 0, 0),
     ("R0", "R1"), (47, 0)),
    ("UMULL skipped in IT", (0xFBA0, 0x2301), (2, 3, 99, 100, 0, 0),
     ("R2", "R3"), (99, 100)),
)


def dsp_image():
    code = bytearray(2)  # LDR R6, =operand_table
    operands = bytearray()
    observations = {}
    for index, (description, halfwords, values, result, flags) in enumerate(CASES):
        predicate = IT_EQ if index == len(CASES) - 1 else NOP
        # MOVS R2,#99, LDMIA R6!,{R0,R1,R3,R4}, MSR APSR_nzcvqg,R4,
        # one optional IT prefix, the DSP instruction, and MRS R5,APSR.
        code.extend(pack("<9H", 0x2263, 0xCE1B, 0xF384, 0x8C00, predicate,
                         *halfwords, 0xF3EF, 0x8500))
        operands.extend(pack("<4I", *values, BASE_FLAGS))
        observations[RESET_PC + len(code) - 4] = description, ("R2", "R5"), (result, flags)
    return finish_image(code, operands, observations)


def long_image():
    code = bytearray(2)  # LDR R6, =operand_table
    operands = bytearray()
    observations = {}
    for index, (description, halfwords, values, destinations, expected) in enumerate(LONG_CASES):
        flags = BASE_FLAGS | (Q if index % 2 else 0)
        predicate = IT_EQ if index == len(LONG_CASES) - 1 else NOP
        # LDMIA R6!,{R0-R5,R7}, MOV LR,R7, MSR APSR_nzcvqg,R5,
        # one optional IT prefix, the long multiply, and MRS R5,APSR.
        code.extend(pack("<9H", 0xCEBF, 0x46BE, 0xF385, 0x8C00, predicate,
                         *halfwords, 0xF3EF, 0x8500))
        operands.extend(pack("<7I", *values[:5], flags, values[5]))
        observations[RESET_PC + len(code) - 4] = description, (*destinations, "R5"), (*expected, flags)
    return finish_image(code, operands, observations)


def finish_image(code, operands, observations):
    idle = RESET_PC + len(code)
    code.extend(pack("<H", 0xE7FE))
    if len(code) % 4:
        code.extend(pack("<H", NOP))
    # The PC base for the literal load at RESET_PC is RESET_PC + 4.
    pack_into("<H", code, 0, 0x4E00 | ((len(code) - 4) // 4))
    code.extend(pack("<I", RESET_PC + len(code) + 4))
    return thumb_image(code + operands, unused=idle | 1), idle, observations


class CortexM7DspTest(unittest.TestCase):
    def test_assembled_dsp_results_and_apsr(self):
        self.check_firmware(*dsp_image())

    def test_long_multiply_updates_both_destinations(self):
        self.check_firmware(*long_image())

    def check_firmware(self, image, idle, observations):
        sim = firmware_simulation(image)
        checked = set()
        for _ in range(20 * len(observations)):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x}, IR={after['IR']:#x}")
            pc = after["LAST_PC"]
            if after["RETIRED"] != before["RETIRED"] and pc in observations:
                description, registers, expected = observations[pc]
                with self.subTest(instruction=description):
                    self.assertEqual(tuple(after[name] for name in registers), expected)
                checked.add(pc)
            if after["PC"] == idle:
                break
        else:
            self.fail("The DSP firmware did not finish.")
        self.assertEqual(checked, observations.keys())
