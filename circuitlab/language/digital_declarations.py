"""Lower sized signal expressions to ordinary combinational wire declarations."""
# A width-bearing declaration can describe a combinational expression or stored initial state.
# Normalize only declarations proven to be wires; indexed assignments and clocked updates retain
# their original meaning and source location.

import re

from circuitlab.language.pin_banks import expand_pin_banks, pin_bank_names
from circuitlab.model.graph import CircuitError


NAME = r"[A-Za-z_][A-Za-z0-9_]*"
# Candidate shorthand: new_name[width]=expression. Exclude a colon from
# the bracket contents so a slice assignment is never a width declaration;
# =(?!=) also keeps equality syntax from matching an assignment.
INLINE_WIRE = re.compile(rf"({NAME})\s*\[\s*([^\]:]+)\s*\]\s*=(?!=)\s*(.+)")
# Explicit input/output/wire declarations reserve their names before any
# inference. STATE recognizes bare state and initializer-shaped rows,
# whose right side still needs classification before its name is reserved.
DECLARATION = re.compile(r"(.+?)\s*=\s*(input|output|wire)", re.I)
STATE = re.compile(rf"({NAME})(?:\s*\[[^]]*\])?\s*(?:=(?!=)\s*(.*))?")
# Preserve numeric state initializers, including diagnostics for invalid ones.
# This is intentionally a broad classifier, not an integer validator.
# It includes digit separators and numeric-looking text such as 1.5 or
# 0xff, leaving unsupported literal forms for the normal state diagnostics
# instead of changing them into combinational wire expressions.
NUMERIC_INITIALIZER = re.compile(r"[+-]?[0-9][A-Za-z0-9_.]*")
PORT_BLOCK = re.compile(rf"(input|output|wire)(?:\s+({NAME}))?\s*\{{", re.I)
PORT_LIST = re.compile(rf"{NAME}(?:\s*\[[^]\n]+\])?(?:\s*,\s*{NAME}(?:\s*\[[^]\n]+\])?)*")


def normalize_port_blocks(rows):
    """Lower signal declaration groups after static loops expand their members.

    Keep the original row metadata so declaration errors still name the member's
    source line. Runtime blocks cannot declare ports, matching ordinary syntax.
    """
    result, direction, depth = [], None, 0
    opening_line = None
    for row in rows:
        text, line = row[:2]
        if header := PORT_BLOCK.fullmatch(text):
            if direction is not None or depth:
                raise CircuitError("Put input/output/wire blocks directly inside a chip or main.", line)
            direction, opening_line = header[1].lower(), line
        elif direction is not None:
            if text == "}":
                direction = None
            elif not PORT_LIST.fullmatch(text):
                raise CircuitError(
                    "List signal names and optional widths inside input/output/wire blocks; "
                    "put assignments outside them.", line,
                )
            else:
                result.append((f"{text} = {direction}", *row[1:]))
        else:
            result.append(row)
            depth += int(text.endswith("{")) - int(text == "}")
    if direction is not None:
        raise CircuitError(f"Close the {direction} block with }}.", opening_line)
    return result


def normalize_wire_declarations(rows, pins=()):
    """Lower new name[width]=expression; preserve literals and indexed writes.

    Rows retain their line and optional origin fields. Only declarations at the
    chip's top level are inferred; clocked/conditional writes stay untouched.
    """
    # Two passes need a stable sequence: first discover existing declarations,
    # then rewrite candidates. rows may also contain origin/binding metadata
    # after the first two fields, which generated rows must retain.
    rows = list(rows)
    # Header pins already count as known even before their declaration rows.
    # Remember top-level indexes in the original sequence, independently of
    # the extra rows that the second pass will insert.
    known, top_level, depth = set(pins), set(), 0
    # Pass 1 scans the entire body, so a declaration appearing later can still
    # protect an earlier name[index]=... row from being misread as a new wire.
    for index, row in enumerate(rows):
        text, line = row[:2]
        if depth == 0:
            top_level.add(index)
            # Expand a numbered pin bank such as SW[3:0] into SW3..SW0, while leaving
            # a real vector such as data[4] intact. Collect each declared base name;
            # the original declaration row itself is not rewritten in this pass.
            if declaration := DECLARATION.fullmatch(text):
                for item in expand_pin_banks(declaration[1], line).split(","):
                    if name := re.match(NAME, item.strip()):
                        known.add(name[0])
            else:
                # Reserve constants regardless of initializer text, bare state names, and
                # numeric-initialized state. A new sized expression such as next[8]=a+1
                # does not reserve itself here and can therefore be inferred in pass 2.
                state = STATE.fullmatch(text.removeprefix("const "))
                if state and (text.startswith("const ") or state[2] is None
                              or NUMERIC_INITIALIZER.fullmatch(state[2].strip())):
                    known.add(state[1])
        # Callers have already split statements/braces into logical rows. Count
        # those structural boundaries so clocked/conditional body writes cannot
        # silently declare combinational signals, even when their names are new.
        depth += int(text.endswith("{")) - int(text == "}")

    # Names ending in digits imply possible bank prefixes. If SW0 exists,
    # protect SW[...] as a selector spelling rather than inventing a vector SW.
    # Even an invalid bank index must reach the selector validator unchanged.
    banks = pin_bank_names(known)
    result = []
    # Pass 2 considers only top-level sized assignments to names that remain
    # unknown. Existing names, bank prefixes, explicit declaration keywords,
    # and numeric-looking state initializers all keep their original rows.
    for index, row in enumerate(rows):
        match = INLINE_WIRE.fullmatch(row[0]) if index in top_level else None
        if (match and match[1] not in known | banks
                and match[3].strip().lower() not in {"input", "output", "wire"}
                and not NUMERIC_INITIALIZER.fullmatch(match[3].strip())):
            # For example, next[8]=a+1 becomes next[8]=wire followed by next=a+1.
            # Only the outer width whitespace is stripped. Expression/width validity,
            # dependencies, and combinational feedback remain for later compiler passes.
            name, width, expression = match.groups()
            # Emit the ordinary declaration and assignment syntax understood downstream.
            # Copy every trailing row field to both statements, so either diagnostic
            # still points to the shorthand's original source line and file binding.
            result.extend(((f"{name}[{width.strip()}] = wire", *row[1:]),
                           (f"{name} = {expression}", *row[1:])))
            # Immediately reserve this inferred name. A later row targeting it is an
            # assignment to an existing signal, not a second implicit declaration;
            # update possible bank prefixes too when the new name ends in digits.
            known.add(name)
            banks.update(pin_bank_names((name,)))
        # Forward all other rows unchanged. The generated explicit wire declaration
        # will itself be recognized as known if a later stage normalizes again.
        else:
            result.append(row)
    return result
