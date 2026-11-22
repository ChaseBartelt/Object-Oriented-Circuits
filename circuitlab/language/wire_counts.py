"""Compile main/method-local wire counters in electrical connection fields.

Counter references are source-generation syntax. They become ordinary named
nets before argument binding, graph construction, simulation, or drawing.
Nothing in this module interprets multiplication inside numerical expressions.
"""

import re

from circuitlab.model.graph import CircuitError


COUNT_DECLARATION = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*count", re.I)
COUNT_REFERENCE = re.compile(
    r"(?P<prefix>[A-Za-z_][A-Za-z0-9_.-]*)\s*(?P<operator>[*^$])"
    r"\s*(?P<counter>[A-Za-z_][A-Za-z0-9_]*)?"
)
_INTERPOLATION = re.compile(r"[$*]\{[^{}]*\}")
_MAX_COUNT = (1 << 63) - 1


def is_count_reference(text):
    """Recognize a complete token for tolerant tooling without evaluating it."""
    match = COUNT_REFERENCE.fullmatch(text.strip())
    return bool(match and (match["counter"] or match["operator"] in "*^"))


def _arguments(text, start, end):
    """Yield comma-separated argument spans, descending into grouped pin lists."""
    cursor, depth, offset = start, 0, start
    # Interpolation can contain its own parentheses. Mask it without changing
    # offsets so its expression punctuation is never a pin-list separator.
    masked = text[:start] + _INTERPOLATION.sub(lambda m: " " * len(m[0]), text[start:end]) + text[end:]
    spans = []
    for offset in range(start, end):
        char = masked[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and not depth:
            spans.append((cursor, offset))
            cursor = offset + 1
    spans.append((cursor, end))
    for left, right in spans:
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right and text[left] == "(" and text[right - 1] == ")":
            yield from _arguments(text, left + 1, right - 1)
        else:
            yield left, right


def wire_fields(text):
    """Find connection spans, leaving component values and equations opaque."""
    from circuitlab.language.syntax import (
        CALL, INLINE, PART_PLACEMENT, PLACEMENT, SOURCE_WIRE, TRANS_POSITIONAL, SECTION,
        SWITCH_CALL, TRANS_CALL,
    )

    # Generated identifiers such as R${n} must still match a placement before
    # execution. Preserve offsets so all returned spans address original text.
    shape = _INTERPOLATION.sub(lambda m: "_" * len(m[0]), text)
    placement = PLACEMENT.fullmatch(shape) or PART_PLACEMENT.fullmatch(shape)
    if placement:
        group = 3 if placement.re is PLACEMENT else 2
        return tuple(_arguments(text, *placement.span(group)))
    if source := SOURCE_WIRE.fullmatch(shape):
        return (source.span(3),)
    if transistor := TRANS_POSITIONAL.fullmatch(shape):
        return tuple(_arguments(text, *transistor.span(2)))[:3]
    if group := INLINE.fullmatch(shape):
        return tuple(_arguments(text, *group.span(2))) if group[2] is not None else ()
    if section := SECTION.fullmatch(shape):
        if section[1].casefold() in {"series", "parallel"} and section[3] is not None:
            return tuple(_arguments(text, *section.span(3)))
    if call := CALL.fullmatch(shape):
        arguments = tuple(_arguments(text, *call.span(2)))
        if SWITCH_CALL.fullmatch(call[1]):
            return arguments[:4]
        if TRANS_CALL.fullmatch(call[1]):
            return arguments[:3]
        return arguments
    # Explicit group/device contacts and the legacy op-amp marker's contacts.
    if field := re.fullmatch(r"(?:wire|ground|base|collector|emitter|in\+|in-|out)\s*=\s*(.+)", shape, re.I):
        return (field.span(1),)
    if returned := re.fullmatch(r"return\s+(.+)", shape, re.I):
        return (returned.span(1),)
    if measure := re.fullmatch(r"measure\s*=\s*[VR]\s+(\S+)\s+(\S+)", shape, re.I):
        return measure.span(1), measure.span(2)
    # Digital main still uses its existing port/connection declaration rules.
    if direction := re.fullmatch(r"(.+?)\s*=\s*(?:input|output|wire)", shape, re.I):
        return tuple(_arguments(text, *direction.span(1)))
    if component := re.fullmatch(r"[RCLXZVIS][A-Za-z0-9_]*\s+(\S+)\s+(\S+)\s+.+", shape, re.I):
        return component.span(1), component.span(2)
    return ()


def has_count_reference(text):
    """Opt into expansion only for a marker in a genuine connection slot."""
    return any(COUNT_REFERENCE.fullmatch(text[left:right].strip()) for left, right in wire_fields(text))


class WireCounts:
    """Remember prefix associations for one local execution, across its branches."""

    def __init__(self):
        self.prefixes = {}

    def resolve(self, token, scope, row):
        match = COUNT_REFERENCE.fullmatch(token.strip())
        if match is None:
            return token
        prefix, operation, name = match.group("prefix", "operator", "counter")

        def error(message):
            return CircuitError(message, row[1], "WireCount", path=row[2].path)

        if not name and operation == "$":
            raise error("Name the counter after $, e.g. w$i.")
        if not name and operation == "^" and getattr(scope.get(prefix), "type", None) != "count":
            # Scoped declarations can shadow a counter and later disappear.
            # Reuse the most recently associated counter still visible here.
            for candidate, variable in reversed(self.prefixes.get(prefix, ())):
                if scope.get(candidate) is variable:
                    name = candidate
                    break
            if not name:
                raise error(
                    f'No current counter for "{prefix}". Declare {prefix} = count; '
                    f'or select a counter with an explicit reference such as {prefix}*i.'
                )
        if not name:
            name = prefix
        variable = scope.get(name)
        if variable is None:
            raise error(f'Unknown wire counter "{name}". Declare {name} = count; inside main or the current method first.')
        if variable.type != "count":
            raise error(f'"{name}" is not a wire counter. Declare {name} = count; instead of an integer variable.')
        if operation == "*":
            if variable.value >= _MAX_COUNT:
                raise error("Wire counter exceeds the signed 64-bit integer range.")
            variable.value += 1
        history = self.prefixes.setdefault(prefix, [])
        if not history or history[-1][1] is not variable:
            history.append((name, variable))
        return prefix + str(variable.value)


def interpolate_connections(row, interpolate, resolve):
    """Expand placeholders and wire references in their textual execution order."""
    text = row[0]
    pieces, cursor = [], 0
    for left, right in wire_fields(text):
        pieces.append(interpolate(text[cursor:left]))
        # An interpolated prefix is allowed too, e.g. bank${k}*i. Its
        # placeholder reads the count before the following advance occurs.
        pieces.append(resolve(interpolate(text[left:right])))
        cursor = right
    pieces.append(interpolate(text[cursor:]))
    return "".join(pieces)
