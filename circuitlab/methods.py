"""Expand reusable circuit methods before assigning electrical connections."""

import re
from dataclasses import dataclass

from .graph import CircuitError
from .numbers import scalar
from .syntax import (
    ASSIGNMENT,
    BATTERY_BRACE,
    CALL,
    DEFAULT_TERMINALS,
    INLINE,
    MAIN,
    OP,
    PARAMETER_TARGET,
    PART_ARGUMENT,
    PART_PLACEMENT,
    SECTION,
    UNITS,
    Binding,
    SourceRow,
    parse_wires,
)


@dataclass(frozen=True)
class Method:
    """A declaration with its source rows and component or wire bindings."""

    kind: str
    name: str
    parameters: tuple[str, ...]
    body: list[SourceRow]
    targets: dict[str, str]


def _bind_arguments(method, raw, parent):
    """Validate typed arguments and resolve wire arguments in the caller."""
    name, formal, targets = method.name, method.parameters, method.targets
    parts = {}
    arguments = (argument.strip() for argument in raw.split(","))
    actual = tuple(parent.values.get(value, value) for value in arguments)
    if len(actual) != len(formal):
        raise CircuitError(f"{name} expects {len(formal)} arguments.")
    mapping = {}
    for parameter, value in zip(formal, actual):
        target = targets.get(parameter, "wire")
        if target.lower() == "part":
            component = PART_ARGUMENT.fullmatch(value)
            if not component:
                raise CircuitError(
                    f'{name}: argument "{parameter}" expects a resistor, capacitor, or inductor such as R(1k).'
                )
            part_name, part_value = component.groups()
            try:
                scalar(part_value, UNITS[part_name[0].upper()])
            except CircuitError as error:
                raise CircuitError(
                    f'{name}: argument "{parameter}" for {part_name}. {error}'
                ) from None
            if len(part_name) == 1:
                part_name = f"{part_name}_{parameter}"
            parts[parameter] = part_name, part_value
        elif target.lower() == "wire":
            mapping[parameter] = parent.wire(parse_wires(value, 1)[0])
        else:
            unit = UNITS[target[0].upper()]
            try:
                scalar(value, unit)
            except CircuitError as error:
                raise CircuitError(
                    f'{name}: argument "{parameter}" for {target}. {error}'
                ) from None
    return actual, mapping, parts


