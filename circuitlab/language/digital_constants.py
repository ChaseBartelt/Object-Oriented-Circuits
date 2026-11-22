"""Lower immutable chip-local integer names to existing sized constants."""

import re

from circuitlab.model.graph import CircuitError


INTEGER_START = re.compile(r"int\s+(?=[A-Za-z_])")
INTEGER = re.compile(r"int\s+([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.+)")


def normalize_integer_constants(rows):
    """Name an unsigned decimal literal without changing its expression width.

    Main's compile-time int variables and for-loop header variables use their
    existing elaborators. This pass applies only to chip body declarations.
    """
    from circuitlab.exporters.verilog import MAX_STATE_WIDTH, _name, _unsigned_decimal

    result, depth = [], 0
    for row in rows:
        text, line = row[:2]
        if INTEGER_START.match(text):
            origin = row[2] if len(row) > 2 else None
            path = getattr(origin, "path", None)
            if depth:
                raise CircuitError("Declare immutable int constants directly inside a chip.", line, path=path)
            declaration = INTEGER.fullmatch(text)
            if declaration is None:
                raise CircuitError("Use int name = nonnegative_decimal for a chip-local constant.", line, path=path)
            name, raw = declaration.groups()
            try:
                _name(name, line)
                value = _unsigned_decimal(raw.strip(), line, "int constant initializer")
            except CircuitError as error:
                raise CircuitError(error.message, line, path=path) from None
            width = max(1, value.bit_length())
            if width > MAX_STATE_WIDTH:
                raise CircuitError(f"An int constant may contain at most {MAX_STATE_WIDTH} bits.", line, path=path)
            text = f"const {name}[{width}] = {raw.strip()}"
        result.append((text, *row[1:]))
        depth += int(text.endswith("{")) - int(text == "}")
    return result
