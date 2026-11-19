"""Lower digital switch/case dispatch into the existing behavioral if/else path."""
# Case lowering must retain written order because fallthrough can apply multiple assignments.
# Keep nested switch breaks scoped to their own switch, and reject conditional breaks whose
# meaning cannot be represented by the supported lowering.

import re

from circuitlab.language.control_flow import ExpansionBudget
from circuitlab.language.syntax import Binding
from circuitlab.model.graph import CircuitError


# Recognize switch headers and leading labels on already split source rows.
# The selector remains expression text for the behavioral compiler; this
# pass resolves case constants and rewrites structure without simulating it.
SWITCH = re.compile(r"switch\s*\((.*)\)\s*\{")
LABEL = re.compile(r"(?:case\s+([^:]+)|default)\s*:\s*(.*)")
# Case literals are unsigned decimal integers with optional digit separators.
# Named const values are resolved to the same integer form before comparison.
DECIMAL = re.compile(r"[0-9]+(?:_[0-9]+)*")


def normalize_switch_labels(rows):
    """Separate labels from statements before static loops are elaborated."""
    normalized = []
    for text, line in rows:
        # Peel only leading labels: case 0: case 1: x=... becomes two label rows
        # and one statement. A colon inside the remaining ternary expression is
        # left intact because that remainder no longer begins with a case label.
        while label := LABEL.fullmatch(text):
            # Keep the original line on every split piece, including stacked labels.
            # Separating a case prefix from for(...) also lets the static loop pass
            # recognize a loop beginning immediately after a label.
            normalized.append((("case " + label[1].strip() if label[1] is not None else "default") + ":", line))
            text = label[2].strip()
        if text:
            normalized.append((text, line))
    return normalized


