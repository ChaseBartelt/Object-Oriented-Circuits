"""Lower digital switch/case dispatch into the existing behavioral if/else path."""

import re

from circuitlab.language.control_flow import ExpansionBudget
from circuitlab.language.syntax import Binding
from circuitlab.model.graph import CircuitError


SWITCH = re.compile(r"switch\s*\((.*)\)\s*\{")
LABEL = re.compile(r"(?:case\s+([^:]+)|default)\s*:\s*(.*)")
DECIMAL = re.compile(r"[0-9]+(?:_[0-9]+)*")


def normalize_switch_labels(rows):
    """Separate labels from statements before static loops are elaborated."""
    normalized = []
    for text, line in rows:
        while label := LABEL.fullmatch(text):
            normalized.append((("case " + label[1].strip() if label[1] is not None else "default") + ":", line))
            text = label[2].strip()
        if text:
            normalized.append((text, line))
    return normalized


def expand_switches(rows):
    """Preserve source lines, stacked labels, and unconditional case fallthrough.

    A break must directly end a case. Conditional breaks are rejected instead of
    being turned into unconditional exits. Nested switches own their own breaks.
    Run after static loop expansion so loop indices can also be case constants.
    """
    rows = normalize_switch_labels(rows)
    if not any(re.match(r"switch\b", text) for text, _ in rows):
        return rows
    constants = {}

    def number(raw, line):
        if not DECIMAL.fullmatch(raw):
            raise CircuitError("Use an unsigned integer or a declared const for a case label.", line)
        digits = raw.replace("_", "").lstrip("0") or "0"
        if len(digits) > 1234 or (value := int(digits)).bit_length() > 4096:
            raise CircuitError("Case values must fit within 4096 bits.", line)
        return value

    for text, line in rows:
        if match := re.fullmatch(r"const\s+(\w+)(?:\s*\[[^]]+\])?\s*=\s*(.*)", text):
            constants[match[1]] = number(match[2].strip(), line)

    budget = ExpansionBudget()

    def emit(output, row):
        budget.emitted((row[0], row[1], Binding()))
        output.append(row)

    def expand(block, depth=0):
        if depth > 64:
            raise CircuitError("Switch blocks may nest at most 64 levels.", block[0][1] if block else None)
        output, index = [], 0
        while index < len(block):
            text, line = block[index]
            index += 1
            if header := SWITCH.fullmatch(text):
                start, nesting = index, 1
                while index < len(block) and nesting:
                    nesting += int(block[index][0].endswith("{")) - int(block[index][0] == "}")
                    index += 1
                if nesting:
                    raise CircuitError("Close the switch block with }.", line)
                output.extend(lower(header[1], block[start:index - 1], line, depth + 1))
            elif re.match(r"(?:case\b|default\s*:)", text):
                raise CircuitError("Place case/default labels directly inside a switch block.", line)
            elif re.match(r"switch\b", text):
                raise CircuitError("Use switch (expression) { case value: ... break; }.", line)
            else:
                emit(output, (text, line))
        return output

    def lower(expression, body, line, depth):
        groups, seen, has_default, nesting = [], set(), False, 0
        for text, number_line in body:
            label = LABEL.fullmatch(text) if nesting == 0 else None
            if label:
                if len(groups) >= 256:
                    raise CircuitError("A switch may contain at most 256 case/default labels.", number_line)
                if label[1] is None:
                    if has_default:
                        raise CircuitError("Use only one default label per switch.", number_line)
                    has_default, value = True, None
                else:
                    raw = label[1].strip()
                    value = constants[raw] if raw in constants else number(raw, number_line)
                    if value in seen:
                        raise CircuitError(f"Duplicate case value {value}.", number_line)
                    seen.add(value)
                groups.append((value, number_line, []))
            else:
                if not groups:
                    raise CircuitError("Start the switch body with a case or default label.", number_line)
                groups[-1][2].append((text, number_line))
                nesting += int(text.endswith("{")) - int(text == "}")
        if not groups:
            raise CircuitError("Give the switch at least one case or default label.", line)

        # Expand nested switches first: their breaks must not exit this switch.
        paths = []
        for value, number_line, statements in groups:
            statements = expand(statements, depth)
            path, nesting, stops = [], 0, False
            for text, source_line in statements:
                if text == "break":
                    if nesting:
                        raise CircuitError("Conditional break is not supported; put break directly at the end of a case.", source_line)
                    stops = True
                    break
                path.append((text, source_line))
                nesting += int(text.endswith("{")) - int(text == "}")
            paths.append((value, number_line, path, stops))

        def fallthrough(start):
            path = []
            for _, _, statements, stops in paths[start:]:
                path.extend(statements)
                if stops:
                    break
            return path

        output, branches = [], 0
        default = None
        for index, (value, number_line, _, _) in enumerate(paths):
            if value is None:
                default = index
                continue
            prefix = "else if" if branches else "if"
            emit(output, (f"{prefix} (({expression}) == {value}) {{", number_line))
            for row in fallthrough(index):
                emit(output, row)
            emit(output, ("}", number_line))
            branches += 1
        if default is not None:
            if branches:
                emit(output, ("else {", paths[default][1]))
            for row in fallthrough(default):
                emit(output, row)
            if branches:
                emit(output, ("}", paths[default][1]))
        return output

    return expand(rows)
