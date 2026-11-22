"""Lower clocked clear groups to ordinary ordered zero assignments."""

import re

from circuitlab.language.control_flow import MAX_EXPANDED_ROWS
from circuitlab.language.digital_arrays import DECLARATION as ARRAY_DECLARATION, MAX_ARRAY_WORDS, _brackets
from circuitlab.model.graph import CircuitError


CLEAR = re.compile(r"clear\s*\{")
CLOCK = re.compile(r"clock\s*\(")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _error(row, message):
    origin = row[2] if len(row) > 2 else None
    return CircuitError(message, row[1], path=getattr(origin, "path", None))


def _target(text, row):
    name = NAME.match(text)
    if name is None:
        raise _error(row, "List state names and optional word/bit/slice selections inside clear blocks.")
    index, selectors = name.end(), 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text[index] != "[" or selectors >= 2:
            raise _error(row, "Use a state name, array word, or bit/slice target in a clear block.")
        try:
            _, index = _brackets(text, index, row[1])
        except CircuitError as error:
            raise _error(row, error.message) from None
        selectors += 1
    return name[0], selectors


def expand_clear_blocks(rows):
    """Expand clear { a, b; words; } only inside existing clocked behavior.

    Whole arrays clear every word. All other targets retain the assignment
    compiler's ordinary destination/width checks and pre-edge write semantics.
    """
    from circuitlab.exporters.verilog import _unsigned_decimal

    rows = list(rows)
    if not any(CLEAR.fullmatch(row[0]) for row in rows):
        return rows

    arrays, depth = {}, 0
    for row in rows:
        text, line = row[:2]
        if depth == 0 and (declaration := ARRAY_DECLARATION.fullmatch(text)):
            try:
                count = _unsigned_decimal(declaration[2].strip(), line, "array word count")
            except CircuitError as error:
                raise _error(row, error.message) from None
            if not 1 <= count <= MAX_ARRAY_WORDS:
                raise _error(row, f"Array word count must be between 1 and {MAX_ARRAY_WORDS}.")
            arrays[declaration[1]] = count
        depth += int(text.endswith("{")) - int(text == "}")

    output, containers, index = [], [], 0

    def append(row):
        if len(output) >= MAX_EXPANDED_ROWS:
            raise _error(row, f"Clear expansion exceeds {MAX_EXPANDED_ROWS} source rows.")
        output.append(row)

    while index < len(rows):
        row = rows[index]
        text = row[0]
        if CLEAR.fullmatch(text):
            if not any(CLOCK.match(container) for container in containers):
                raise _error(row, "Put clear blocks inside a clock block.")
            opening, targets = row, 0
            index += 1
            while index < len(rows) and rows[index][0] != "}":
                member = rows[index]
                for raw in member[0].split(","):
                    target = raw.strip()
                    name, selectors = _target(target, member)
                    if name in arrays and not selectors:
                        for word in range(arrays[name]):
                            append((f"{name}[{word}] = 0", *member[1:]))
                    else:
                        append((f"{target} = 0", *member[1:]))
                    targets += 1
                index += 1
            if index == len(rows):
                raise _error(opening, "Close the clear block with }.")
            if targets == 0:
                raise _error(opening, "List at least one state target inside a clear block.")
        else:
            append(row)
            if text.endswith("{"):
                containers.append(text)
            elif text == "}" and containers:
                containers.pop()
        index += 1
    return output