# Lower ordered case bodies to conditional behavior after static loop indices are known.
# A case without break continues into later bodies exactly as written.
def expand_switches(rows):
    """Preserve source lines, stacked labels, and unconditional case fallthrough.

    A break must directly end a case. Conditional breaks are rejected instead of
    being turned into unconditional exits. Nested switches own their own breaks.
    Run after static loop expansion so loop indices can also be case constants.
    """
    # Normalization is safe to repeat after loop unrolling. Loop-index case
    # labels have now become constants, while ordinary switch-free bodies
    # take the fast return without any branch reconstruction.
    rows = normalize_switch_labels(rows)
    if not any(re.match(r"switch\b", text) for text, _ in rows):
        return rows
    constants = {}

    # Check case literal syntax and bound conversion before int(). Values may
    # use up to the maximum digital width, independent of the static loop
    # evaluator's signed 64-bit arithmetic limit.
    def number(raw, line):
        if not DECIMAL.fullmatch(raw):
            raise CircuitError("Use an unsigned integer or a declared const for a case label.", line)
        digits = raw.replace("_", "").lstrip("0") or "0"
        # Strip separators/leading zeros first. The decimal-length guard bounds
        # conversion work; bit_length then enforces the exact 4096-bit maximum.
        if len(digits) > 1234 or (value := int(digits)).bit_length() > 4096:
            raise CircuitError("Case values must fit within 4096 bits.", line)
        return value

    # Collect declared decimal constants across the body before lowering, so
    # case lookup does not depend on their position relative to a switch.
    # Declaration widths/duplicates are still validated by the module compiler.
    for text, line in rows:
        if match := re.fullmatch(r"const\s+(\w+)(?:\s*\[[^]]+\])?\s*=\s*(.*)", text):
            constants[match[1]] = number(match[2].strip(), line)

    budget = ExpansionBudget()

    # Charge generated and intermediate rows to one shared expansion budget.
    # Fallthrough duplicates paths, and nested lowering also creates rows;
    # count that work rather than just the original number of labels.
    def emit(output, row):
        budget.emitted((row[0], row[1], Binding()))
        output.append(row)

    # Walk arbitrary surrounding if/clock rows and recursively replace each
    # complete switch. depth counts switch nesting; source braces also include
    # other behavioral blocks when locating a switch's closing row.
    def expand(block, depth=0):
        if depth > 64:
            raise CircuitError("Switch blocks may nest at most 64 levels.", block[0][1] if block else None)
        output, index = [], 0
        while index < len(block):
            text, line = block[index]
            index += 1
            if header := SWITCH.fullmatch(text):
                # Extract only this switch body. Count all nested braced structures so an
                # inner if or switch cannot terminate the enclosing switch early.
                start, nesting = index, 1
                while index < len(block) and nesting:
                    nesting += int(block[index][0].endswith("{")) - int(block[index][0] == "}")
                    index += 1
                if nesting:
                    raise CircuitError("Close the switch block with }.", line)
                output.extend(lower(header[1], block[start:index - 1], line, depth + 1))
            # A label left outside a switch body has no owner. A malformed switch
            # header receives its own diagnostic rather than passing as ordinary HDL.
            elif re.match(r"(?:case\b|default\s*:)", text):
                raise CircuitError("Place case/default labels directly inside a switch block.", line)
            elif re.match(r"switch\b", text):
                raise CircuitError("Use switch (expression) { case value: ... break; }.", line)
            else:
                emit(output, (text, line))
        return output

    # First group labels with their written statement bodies; then determine
    # each possible entry path; finally emit an exclusive if/else-if dispatch.
    # Keeping those stages separate preserves fallthrough without treating
    # every case body as an independently selected conditional.
    def lower(expression, body, line, depth):
        # Each group is (numeric_value_or_None, label_line, statements). None is
        # default, distinct from numeric case 0. seen compares resolved integers,
        # so case 1 and a const whose value is 1 are duplicates.
        groups, seen, has_default, nesting = [], set(), False, 0
        for text, number_line in body:
            # Only labels at this switch's own level start a new group. Labels inside
            # a nested switch remain part of the current body until recursive lowering.
            label = LABEL.fullmatch(text) if nesting == 0 else None
            if label:
                # Bound labels, including default, because each one can produce a copied
                # fallthrough path. Multiple default labels and repeated values are errors.
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
                # Statements need an entry label. Do not silently attach a leading action
                # to every case or drop it while reconstructing the dispatch.
                if not groups:
                    raise CircuitError("Start the switch body with a case or default label.", number_line)
                groups[-1][2].append((text, number_line))
                nesting += int(text.endswith("{")) - int(text == "}")
        if not groups:
            raise CircuitError("Give the switch at least one case or default label.", line)

        # Expand nested switches first: their breaks must not exit this switch.
        # An inner switch's break rows disappear into its generated branches, so
        # this case's scan cannot mistake them for an exit from the outer switch.
        paths = []
        for value, number_line, statements in groups:
            statements = expand(statements, depth)
            path, nesting, stops = [], 0, False
            for text, source_line in statements:
                # Only a direct break at the case's current level can end this path.
                # A break inside an if/other braced construct is conditional, which this
                # copy-and-concatenate lowering cannot represent as an unconditional stop.
                if text == "break":
                    if nesting:
                        raise CircuitError("Conditional break is not supported; put break directly at the end of a case.", source_line)
                    # Keep statements before the first direct break, then omit later rows from
                    # this case's path. stops prevents entry paths from continuing into the
                    # next label; it does not become a break in the generated HDL.
                    stops = True
                    break
                path.append((text, source_line))
                nesting += int(text.endswith("{")) - int(text == "}")
            paths.append((value, number_line, path, stops))

        # Entering a label executes that group and successive groups until a break
        # or the end. Empty groups implement stacked labels naturally. Default
        # participates in this written order just like any other label.
        def fallthrough(start):
            path = []
            for _, _, statements, stops in paths[start:]:
                path.extend(statements)
                if stops:
                    break
            return path

        # Generate tests for explicit numeric cases first. Default selection must
        # be the final fallback, even if its label appeared in the middle; its
        # copied path still starts at that original middle position.
        output, branches = [], 0
        default = None
        for index, (value, number_line, _, _) in enumerate(paths):
            if value is None:
                default = index
                continue
            prefix = "else if" if branches else "if"
            # A generated comparison/closing brace points to its case label. Copied
            # action rows retain their own source lines, including actions reached
            # through fallthrough from a different label.
            emit(output, (f"{prefix} (({expression}) == {value}) {{", number_line))
            for row in fallthrough(index):
                emit(output, row)
            emit(output, ("}", number_line))
            branches += 1
        # With numeric branches, default becomes else. A default-only switch is
        # unconditional, so emitting an orphan else wrapper would be incorrect.
        # Without default, add no artificial fallback: clocked state can hold,
        # and the combinational compiler enforces its own complete-assignment rules.
        if default is not None:
            if branches:
                emit(output, ("else {", paths[default][1]))
            for row in fallthrough(default):
                emit(output, row)
            if branches:
                emit(output, ("}", paths[default][1]))
        return output

    return expand(rows)
