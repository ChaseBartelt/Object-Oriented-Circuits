"""Assemble a bounded Arm Thumb program and pack its bytes into ROM words."""

from collections.abc import Iterable
from dataclasses import dataclass
import re

from circuitlab.language.thumb_fp import encode_fp
from circuitlab.model.graph import CircuitError


_NAME = r"[A-Za-z_.$][A-Za-z0-9_.$]*"
_NUMBER = r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+)"
_LABEL = re.compile(rf"^({_NAME})\s*:\s*")
_EXPRESSION = re.compile(rf"([+-]?{_NUMBER}|{_NAME})(?:\s*([+-])\s*({_NUMBER}))?")
_REGISTER_ALIASES = {"sp": 13, "lr": 14, "pc": 15}
_CONDITIONS = {name: index for index, name in enumerate(
    ("eq", "ne", "cs", "cc", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le")
)}
_CONDITIONS.update(hs=2, lo=3)
_HINTS = {"nop": 0, "yield": 1, "wfe": 2, "wfi": 3, "sev": 4}
_BARRIERS = {"dsb": 0x8F4F, "dmb": 0x8F5F, "isb": 0x8F6F}
_WIDE_THREE_REGISTERS = {
    "sdiv": (0xFB90, 0xF0F0),
    "udiv": (0xFBB0, 0xF0F0),
    "qadd": (0xFA80, 0xF080),
    "qsub": (0xFA80, 0xF0A0),
    "qdadd": (0xFA80, 0xF090),
    "qdsub": (0xFA80, 0xF0B0),
    "sel": (0xFAA0, 0xF080),
    "sadd16": (0xFA90, 0xF000),
    "uadd8": (0xFA80, 0xF040),
}
_MEMORY = {
    # Narrow immediate and register encodings, wide immediate base, and access size.
    "str": (0x6000, 0x5000, 0xF8C0, 4),
    "ldr": (0x6800, 0x5800, 0xF8D0, 4),
    "strh": (0x8000, 0x5200, 0xF8A0, 2),
    "ldrh": (0x8800, 0x5A00, 0xF8B0, 2),
    "strb": (0x7000, 0x5400, 0xF880, 1),
    "ldrb": (0x7800, 0x5C00, 0xF890, 1),
    "ldrsb": (None, 0x5600, 0xF990, 1),
    "ldrsh": (None, 0x5E00, 0xF9B0, 2),
}


@dataclass(frozen=True)
class _Reference:
    """An encoding whose value depends on the final address of a label."""

    kind: str
    expression: str
    size: int
    field: int = 0


def _split_operands(text):
    if not text.strip():
        return ()
    operands = []
    start, closing = 0, ""
    for index, char in enumerate(text):
        if char in "[{":
            if closing:
                raise ValueError("Nested address or register lists are unsupported.")
            closing = "]" if char == "[" else "}"
        elif char in "]}":
            if char != closing:
                raise ValueError("Mismatched address or register-list brackets.")
            closing = ""
        elif char == "," and not closing:
            operands.append(text[start:index].strip())
            start = index + 1
    if closing:
        raise ValueError(f"Close the operand with {closing}.")
    operands.append(text[start:].strip())
    if not all(operands):
        raise ValueError("Expected an operand on each side of the comma.")
    return tuple(operands)


def _integer(text):
    if not re.fullmatch(rf"[+-]?{_NUMBER}", text):
        raise ValueError(f'Expected an integer, received "{text}".')
    unsigned = text.lstrip("+-").lower()
    base = 16 if unsigned.startswith("0x") else 2 if unsigned.startswith("0b") else 10
    return int(text, base)


def _immediate(text):
    if not text.startswith("#"):
        raise ValueError(f'Write an immediate operand as #number, received "{text}".')
    return _integer(text[1:].strip())


def _register(text):
    name = text.lower()
    if name in _REGISTER_ALIASES:
        return _REGISTER_ALIASES[name]
    if re.fullmatch(r"r(?:[0-9]|1[0-5])", name):
        return int(name[1:])
    raise ValueError(f'Expected a register r0–r15, received "{text}".')


def _bound(value, minimum, maximum, description, *, multiple=1):
    if not minimum <= value <= maximum or value % multiple:
        scale = f" a multiple of {multiple}" if multiple != 1 else ""
        raise ValueError(f"{description} must be{scale} in the range {minimum} through {maximum}.")
    return value // multiple


def _count(mnemonic, operands, *counts):
    if len(operands) not in counts:
        expected = " or ".join(map(str, counts))
        raise ValueError(f"{mnemonic} expects {expected} operands.")


def _no_pc(*registers):
    if 15 in registers:
        raise ValueError("PC is unavailable in this operand form.")


def _general(*registers):
    if any(reg in (13, 15) for reg in registers):
        raise ValueError("SP and PC are unavailable in this operand form.")


def _require_wide(width):
    if width == "n":
        raise ValueError("These operands require a wide Thumb encoding.")


def _expression(text, symbols, address):
    match = _EXPRESSION.fullmatch(text)
    if not match:
        raise ValueError(f'Expected an integer or label with an optional integer offset, received "{text}".')
    atom, operator, offset = match.groups()
    if atom == ".":
        value = address
    elif re.fullmatch(_NAME, atom):
        if atom not in symbols:
            raise ValueError(f'Unknown label "{atom}".')
        value = symbols[atom]
    else:
        value = _integer(atom)
    if operator:
        value += _integer(operator + offset)
    return value


def _mov(name, operands, width):
    _count(name, operands, 2)
    rd = _register(operands[0])
    flags = name == "movs"
    if operands[1].startswith("#"):
        value = _immediate(operands[1])
        if flags and rd < 8 and width != "w" and 0 <= value <= 255:
            return (0x2000 | rd << 8 | value,)
        _require_wide(width)
        _general(rd)
        if name == "movw" or (not flags and value > 255):
            _bound(value, 0, 65535, "MOVW immediate")
            return (0xF240 | ((value >> 11) & 1) << 10 | value >> 12,
                    ((value >> 8) & 7) << 12 | rd << 8 | value & 255)
        _bound(value, 0, 255, "MOV immediate")
        return (0xF05F if flags else 0xF04F, rd << 8 | value)
    if name == "movw":
        raise ValueError("MOVW expects an immediate as its second operand.")
    rm = _register(operands[1])
    if not flags and width != "w":
        return (0x4600 | (rd & 8) << 4 | rm << 3 | rd & 7,)
    if flags and max(rd, rm) < 8 and width != "w":
        return (rm << 3 | rd,)
    _require_wide(width)
    _general(rd, rm)
    return (0xEA5F if flags else 0xEA4F, rd << 8 | rm)


def _add_sub(name, operands, width):
    _count(name, operands, 2, 3)
    rd = _register(operands[0])
    rn = rd if len(operands) == 2 else _register(operands[1])
    rhs = operands[-1]
    subtract = name.startswith("sub")
    flags = name.endswith("s")
    _no_pc(rd, rn)
    if rhs.startswith("#"):
        value = _immediate(rhs)
        if flags and max(rd, rn) < 8 and width != "w":
            if len(operands) == 3 and 0 <= value <= 7:
                return ((0x1E00 if subtract else 0x1C00) | value << 6 | rn << 3 | rd,)
            if rd == rn and 0 <= value <= 255:
                return ((0x3800 if subtract else 0x3000) | rd << 8 | value,)
        if not flags and rd == rn == 13 and width != "w" and name in ("add", "sub"):
            if 0 <= value <= 508 and value % 4 == 0:
                return ((0xB080 if subtract else 0xB000) | value // 4,)
        _require_wide(width)
        if flags:
            _general(rd, rn)
            _bound(value, 0, 255, f"{name} immediate")
            return ((0xF1B0 if subtract else 0xF110) | rn, rd << 8 | value)
        _bound(value, 0, 4095, f"{name} immediate")
        if rd == 13 and rn != 13:
            raise ValueError("Writing SP requires SP as the first source.")
        return ((0xF2A0 if subtract else 0xF200) | ((value >> 11) & 1) << 10 | rn,
                ((value >> 8) & 7) << 12 | rd << 8 | value & 255)
    if name.endswith("w"):
        raise ValueError(f"{name} expects an immediate as its last operand.")
    rm = _register(rhs)
    _no_pc(rm)
    if flags and max(rd, rn, rm) < 8 and width != "w":
        return ((0x1A00 if subtract else 0x1800) | rm << 6 | rn << 3 | rd,)
    if not flags and not subtract and rd == rn and width != "w":
        return (0x4400 | (rd & 8) << 4 | rm << 3 | rd & 7,)
    _require_wide(width)
    _general(rd, rn, rm)
    return ((0xEBA0 if subtract else 0xEB00) | (16 if flags else 0) | rn, rd << 8 | rm)


def _compare(operands, width):
    _count("cmp", operands, 2)
    rn = _register(operands[0])
    _no_pc(rn)
    if operands[1].startswith("#"):
        value = _bound(_immediate(operands[1]), 0, 255, "CMP immediate")
        if rn < 8 and width != "w":
            return (0x2800 | rn << 8 | value,)
        _require_wide(width)
        return (0xF1B0 | rn, 0x0F00 | value)
    rm = _register(operands[1])
    _no_pc(rm)
    if width == "w":
        return (0xEBB0 | rn, 0x0F00 | rm)
    if max(rn, rm) < 8:
        return (0x4280 | rm << 3 | rn,)
    return (0x4500 | (rn & 8) << 4 | rm << 3 | rn & 7,)


def _literal(rt, offset, size):
    if size == 2:
        scaled = _bound(offset, 0, 1020, "Narrow PC-relative offset", multiple=4)
        return (0x4800 | rt << 8 | scaled,)
    _bound(offset, -4095, 4095, "Wide PC-relative offset", multiple=4)
    return (0xF8DF if offset >= 0 else 0xF85F, rt << 12 | abs(offset))


def _load_store(name, operands, width):
    _count(name, operands, 2)
    rt = _register(operands[0])
    if name.startswith("str"):
        _no_pc(rt)
    narrow, narrow_reg, wide_base, scale = _MEMORY[name]
    address = operands[1]
    if not address.startswith("["):
        if name != "ldr":
            raise ValueError("Only LDR supports a bare literal label here.")
        if rt >= 8:
            _require_wide(width)
        return _Reference("literal", address, 4 if width == "w" or rt >= 8 else 2, rt)
    if not address.endswith("]"):
        raise ValueError("Write the address as [register], [register, #offset], or [register, register].")
    parts = _split_operands(address[1:-1])
    _count("Address", parts, 1, 2)
    rn = _register(parts[0])
    if len(parts) == 1 or parts[1].startswith("#"):
        offset = _immediate(parts[1]) if len(parts) == 2 else 0
        if rn == 15:
            if name != "ldr":
                raise ValueError("Only LDR supports a PC-relative address here.")
            size = 4 if width == "w" or rt >= 8 or not 0 <= offset <= 1020 else 2
            if size == 4:
                _require_wide(width)
            return _literal(rt, offset, size)
        if width != "w" and narrow is not None and rt < 8 and offset % scale == 0:
            if rn < 8 and 0 <= offset <= 31 * scale:
                return (narrow | (offset // scale) << 6 | rn << 3 | rt,)
            if rn == 13 and scale == 4 and 0 <= offset <= 1020:
                return ((0x9800 if name == "ldr" else 0x9000) | rt << 8 | offset // 4,)
        _require_wide(width)
        if scale != 4:
            _general(rt)
        if offset < 0:
            _bound(offset, -255, -1, "Negative memory offset")
            return ((wide_base - 0x80) | rn, rt << 12 | 0x0C00 | abs(offset))
        _bound(offset, 0, 4095, "Memory offset")
        return (wide_base | rn, rt << 12 | offset)
    _no_pc(rn)
    rm = _register(parts[1])
    if width != "w" and max(rt, rn, rm) < 8:
        return (narrow_reg | rm << 6 | rn << 3 | rt,)
    _require_wide(width)
    _general(rm)
    if scale != 4:
        _general(rt)
    return ((wide_base - 0x80) | rn, rt << 12 | rm)


def _register_list(text):
    if not text.startswith("{") or not text.endswith("}"):
        raise ValueError("Write the register list in braces.")
    registers = []
    for part in _split_operands(text[1:-1]):
        first, dash, last = part.partition("-")
        low = _register(first.strip())
        high = _register(last.strip()) if dash else low
        if high < low:
            raise ValueError("A register range must increase.")
        registers.extend(range(low, high + 1))
    if not registers or len(set(registers)) != len(registers):
        raise ValueError("Provide a nonempty register list without duplicates.")
    return registers


def _branch(offset, reference):
    if reference.size == 2:
        bits = 11 if reference.field == 14 else 8
        halfwords = _bound(offset, -(1 << bits), (1 << bits) - 2, "Branch displacement", multiple=2)
        base = 0xE000 if bits == 11 else 0xD000 | reference.field << 8
        return (base | halfwords & ((1 << bits) - 1),)
    if reference.field < 14:
        _bound(offset, -(1 << 20), (1 << 20) - 2, "Wide conditional branch displacement", multiple=2)
        first = 0xF000 | ((offset >> 20) & 1) << 10 | reference.field << 6 | (offset >> 12) & 63
        second = 0x8000 | ((offset >> 18) & 1) << 13 | ((offset >> 19) & 1) << 11 | (offset >> 1) & 2047
        return first, second
    _bound(offset, -(1 << 24), (1 << 24) - 2, "Wide branch displacement", multiple=2)
    s = (offset >> 24) & 1
    j1 = 1 ^ s ^ ((offset >> 23) & 1)
    j2 = 1 ^ s ^ ((offset >> 22) & 1)
    first = 0xF000 | s << 10 | (offset >> 12) & 1023
    second = (0xD000 if reference.kind == "call" else 0x9000) | j1 << 13 | j2 << 11 | (offset >> 1) & 2047
    return first, second


def _instruction(text):
    pieces = text.split(maxsplit=1)
    mnemonic = pieces[0].lower()
    operands = _split_operands(pieces[1] if len(pieces) == 2 else "")
    fp = encode_fp(mnemonic, operands)
    if fp is not None:
        return fp
    name, dot, width = mnemonic.rpartition(".")
    if not dot or width not in ("n", "w"):
        name, width = mnemonic, ""
    if name in _HINTS:
        _count(mnemonic, operands, 0)
        return (0xF3AF, 0x8000 | _HINTS[name]) if width == "w" else (0xBF00 | _HINTS[name] << 4,)
    if name in _BARRIERS:
        _require_wide(width)
        _count(mnemonic, operands, 0, 1)
        if operands and operands[0].lower() != "sy":
            raise ValueError(f"{name} supports only the SY barrier option.")
        return 0xF3BF, _BARRIERS[name]
    if name in _WIDE_THREE_REGISTERS:
        _require_wide(width)
        _count(mnemonic, operands, 3)
        rd, rn, rm = map(_register, operands)
        _general(rd, rn, rm)
        first, second = _WIDE_THREE_REGISTERS[name]
        # Scalar saturating operations place their second source first in the encoding.
        if name.startswith("q"):
            rn, rm = rm, rn
        return first | rn, second | rd << 8 | rm
    if name in ("mov", "movs", "movw"):
        return _mov(name, operands, width)
    if name in ("add", "adds", "addw", "sub", "subs", "subw"):
        return _add_sub(name, operands, width)
    if name == "cmp":
        return _compare(operands, width)
    if name in _MEMORY:
        return _load_store(name, operands, width)
    if name in ("bx", "blx"):
        _count(mnemonic, operands, 1)
        if width == "w":
            raise ValueError(f"{name} register form has only a narrow encoding.")
        rm = _register(operands[0])
        _no_pc(rm)
        return ((0x4780 if name == "blx" else 0x4700) | rm << 3,)
    if name in ("push", "pop"):
        _count(mnemonic, operands, 1)
        registers = _register_list(operands[0])
        extra = 14 if name == "push" else 15
        if any(reg > 7 and reg != extra for reg in registers) or width == "w":
            raise ValueError(f"Use the narrow {name} form with r0–r7 and optionally {'lr' if extra == 14 else 'pc'}.")
        mask = sum(1 << (8 if reg == extra else reg) for reg in registers)
        return ((0xB400 if name == "push" else 0xBC00) | mask,)
    if name in ("svc", "udf") and width != "w":
        _count(mnemonic, operands, 1)
        value = _bound(_immediate(operands[0]), 0, 255, f"{name} immediate")
        return ((0xDF00 if name == "svc" else 0xDE00) | value,)
    if name in ("b", "bal", "bl") or (name.startswith("b") and name[1:] in _CONDITIONS):
        _count(mnemonic, operands, 1)
        if name == "bl":
            _require_wide(width)
        return _Reference("call" if name == "bl" else "branch", operands[0],
                          4 if width == "w" or name == "bl" else 2, _CONDITIONS.get(name[1:], 14))
    raise ValueError(f'Unsupported Thumb instruction "{mnemonic}".')


def _emit(payload, symbols, address):
    if isinstance(payload, _Reference):
        target = _expression(payload.expression, symbols, address)
        if payload.kind == "data":
            width = payload.size * 8
            _bound(target, -(1 << (width - 1)), (1 << width) - 1, "Data value")
            return (target & ((1 << width) - 1)).to_bytes(payload.size, "little")
        if payload.kind == "literal":
            payload = _literal(payload.field, target - ((address + 4) & ~3), payload.size)
        else:
            payload = _branch(target - (address + 4), payload)
    return b"".join(halfword.to_bytes(2, "little") for halfword in payload)


def assemble_thumb(
    rows: Iterable[tuple[str, int]], *, max_bytes: int = 1 << 32
) -> dict[int, int]:
    """Return little-endian ROM words indexed by aligned byte address.

    Assembly starts at byte address zero. A label may prefix another label,
    instruction, or directive. .align is a power-of-two alignment, .balign is
    byte alignment, and .org advances to an absolute byte address.
    Unlisted words are zero. The final word preserves the image length.
    """
    max_bytes = min(max_bytes, 1 << 32)
    symbols, statements = {}, []
    address = 0
    data_sizes = {".byte": 1, ".hword": 2, ".short": 2, ".word": 4}
    for source, line in rows:
        text = source.partition("//")[0].strip().removesuffix(";").strip()
        try:
            while label := _LABEL.match(text):
                name = label[1]
                if name == "." or name in symbols:
                    raise ValueError(f'Invalid or duplicate label "{name}".')
                if address >= 1 << 32:
                    raise ValueError("A label must have a 32-bit byte address.")
                symbols[name] = address
                text = text[label.end():]
            if not text:
                continue
            if text.startswith("."):
                parts = text.split(maxsplit=1)
                directive = parts[0].lower()
                arguments = _split_operands(parts[1] if len(parts) == 2 else "")
                if directive in data_sizes:
                    if not arguments:
                        raise ValueError(f"{directive} expects at least one value.")
                    size = data_sizes[directive]
                    for argument in arguments:
                        statements.append((address, line, _Reference("data", argument, size)))
                        address += size
                elif directive in (".align", ".balign", ".org"):
                    _count(directive, arguments, 1)
                    value = _integer(arguments[0])
                    if directive == ".org":
                        address = _bound(value, address, min(max_bytes, (1 << 32) - 1), "New ROM address")
                    else:
                        if directive == ".align":
                            value = 1 << _bound(value, 0, max_bytes.bit_length() - 1, "Alignment exponent")
                        _bound(value, 1, max_bytes, "Alignment")
                        address += (-address) % value
                else:
                    raise ValueError(f'Unknown Arm directive "{directive}".')
            else:
                if address % 2:
                    raise ValueError("Thumb instructions must start at an even byte address.")
                payload = _instruction(text)
                statements.append((address, line, payload))
                address += payload.size if isinstance(payload, _Reference) else len(payload) * 2
            if address > max_bytes:
                raise ValueError(f"Arm ROM exceeds its {max_bytes}-byte capacity.")
        except ValueError as error:
            raise CircuitError(str(error), line) from error

    words = {}
    for start, line, payload in statements:
        try:
            code = _emit(payload, symbols, start)
        except ValueError as error:
            raise CircuitError(str(error), line) from error
        for byte_address, value in enumerate(code, start):
            if value:
                word_address = byte_address & ~3
                words[word_address] = words.get(word_address, 0) | value << (8 * (byte_address & 3))
    if address:
        words.setdefault((address - 1) & ~3, 0)
    return words
