"""Expand reusable circuit methods before assigning electrical connections."""

import re
from dataclasses import dataclass, replace
from itertools import chain

from circuitlab.language.control_flow import (
    MAX_EXPANDED_ROWS,
    ExpansionBudget,
    expand_control_flow,
    is_control_block,
    is_control_statement,
)
from circuitlab.language.gates import GATE_HELP, gate_accepts, gate_body, gate_call_help, gate_signature
from circuitlab.model.graph import CircuitError, MethodInstance
from circuitlab.math.numbers import scalar
from circuitlab.language.syntax import (
    ASSIGNMENT,
    BATTERY_BRACE,
    CALL,
    DEFAULT_TERMINALS,
    INLINE,
    MAIN,
    NET_METHODS,
    OP,
    OVERRIDE,
    PARAMETER_TARGET,
    PART_ARGUMENT,
    PART_PLACEMENT,
    SECTION,
    SWITCH_CALL,
    TRANSISTOR,
    TRANS_CALL,
    UNAVAILABLE_MODEL,
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


_INTERPOLATION_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


def _bind_gate_call(name, raw, parent):
    """Resolve the gate's full pin list, preserving existing positional calls."""
    grouped = re.fullmatch(r"([^(),]+),\s*\(([^()]*)\)\s*,\s*([^(),]+)", raw)
    try:
        arguments = parse_wires(",".join(grouped.groups()) if grouped else raw)
    except CircuitError:
        raise CircuitError(gate_call_help(name)) from None
    actual = tuple(map(parent.wire, arguments))
    passed = tuple(zip(arguments, actual))
    if not grouped and gate_accepts(name, len(actual) - 3):
        return actual, passed
    if gate_accepts(name, len(actual) - 2):
        ground = parent.mapping.get("gnd", "0")
        return (actual[0], ground, *actual[1:]), passed
    raise CircuitError(gate_call_help(name))


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
                    f'{name}: argument "{parameter}" expects a resistor, capacitor, inductor, or AC impedance such as R(1k) or Z(2-j2).'
                )
            part_name, part_value = component.groups()
            part_value = part_value or ""
            try:
                if part_value.strip():
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