def expand_main(rows: list[SourceRow]) -> list[SourceRow]:
    """Replace method calls with bound statements in execution order."""
    starts = [i for i, (text, _, _) in enumerate(rows) if MAIN.fullmatch(text)]
    if not starts:
        for text, line, _ in rows:
            match = SECTION.fullmatch(text)
            if match and (match[1].lower() == "circuit" or match[3] is None):
                raise CircuitError(f'Call "{match[2]}" from a main block.', line)
        return rows
    if len(starts) != 1:
        raise CircuitError("Use one main block.", rows[starts[1]][1])

    def end(start, name):
        openings = [start]
        for i in range(start + 1, len(rows)):
            text, line, _ = rows[i]
            if text == "}":
                openings.pop()
                if not openings:
                    return i
            elif INLINE.fullmatch(text) or BATTERY_BRACE.fullmatch(text):
                openings.append(i)
            elif text.endswith("{"):
                raise CircuitError(
                    "Define named sections at the top level. Call them from inside main or a circuit method.",
                    line,
                )
        opener = rows[openings[-1]]
        inline = INLINE.fullmatch(opener[0])
        battery = BATTERY_BRACE.fullmatch(opener[0])
        label = inline[1] if inline else battery[1] if battery else name
        raise CircuitError(f'Close "{label}" with }}.', opener[1])

    # Collect definitions before expanding any call so forward references work.
    start = starts[0]
    stop = end(start, "main")
    definitions, counts, before, after = {}, {}, [], []
    i = 0
    while i < len(rows):
        if i == start:
            i = stop + 1
            continue
        text, line, _ = rows[i]
        match = SECTION.fullmatch(text)
        if not match:
            (before if i < start else after).append(rows[i])
            i += 1
            continue
        kind, name, raw = match.groups()
        kind = kind.lower()
        key = name.casefold()
        if key in definitions or OP.fullmatch(name):
            raise CircuitError(
                f'Section name "{name}" is reserved or already defined.', line
            )
        if raw is None:
            pins = DEFAULT_TERMINALS
        else:
            pins = tuple(part.strip() for part in raw.split(",")) if raw.strip() else ()
        j = end(i, name)
        child = rows[i + 1 : j]
        targets = {}
        if kind == "circuit":
            targets = {
                placement[1]: "part"
                for statement, _, _ in child
                if (placement := PART_PLACEMENT.fullmatch(statement))
                and placement[1] in pins
            }
        elif raw is not None:
            for statement, body_line, _ in child:
                assignment = ASSIGNMENT.fullmatch(statement)
                if not assignment or assignment[1] not in pins:
                    continue
                parameter, target = assignment.groups()
                if target.lower() in {"wire", "part"} or PARAMETER_TARGET.fullmatch(
                    target
                ):
                    if parameter in targets:
                        raise CircuitError(
                            f'Bind parameter "{parameter}" once.', body_line
                        )
                    targets[parameter] = target
        if (
            len(set(pins)) != len(pins)
            or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in pins)
            or (kind != "circuit" and not targets and len(pins) != 2)
        ):
            requirement = "distinct" if kind == "circuit" or targets else "two distinct"
            raise CircuitError(
                f'Use {requirement} parameter names in the header of "{name}".', line
            )
        if kind != "circuit" and targets and targets.keys() != set(pins):
            missing = ", ".join(p for p in pins if p not in targets)
            raise CircuitError(f'Bind these parameters in "{name}": {missing}.', line)
        if kind == "circuit" and j == i + 1:
            raise CircuitError(f'Circuit method "{name}" needs a body.', line)
        definitions[key] = Method(kind, name, pins, child, targets)
        i = j + 1

    def expand(body, parent, active):
        for text, line, _ in body:
            call = CALL.fullmatch(text)
            if not call or OP.fullmatch(call[1]):
                yield text, line, parent
                continue
            key = call[1].casefold()
            if key not in definitions:
                raise CircuitError(f'Unknown section "{call[1]}".', line)
            method = definitions[key]
            kind, name = method.kind, method.name
            formal, child, targets = method.parameters, method.body, method.targets
            if key in active:
                raise CircuitError(
                    f'Circuit method "{name}" calls itself recursively.', line
                )
            parts = {}
            try:
                if targets:
                    actual, mapping, parts = _bind_arguments(method, call[2], parent)
                else:
                    actual = tuple(map(parent.wire, parse_wires(call[2], len(formal))))
                    mapping = dict(zip(formal, actual))
            except CircuitError as error:
                if targets:
                    raise CircuitError(str(error), line) from None
                message = (
                    f"{name} expects {len(formal)} wire arguments."
                    if kind == "circuit"
                    else f"Use {name}(inputWire, outputWire); with two wire names."
                )
                raise CircuitError(message, line) from None
            # Sibling calls get distinct scopes even when they share a definition.
            instance = parent.scope, key
            counts[instance] = counts.get(instance, 0) + 1
            scope = parent.part(f"{name}_{counts[instance]}")
            values = {p: v for p, v in zip(formal, actual) if p not in mapping}
            binding = Binding(
                scope, mapping, in_main=bool(targets and parent.in_main), values=values
            )
            if targets:
                expanded_child = []
                for statement, body_line, original in child:
                    assignment = ASSIGNMENT.fullmatch(statement)
                    placement = PART_PLACEMENT.fullmatch(statement)
                    if kind == "circuit" and placement and placement[1] in parts:
                        part, value = parts[placement[1]]
                        statement = f"{part}({value}) between({placement[2]})"
                    elif (
                        kind != "circuit"
                        and assignment
                        and targets.get(assignment[1]) == assignment[2]
                    ):
                        parameter, target = assignment.groups()
                        if target.lower() == "wire":
                            statement = f"wire = {parameter}"
                        else:
                            part, value = parts.get(
                                parameter, (target, values[parameter])
                            )
                            statement = f"{part} = {value}"
                    expanded_child.append((statement, body_line, original))
                child = expanded_child
            if kind == "circuit":
                yield from expand(child, binding, (*active, key))
            else:
                header = (
                    f"{kind} {{"
                    if targets
                    else f"{kind} {scope}({', '.join(actual)}) {{"
                )
                yield header, line, binding
                yield from expand(child, binding, (*active, key))
                yield "}", line, binding

    expanded = list(expand(rows[start + 1 : stop], Binding(in_main=True), ()))
    return before + expanded + after
