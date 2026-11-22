"""Exercise assembled signed and unsigned divide and register shifts."""

from random import Random
from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import firmware_simulation, thumb_image


WORD = 0xFFFFFFFF
SIGN = 1 << 31
RESET_PC = 0x44
TABLE = 0x80
IDLE = 0x76
OPS = {
    0x4E: ("UDIV", "R3"),
    0x52: ("SDIV", "R4"),
    0x58: ("LSL", "R5"),
    0x5E: ("LSR", "R8"),
    0x68: ("ASR", "R5"),
    0x6E: ("ROR", "R9"),
}


def cases():
    rng = Random(7319)
    edges = [
        (0, 0), (SIGN, WORD), (SIGN, 1), (WORD, SIGN), (SIGN, SIGN),
        (0xFFFFFFF7, 2), (9, 0xFFFFFFFD),
        (1, 32), (SIGN | 1, 32), (0, 33), (WORD, 256),
    ]
    distances = (0, 1, 7, 8, 15, 16, 23, 31, 32, 33, 63, 64, 127, 128, 255, 256, 257, 288, 289, WORD)
    edges.extend((rng.getrandbits(32), distance) for distance in distances)
    edges.extend((rng.getrandbits(32), rng.randrange(1, 256)) for _ in range(16))
    edges.extend((rng.getrandbits(32), rng.getrandbits(32)) for _ in range(64 - len(edges)))
    return [(a, b, rng.getrandbits(4)) for a, b in edges]


def signed(value):
    return (value ^ SIGN) - SIGN


def expected(op, a, b, nzcv):
    if op in {"UDIV", "SDIV"}:
        if op == "SDIV":
            a, b = signed(a), signed(b)
        magnitude = abs(a) // abs(b) if b else 0
        return (-magnitude if (a < 0) != (b < 0) else magnitude) & WORD, nzcv
    distance = b & 255
    if distance == 0:
        result, carry = a, (nzcv >> 1) & 1
    elif op == "LSL":
        result = (a << distance) & WORD
        carry = (a >> (32 - distance)) & 1 if distance <= 32 else 0
    elif op == "LSR":
        result = a >> distance
        carry = (a >> (distance - 1)) & 1 if distance <= 32 else 0
    elif op == "ASR":
        amount = min(distance, 32)
        result = (signed(a) >> amount) & WORD
        carry = (a >> (amount - 1)) & 1
    else:
        rotate = distance % 32
        result = ((a >> rotate) | (a << (32 - rotate))) & WORD
        carry = result >> 31
    return result, ((result >> 31) << 3) | ((result == 0) << 2) | (carry << 1) | (nzcv & 1)


def arithmetic_image(rows):
    # Zig's ARM assembler generated these Cortex-M7 Thumb encodings.
    instructions = (
        0x4E0C, 0x2700 | len(rows),  # Load the case table pointer and count.
        0xCE07,                    # LDMIA R6!, {R0-R2}
        0xF382, 0x8800,            # MSR APSR_nzcvq, R2
        0xFBB0, 0xF3F1,            # UDIV R3, R0, R1
        0xFB90, 0xF4F1,            # SDIV R4, R0, R1
        0x4605, 0x408D,            # MOV R5, R0 and LSLS R5, R1
        0xF382, 0x8800,            # Reset NZCV before each following shift.
        0xFA30, 0xF801,            # LSRS.W R8, R0, R1
        0x4605,                    # MOV R5, R0
        0xF382, 0x8800,            # MSR APSR_nzcvq, R2
        0x410D,                    # ASRS R5, R1
        0xF382, 0x8800,            # MSR APSR_nzcvq, R2
        0xFA70, 0xF901,            # RORS.W R9, R0, R1
        0x3F01, 0xD1E8,            # SUBS R7, #1 and BNE to LDMIA.
        0xE7FE,                    # B .
    )
    code = pack(f"<{len(instructions)}H", *instructions) + pack("<I", TABLE)
    operands = b"".join(pack("<3I", a, b, flags << 28) for a, b, flags in rows)
    return thumb_image(code.ljust(TABLE - RESET_PC, b"\0") + operands, unused=IDLE | 1)


class CortexM7ThumbArithmeticTest(unittest.TestCase):
    def test_integer_division_and_register_shift_result_and_flags(self):
        rows = cases()
        sim = firmware_simulation(arithmetic_image(rows))
        index = 0
        count = 0
        for _ in range(35 * len(rows)):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x}, IR={after['IR']:#x}")
            pc = after["LAST_PC"]
            if after["RETIRED"] != before["RETIRED"] and pc in OPS:
                op, register = OPS[pc]
                a, b, flags = rows[index]
                self.assertEqual((after[register], after["NZCV"]), expected(op, a, b, flags),
                                 f"{op}: R0={a:#010x}, R1={b:#010x}, initial NZCV={flags:#x}")
                count += 1
                if op == "ROR":
                    index += 1
            if after["PC"] == IDLE:
                break
        else:
            self.fail("The Thumb arithmetic firmware did not finish.")
        self.assertEqual((index, count), (len(rows), len(rows) * len(OPS)))


if __name__ == "__main__":
    unittest.main()