def expand_main(rows: list[SourceRow], instances: list[MethodInstance], *, imports=()) -> list[SourceRow]:
    """Replace method calls with bound statements in execution order."""
    import_count = len(imports)
    rows = [*imports, *rows]
    uses_control_flow = any(is_control_statement(text) for text, _, _ in rows)
    starts = [i for i, (text, _, _) in enumerate(rows) if MAIN.fullmatch(text)]
    if len(starts) > 1:
        raise CircuitError("Use one main block.", rows[starts[1]][1])

    def end(start, name):
        openings = [start]
        for i in range(start + 1, len(rows)):
            text, line, _ = rows[i]
            if OVERRIDE.fullmatch(text):
                raise CircuitError("Put @Override above a top-level gate definition.", line)
            if text == "}":
                openings.pop()
                if not openings:
                    return i
            elif text.endswith("{"):
                shape = _INTERPOLATION_PLACEHOLDER.sub("_", text)
                if is_control_block(text) or any(
                    header.fullmatch(shape) for header in (INLINE, BATTERY_BRACE, TRANSISTOR)
                ):
                    openings.append(i)
                    continue
                raise CircuitError(
                    "Define named sections at the top level. Call them from inside main or a circuit method.",
                    line,
                )
        opener = rows[openings[-1]]
        block = (INLINE.fullmatch(opener[0]) or BATTERY_BRACE.fullmatch(opener[0])
                 or TRANSISTOR.fullmatch(opener[0]))
        label = block[1] if block else name
        raise CircuitError(f'Close "{label}" with }}.', opener[1])

    # Collect definitions before expanding any call so forward references work.
    start = starts[0] if starts else len(rows)
    stop = end(start, "main") if starts else start
    definitions, overrides, counts = {}, {}, {}
    before, after = [], []
    i = 0
    while i < len(rows):
        if i == start:
            i = stop + 1
            continue
        text, line, origin = rows[i]
        annotation = None
        if OVERRIDE.fullmatch(text):
            annotation = line
            i += 1
            if i < len(rows):
                text, line, origin = rows[i]
            else:
                text = ""
        match = SECTION.fullmatch(text)
        if annotation and (not match or match[1].lower() != "gate"):
            raise CircuitError("Put @Override directly above a gate definition.", annotation)
        if match and not starts and not annotation and i >= import_count and (
            match[1].lower() in NET_METHODS or match[3] is None
        ):
            raise CircuitError(f'Call "{match[2]}" from a main block.', line)
        if not match or (not starts and not annotation and i >= import_count):
            (before if i < start else after).append(rows[i])
            i += 1
            continue
        kind, name, raw = match.groups()
        kind = kind.lower()
        net_method = kind in NET_METHODS
        key = name.casefold()
        if OP.fullmatch(name) or (not annotation and key in definitions):
            raise CircuitError(
                f'Section name "{name}" is reserved or already defined.', line, path=origin.path
            )
        if raw is None:
            pins = DEFAULT_TERMINALS
        else:
            pins = tuple(part.strip() for part in raw.split(",")) if raw.strip() else ()
        if annotation:
            if key not in GATE_HELP:
                raise CircuitError(f'No built-in gate named "{name}" to override.', line)
            if not gate_accepts(key, len(pins) - 3):
                raise CircuitError(f"Use gate {key}({gate_signature(key)}).", line)
            if (key, len(pins)) in overrides:
                raise CircuitError(f'Gate "{name}" already has an override for these pins.', line)
            if key in definitions:
                raise CircuitError(f'Gate "{name}" already has an unannotated definition.', line)
        elif any(gate == key for gate, _ in overrides):
            raise CircuitError(f'Gate "{name}" already has an @Override definition.', line)
        j = end(i, name)
        child = rows[i + 1 : j]
        targets = {}
        if net_method:
            targets = {
                placement[1]: "part"
                for statement, _, _ in child
                if (placement := PART_PLACEMENT.fullmatch(_INTERPOLATION_PLACEHOLDER.sub("_", statement)))
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
            or (not net_method and not targets and len(pins) != 2)
        ):
            requirement = "distinct" if net_method or targets else "two distinct"
            raise CircuitError(
                f'Use {requirement} parameter names in the header of "{name}".', line, path=origin.path
            )
        if not net_method and targets and targets.keys() != set(pins):
            missing = ", ".join(p for p in pins if p not in targets)
            raise CircuitError(f'Bind these parameters in "{name}": {missing}.', line)
        if net_method and j == i + 1:
            raise CircuitError(f'{kind.capitalize()} method "{name}" needs a body.', line)
        if annotation and targets:
            raise CircuitError("Use wire pins in a gate override.", line)
        method = Method(kind, name, pins, child, targets)
        if annotation:
            overrides[key, len(pins)] = method
        else:
            definitions[key] = method
        i = j + 1

    budget = ExpansionBudget()

    def expand(body, parent, active, *, top_level=False):
        transistor = False
        for text, line, origin in body:
            if parent.path != origin.path:
                parent = replace(parent, path=origin.path)
            if top_level and is_control_statement(text):
                raise CircuitError(
                    "Put compile-time control flow inside main or a circuit method.",
                    line,
                    path=origin.path,
                )
            if transistor or TRANSISTOR.fullmatch(text):
                yield text, line, parent
                transistor = text != "}"
                continue
            call = CALL.fullmatch(text.removesuffix(";").rstrip())
            if not call or OP.fullmatch(call[1]):
                yield text, line, parent
                continue
            key = call[1].casefold()
            method_key = key, None
            gate_wires = None
            builtin_gate = False
            passed = ()
            if key in GATE_HELP and key not in definitions:
                try:
                    gate_wires, passed = _bind_gate_call(key, call[2], parent)
                except CircuitError as error:
                    raise CircuitError(error.message, line, path=parent.path) from None
                arity = len(gate_wires)
                method_key = key, arity
                method = overrides.get(method_key)
                if method is None:
                    builtin_gate = True
                    pins, statements = gate_body(key, arity - 3)
                    child = [(statement, line, parent) for statement in statements]
                    method = Method("gate", key, pins, child, {})
            elif top_level or key not in definitions:
                if top_level or TRANS_CALL.fullmatch(call[1]) or SWITCH_CALL.fullmatch(call[1]):
                    yield text, line, parent
                    continue
                raise CircuitError(f'Unknown section "{call[1]}".', line, path=parent.path)
            else:
                method = definitions[key]
            kind, name = method.kind, method.name
            net_method = kind in NET_METHODS
            formal, child, targets = method.parameters, method.body, method.targets
            if method_key in active:
                raise CircuitError(
                    f'Circuit method "{name}" calls itself recursively.', line, path=parent.path
                )
            parts = {}
            try:
                if targets:
                    actual, mapping, parts = _bind_arguments(method, call[2], parent)
                else:
                    if gate_wires is None:
                        raw_wires = parse_wires(call[2], len(formal))
                        actual = tuple(map(parent.wire, raw_wires))
                        passed = tuple(zip(raw_wires, actual))
                    else:
                        actual = gate_wires
                    mapping = dict(zip(formal, actual))
            except CircuitError as error:
                if targets:
                    raise CircuitError(error.message, line, path=parent.path) from None
                message = (
                    f"{name} expects {len(formal)} wire arguments."
                    if net_method
                    else f"Use {name}(inputWire, outputWire); with two wire names."
                )
                raise CircuitError(message, line, path=parent.path) from None
            # Sibling calls get distinct scopes even when they share a definition.
            instance = parent.scope, key
            counts[instance] = counts.get(instance, 0) + 1
            scope = parent.part(f"{name}_{counts[instance]}")
            values = {p: v for p, v in zip(formal, actual) if p not in mapping}
            index = len(instances)
            instances.append(
                MethodInstance(kind, name, parent.instance, tuple(mapping.items()),
                               line=line, arguments=passed, path=parent.path,
                               builtin_gate=builtin_gate,
                               model_available=not (
                                   kind == "chip" and len(child) == 1
                                   and UNAVAILABLE_MODEL.fullmatch(child[0][0])
                               ))
            )
            if not instances[-1].model_available:
                continue
            binding = Binding(
                scope, mapping, in_main=bool(targets and parent.in_main),
                values=values, instance=index
            )
            child = expand_control_flow(child, budget=budget)
            if targets:
                expanded_child = []
                for statement, body_line, original in child:
                    assignment = ASSIGNMENT.fullmatch(statement)
                    placement = PART_PLACEMENT.fullmatch(statement)
                    if net_method and placement and placement[1] in parts:
                        part, value = parts[placement[1]]
                        statement = f"{part}({value}) between({placement[2]})"
                    elif (
                        not net_method
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
            if not net_method:
                header = (
                    f"{kind} {{"
                    if targets
                    else f"{kind} {scope}({', '.join(actual)}) {{"
                )
                yield header, line, binding
            yield from expand(child, binding, (*active, method_key))
            if not net_method:
                yield "}", line, binding

    expanded = chain(
        expand(before, Binding(), (), top_level=True),
        expand(expand_control_flow(rows[start + 1 : stop], budget=budget), Binding(in_main=True), ()),
        expand(after, Binding(), (), top_level=True),
    )
    if not uses_control_flow:
        return list(expanded)

    result = []
    for row in expanded:
        if len(result) >= MAX_EXPANDED_ROWS:
            _, line, origin = row
            raise CircuitError(
                f"Compile-time control flow exceeded {MAX_EXPANDED_ROWS:,} expanded electrical statements.",
                line,
                path=origin.path,
            )
        result.append(row)
    return result
