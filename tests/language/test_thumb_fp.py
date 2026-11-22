"""Check Thumb floating-point encodings against Arm assembler output."""

import struct
import unittest

from circuitlab.language.thumb_fp import encode_fp


class ThumbFloatingPointTests(unittest.TestCase):
    def test_fp_instructions_match_assembler_bytes(self):
        cases = (
            ("vadd.f32", ("s0", "s1", "s31"), "30 ee af 0a"),
            ("vsub.f32", ("s17", "s30", "s29"), "7f ee 6e 8a"),
            ("vmul.f32", ("s0", "s1", "s31"), "20 ee af 0a"),
            ("vdiv.f32", ("s17", "s30", "s29"), "cf ee 2e 8a"),
            ("vmla.f32", ("s4", "s0", "s6"), "00 ee 03 2a"),
            ("vmls.f32", ("s31", "s29", "s30"), "4e ee cf fa"),
            ("vnmla.f32", ("s31", "s29", "s30"), "5e ee cf fa"),
            ("vnmls.f32", ("s31", "s29", "s30"), "5e ee 8f fa"),
            ("vnmul.f32", ("s31", "s29", "s30"), "6e ee cf fa"),
            ("vfma.f32", ("s17", "s30", "s29"), "ef ee 2e 8a"),
            ("vfms.f32", ("s0", "s1", "s31"), "a0 ee ef 0a"),
            ("vfnma.f32", ("s17", "s30", "s29"), "df ee 6e 8a"),
            ("vfnms.f32", ("s0", "s1", "s31"), "90 ee af 0a"),
            ("vsqrt.f32", ("s17", "s30"), "f1 ee cf 8a"),
            ("vabs.f32", ("s0", "s31"), "b0 ee ef 0a"),
            ("vneg.f32", ("s17", "s30"), "f1 ee 4f 8a"),
            ("vcmp.f32", ("s0", "s31"), "b4 ee 6f 0a"),
            ("vcmpe.f32", ("s31", "#0.0"), "f5 ee c0 fa"),
            ("vcvt.f32.u32", ("s17", "s30"), "f8 ee 4f 8a"),
            ("vcvt.f32.s32", ("s0", "s31"), "b8 ee ef 0a"),
            ("vcvt.u32.f32", ("s17", "s30"), "fc ee cf 8a"),
            ("vcvt.s32.f32", ("s0", "s31"), "bd ee ef 0a"),
            ("vcvtr.u32.f32", ("s17", "s30"), "fc ee 4f 8a"),
            ("vcvtr.s32.f32", ("s0", "s31"), "bd ee 6f 0a"),
            ("vmov.f32", ("s17", "s30"), "f0 ee 4f 8a"),
            ("vmov.f32", ("s29", "#1.0"), "f7 ee 00 ea"),
            ("vmov", ("s31", "r12"), "0f ee 90 ca"),
            ("vmov", ("lr", "s29"), "1e ee 90 ea"),
            ("vmsr", ("fpscr", "r11"), "e1 ee 10 ba"),
            ("vmrs", ("r8", "fpscr"), "f1 ee 10 8a"),
            ("vmrs", ("apsr_nzcv", "fpscr"), "f1 ee 10 fa"),
        )
        for mnemonic, operands, expected in cases:
            with self.subTest(mnemonic=mnemonic, operands=operands):
                self.assertEqual(struct.pack("<2H", *encode_fp(mnemonic, operands)), bytes.fromhex(expected))

    def test_rejects_invalid_fp_operands(self):
        for mnemonic, operands, expected in (
            ("vsqrt.f32", ("s0", "s32"), "single-precision register"),
            ("vmul.f32", ("s0", "s1"), "expects 3 operands"),
            ("vmov.f32", ("s29", "#0.0"), "cannot be encoded"),
            ("vcmp.f32", ("s0", "#1.0"), "only supports zero"),
            ("vmrs", ("sp", "fpscr"), "core register"),
        ):
            with self.subTest(mnemonic=mnemonic, operands=operands):
                with self.assertRaisesRegex(ValueError, expected):
                    encode_fp(mnemonic, operands)


if __name__ == "__main__":
    unittest.main()
