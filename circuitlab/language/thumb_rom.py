"""Lower inline Thumb firmware into the existing digital ROM representation."""

import re

from circuitlab.model.graph import CircuitError


_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
ARM_ROM = re.compile(
    rf"\barm\s*\(\s*({_NAME})\s*,\s*({_NAME})(?:\s*,\s*({_NAME}))?\s*\)\s*\{{",
    re.I,
)


def lower_thumb_roms(rows):
    """Turn chip-level arm(address, word[, valid]) blocks into ROM drivers."""
    result, pending, depth = [], iter(rows), 0
    for row in pending:
        text, line = row[:2]
        match = ARM_ROM.fullmatch(text)
        if not match:
            depth += int(text.endswith("{")) - int(text == "}")
            result.append(row)
            continue
        if depth:
            raise CircuitError("Put the arm ROM at chip level.", line)

        from circuitlab.language.thumb_assembler import assemble_thumb

        body = []
        for instruction in pending:
            if instruction[0] == "}":
                break
            body.append(instruction[:2])
        else:
            raise CircuitError("Close the arm ROM with }.", line)
        words = assemble_thumb(body)
        address, word, valid = match.groups()
        origin = row[1:]
        if valid:
            check = f"(({address} & 3) == 0) && ({address} <= {max(words)})" if words else "0"
            result.append((f"{valid} = {check}", *origin))
        result.append((f"switch ({address}) {{", *origin))
        for offset, value in words.items():
            if value:
                result.extend(((f"case {offset}:", *origin), (f"{word} = {value}", *origin), ("break", *origin)))
        result.extend((("default:", *origin), (f"{word} = 0", *origin), ("}", *origin)))
    return result
