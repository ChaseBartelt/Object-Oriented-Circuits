"""Expand local, named chip-call argument lists without creating hardware state."""

from dataclasses import dataclass
import re

from circuitlab.language.pin_banks import MAX_PIN_INDEX, expand_pin_banks, pin_bank_names
from circuitlab.model.graph import CircuitError


NAME = r"[A-Za-z_][A-Za-z0-9_]*"
DECIMAL = re.compile(r"[0-9]+(?:_[0-9]+)*")
QUOTED_ASSIGNMENT = re.compile(rf'({NAME})(?:\s*\[([^]]*)\])?\s*=(?!=)\s*(".*)', re.S)
REFERENCE = re.compile(rf"({NAME})(?:\s*\[([^]]*)\])?")
CALL = re.compile(rf"({NAME})\s*\(([^()]*)\)")
DECLARATION = re.compile(r"(.+?)\s*=\s*(input|output|wire)", re.I)
STORED_OR_ASSIGNED = re.compile(rf"(?:const\s+)?({NAME})(?:\s*\[[^]]*\])*\s*(?:=(?!=)|$)")
IDENTIFIER = re.compile(rf"\b{NAME}\b")


@dataclass(frozen=True)
class _ArgumentList:
    high: int
    low: int
    members: tuple[str, ...]

    def select(self, selector, row):
        if selector is None:
            return self.members
        bounds = _indices(selector, row)
        high, low = (bounds[0], bounds[0]) if len(bounds) == 1 else bounds
        if high > self.high or low < self.low:
            raise _error(row, f"Argument-list selection must stay within [{self.high}:{self.low}].")
        return self.members[self.high - high:self.high - low + 1]


def _error(row, message):
    origin = row[2] if len(row) > 2 else None
    return CircuitError(message, row[1], path=getattr(origin, "path", None))


def _indices(raw, row):
    parts = [part.strip() for part in raw.split(":")]
    if len(parts) not in (1, 2) or any(not DECIMAL.fullmatch(part) for part in parts):
        raise _error(row, "Use nonnegative decimal indices in an argument-list selection.")
    values = []
    for part in parts:
        digits = part.replace("_", "").lstrip("0") or "0"
        if len(digits) > 4 or int(digits) > MAX_PIN_INDEX:
            raise _error(row, f"Argument-list indices must be between 0 and {MAX_PIN_INDEX}.")
        values.append(int(digits))
    if len(values) == 2 and values[0] < values[1]:
        raise _error(row, "Write argument-list ranges in descending order: name[high:low].")
    return values


def expand_argument_lists(rows, pins=()):
    """Resolve name[high:low] = \"a, b, ...\" in one chip or main scope.

    Highest declared index names the first member. Definitions disappear;
    expanded calls retain their original line and optional binding fields.
    This pass runs after loop expansion and before signal/array lowering.
    """
    rows = list(rows)
    aliases, declarations, real_names = {}, set(), set(pins)
    depth = 0
    for position, row in enumerate(rows):
        text, line = row[:2]
        match = QUOTED_ASSIGNMENT.fullmatch(text)
        if match:
            if depth:
                raise _error(row, "Declare argument lists directly inside a chip or main.")
            name, raw_range, quoted = match.groups()
            if name in aliases:
                raise _error(row, f'Argument list "{name}" is already declared in this scope.')
            if raw_range is None or ":" not in raw_range:
                raise _error(row, 'Declare an argument list with name[high:low] = "a, b, ...".')
            high, low = _indices(raw_range, row)
            if not re.fullmatch(r'"[^"\\]*"', quoted, re.S):
                raise _error(row, "Use one quoted, comma-separated list of signal names without escapes.")
            raw_members = quoted[1:-1].strip()
            members = tuple(item.strip() for item in raw_members.split(",")) if raw_members else ()
            expected = high - low + 1
            if len(members) != expected:
                raise _error(row, f'Argument list "{name}" declares {expected} members but contains {len(members)}.')
            if any(not re.fullmatch(NAME, member) for member in members):
                raise _error(row, "Argument-list members must be plain signal names separated by commas.")
            aliases[name] = _ArgumentList(high, low, members), row
            declarations.add(position)
        elif depth == 0:
            if declaration := DECLARATION.fullmatch(text):
                expanded = expand_pin_banks(declaration[1], line)
                real_names.update(item.strip().partition("[")[0].rstrip() for item in expanded.split(","))
            elif assignment := STORED_OR_ASSIGNED.match(text):
                real_names.add(assignment[1])
        depth += int(text.endswith("{")) - int(text == "}")

    if not aliases:
        return rows
    reserved = real_names | pin_bank_names(real_names)
    for name, (alias, row) in aliases.items():
        if name in reserved:
            raise _error(row, f'Argument list "{name}" conflicts with a signal or numbered pin bank.')
        for member in alias.members:
            if member not in real_names:
                raise _error(row, f'Argument list "{name}" contains undeclared local signal "{member}".')

    result, depth = [], 0
    for position, row in enumerate(rows):
        if position in declarations:
            continue
        text = row[0]
        call = CALL.fullmatch(text) if depth == 0 else None
        if call:
            arguments = []
            for argument in call[2].split(",") if call[2].strip() else ():
                selection = REFERENCE.fullmatch(argument.strip())
                if selection and selection[1] in aliases:
                    arguments.extend(aliases[selection[1]][0].select(selection[2], row))
                else:
                    arguments.append(argument.strip())
            text = f"{call[1]}({', '.join(arguments)})"
        elif any(token[0] in aliases for token in IDENTIFIER.finditer(text)):
            raise _error(row, "Argument lists may only be used as chip-call arguments.")
        result.append((text, *row[1:]))
        depth += int(text.endswith("{")) - int(text == "}")
    return result
