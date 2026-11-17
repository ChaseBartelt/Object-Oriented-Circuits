"""Lower sized signal expressions to ordinary combinational wire declarations."""

import re

from circuitlab.language.pin_banks import expand_pin_banks, pin_bank_names


NAME = r"[A-Za-z_][A-Za-z0-9_]*"
INLINE_WIRE = re.compile(rf"({NAME})\s*\[\s*([^\]:]+)\s*\]\s*=(?!=)\s*(.+)")
DECLARATION = re.compile(r"(.+?)\s*=\s*(input|output|wire)", re.I)
STATE = re.compile(rf"({NAME})(?:\s*\[[^]]*\])?\s*(?:=(?!=)\s*(.*))?")
# Preserve numeric state initializers, including diagnostics for invalid ones.
NUMERIC_INITIALIZER = re.compile(r"[+-]?[0-9][A-Za-z0-9_.]*")


def normalize_wire_declarations(rows, pins=()):
    """Lower new name[width]=expression; preserve literals and indexed writes.

    Rows retain their line and optional origin fields. Only declarations at the
    chip's top level are inferred; clocked/conditional writes stay untouched.
    """
    rows = list(rows)
    known, top_level, depth = set(pins), set(), 0
    for index, row in enumerate(rows):
        text, line = row[:2]
        if depth == 0:
            top_level.add(index)
            if declaration := DECLARATION.fullmatch(text):
                for item in expand_pin_banks(declaration[1], line).split(","):
                    if name := re.match(NAME, item.strip()):
                        known.add(name[0])
            else:
                state = STATE.fullmatch(text.removeprefix("const "))
                if state and (text.startswith("const ") or state[2] is None
                              or NUMERIC_INITIALIZER.fullmatch(state[2].strip())):
                    known.add(state[1])
        depth += int(text.endswith("{")) - int(text == "}")

    banks = pin_bank_names(known)
    result = []
    for index, row in enumerate(rows):
        match = INLINE_WIRE.fullmatch(row[0]) if index in top_level else None
        if (match and match[1] not in known | banks
                and match[3].strip().lower() not in {"input", "output", "wire"}
                and not NUMERIC_INITIALIZER.fullmatch(match[3].strip())):
            name, width, expression = match.groups()
            result.extend(((f"{name}[{width.strip()}] = wire", *row[1:]),
                           (f"{name} = {expression}", *row[1:])))
            known.add(name)
            banks.update(pin_bank_names((name,)))
        else:
            result.append(row)
    return result
