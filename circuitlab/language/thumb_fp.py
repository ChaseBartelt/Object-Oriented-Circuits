"""Encode the single-precision Thumb floating-point forms used by the OOC core."""

import re
from fractions import Fraction


_BINARY = {
    "vadd.f32": (0xEE30, 0x0A00),
    "vsub.f32": (0xEE30, 0x0A40),
    "vmul.f32": (0xEE20, 0x0A00),
    "vnmul.f32": (0xEE20, 0x0A40),
    "vdiv.f32": (0xEE80, 0x0A00),
    "vmla.f32": (0xEE00, 0x0A00),
    "vmls.f32": (0xEE00, 0x0A40),
    "vnmla.f32": (0xEE10, 0x0A40),
    "vnmls.f32": (0xEE10, 0x0A00),
    "vfma.f32": (0xEEA0, 0x0A00),
    "vfms.f32": (0xEEA0, 0x0A40),
    "vfnma.f32": (0xEE90, 0x0A40),
    "vfnms.f32": (0xEE90, 0x0A00),
}

_UNARY = {
    "vmov.f32": (0xEEB0, 0x0A40),
    "vabs.f32": (0xEEB0, 0x0AC0),
    "vneg.f32": (0xEEB1, 0x0A40),
    "vsqrt.f32": (0xEEB1, 0x0AC0),
    "vcvt.f32.u32": (0xEEB8, 0x0A40),
    "vcvt.f32.s32": (0xEEB8, 0x0AC0),
    "vcvt.u32.f32": (0xEEBC, 0x0AC0),
    "vcvt.s32.f32": (0xEEBD, 0x0AC0),
    "vcvtr.u32.f32": (0xEEBC, 0x0A40),
    "vcvtr.s32.f32": (0xEEBD, 0x0A40),
}


def _sreg(text):
    match = re.fullmatch(r"s([0-9]|[12][0-9]|3[01])", text)
    if not match:
        raise ValueError(f"Expected a single-precision register s0–s31, received {text!r}.")
    return int(match[1])


def _core_reg(text):
    if text in {"lr", "r14"}:
        return 14
    match = re.fullmatch(r"r([0-9]|1[0-2])", text)
    if not match:
        raise ValueError(f"Expected a core register r0–r12 or lr, received {text!r}.")
    return int(match[1])


def _operands(mnemonic, operands, count):
    if len(operands) != count:
        raise ValueError(f"{mnemonic} expects {count} operands.")
    return tuple(operand.strip().lower() for operand in operands)


def _data(first, second, sd, sm=0, sn=0):
    # Scalar register numbers split their low selector bit from the upper four bits.
    first |= ((sd & 1) << 6) | (sn >> 1)
    second |= ((sd >> 1) << 12) | ((sn & 1) << 7) | ((sm & 1) << 5) | (sm >> 1)
    return first, second


def _immediate(text):
    match = re.fullmatch(r"#([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d{1,2})?)", text)
    if not match:
        raise ValueError(f"Expected a decimal floating-point immediate, received {text!r}.")
    return Fraction(match[1])


# VFP immediates encode a sign bit, a three-bit exponent, and four fraction bits.
_LITERALS = {
    (-1 if field & 128 else 1) * Fraction(16 + (field & 15), 16)
    * Fraction(2) ** (((field >> 4) & 7) ^ 4) / 8: field
    for field in range(256)
}


def encode_fp(mnemonic, operands):
    """Return Thumb halfwords in fetch order, or None for a different instruction."""
    if mnemonic in _BINARY:
        sd, sn, sm = map(_sreg, _operands(mnemonic, operands, 3))
        return _data(*_BINARY[mnemonic], sd, sm, sn)
    if mnemonic in _UNARY:
        left, right = _operands(mnemonic, operands, 2)
        sd = _sreg(left)
        if mnemonic == "vmov.f32" and right.startswith("#"):
            immediate = _LITERALS.get(_immediate(right))
            if immediate is None:
                raise ValueError(f"{right} cannot be encoded as a vmov.f32 immediate.")
            return _data(0xEEB0 | (immediate >> 4), 0x0A00 | (immediate & 15), sd)
        return _data(*_UNARY[mnemonic], sd, _sreg(right))
    if mnemonic in {"vcmp.f32", "vcmpe.f32"}:
        left, right = _operands(mnemonic, operands, 2)
        sd = _sreg(left)
        second = 0x0AC0 if mnemonic == "vcmpe.f32" else 0x0A40
        if right.startswith("#"):
            if _immediate(right):
                raise ValueError(f"{mnemonic} only supports zero as an immediate.")
            return _data(0xEEB5, second, sd)
        return _data(0xEEB4, second, sd, _sreg(right))
    if mnemonic == "vmov":
        left, right = _operands(mnemonic, operands, 2)
        from_fp = not left.startswith("s")
        sn = _sreg(right if from_fp else left)
        rt = _core_reg(left if from_fp else right)
        first = 0xEE10 if from_fp else 0xEE00
        return first | (sn >> 1), 0x0A10 | (rt << 12) | ((sn & 1) << 7)
    if mnemonic in {"vmrs", "vmsr"}:
        left, right = _operands(mnemonic, operands, 2)
        read = mnemonic == "vmrs"
        system, core = (right, left) if read else (left, right)
        if system != "fpscr":
            raise ValueError(f"{mnemonic} requires FPSCR as its system register.")
        rt = 15 if read and core == "apsr_nzcv" else _core_reg(core)
        return (0xEEF1 if read else 0xEEE1), 0x0A10 | (rt << 12)
    return None
