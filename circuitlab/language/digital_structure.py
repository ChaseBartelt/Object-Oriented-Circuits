"""Elaborate static digital assembly into a hygienic, behavior-only top module.

The electrical compiler is unchanged. Main expands compile-time control flow;
chip calls create independent signal scopes. Behavioral if/clock blocks are
copied intact and compiled by the digital exporter, never executed here.
"""

import re
from dataclasses import dataclass

from circuitlab.language.control_flow import ExpansionBudget, expand_control_flow
from circuitlab.language.syntax import CHIP, DIGITAL_MAIN, source_rows
from circuitlab.model.graph import CircuitError, MethodInstance


DECLARATION = re.compile(r"(.+?)\s*=\s*(input|output|wire)", re.I)
CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)")
IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
MAX_INSTANCES = 1024
MAX_DEPTH = 32


@dataclass
class _Chip:
    name: str
    pins: tuple
    body: list
    line: int
    signals: dict
    directions: dict


def _declarations(rows):
    # Import lazily: the exporter invokes this module after defining validators.
    from circuitlab.exporters.verilog import _signal_list, _state
    signals, kinds, states = {}, {}, {}
    depth, top_rows = 0, []
    for text, line, _ in rows:
        if depth == 0:
            top_rows.append((text, line))
            if match := DECLARATION.fullmatch(text):
                for name, width in _signal_list(match[1], line).items():
                    if name in signals:
                        raise CircuitError(f'Signal "{name}" is already declared.', line)
                    signals[name], kinds[name] = width, match[2].lower()
        depth += int(text.endswith("{")) - int(text == "}")
    for text, line in top_rows:
        if not text.endswith("{") and not CALL.fullmatch(text) and not DECLARATION.fullmatch(text):
            declaration = text.removeprefix("const ")
            left = declaration.partition("=")[0].strip()
            name = left.partition("[")[0].strip()
            # Output/wire assignments are behavior, not state declarations.
            if kinds.get(name) not in {"input", "output", "wire"} and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\[[^]]+\])?", left):
                _state(declaration, (), states, line)
                width, _, vector = states[name]
                signals[name], kinds[name] = width if vector else None, "state"
    return signals, kinds


def _definitions(source):
    from circuitlab.exporters.verilog import _name, _pin_list
    rows = list(source_rows(source, legacy_markers=False))
    # Leave old chip-only programs entirely on their existing compiler path.
    if not any(DIGITAL_MAIN.fullmatch(text) for text, _, _ in rows):
        return None
    chips, main = {}, None
    index = 0
    while index < len(rows):
        text, line, _ = rows[index]
        chip, assembly = CHIP.fullmatch(text), DIGITAL_MAIN.fullmatch(text)
        if not chip and not assembly:
            raise CircuitError("Digital assembly contains chip definitions and one main block; electrical parts cannot be mixed in.", line)
        index += 1
        body, depth = [], 1
        while index < len(rows) and depth:
            row = rows[index]
            depth += int(row[0].endswith("{")) - int(row[0] == "}")
            if depth > 64:
                raise CircuitError("Digital blocks may nest at most 64 levels.", row[1])
            if depth:
                body.append(row)
            index += 1
        if depth:
            raise CircuitError("Close the digital assembly block with }.", line)
        if assembly:
            if main is not None:
                raise CircuitError("Use one main block.", line)
            main = (_name(assembly[1] or "main", line), body, line)
        else:
            name = _name(chip[1], line)
            if name in chips:
                raise CircuitError(f'Chip "{name}" is already defined.', line)
            pins = _pin_list(chip[2], line)
            signals, kinds = _declarations(body)
            declared = {p for p, kind in kinds.items() if kind in {"input", "output"}}
            if declared != set(pins):
                raise CircuitError(f'Chip "{name}" must declare every header pin as input or output, with no extra ports.', line)
            chips[name] = _Chip(name, pins, body, line, signals, kinds)
    return chips, main


def assemble_digital(source, *, instances=None):
    """Return (top, pins, flattened body rows, line), or None without main.

Optional instance metadata lets the schematic display actual assembled chips.
Source text and saved layouts are never rewritten by elaboration.
"""
    from circuitlab.exporters.verilog import _module, _name, _signal_list
    parsed = _definitions(source)
    if parsed is None:
        return None
    chips, (top, main, line) = parsed
    if top in chips:
        raise CircuitError(f'Main name "{top}" conflicts with a chip definition; choose another top name.', line)
    budget = ExpansionBudget()
    main = list(expand_control_flow(main, budget=budget))
    widths, kinds, pins, output = {}, {}, [], []
    for text, number, _ in main:
        if match := DECLARATION.fullmatch(text):
            kind = match[2].lower()
            for name, width in _signal_list(match[1], number).items():
                if name in widths:
                    raise CircuitError(f'Signal "{name}" is already declared in main.', number)
                widths[name], kinds[name] = width, kind
                if kind != "wire":
                    pins.append(name)
            output.append((text, number))
        elif not CALL.fullmatch(text):
            raise CircuitError("Main builds hardware: declare input/output/wire signals and call chips. Put signal-dependent if/clock behavior inside a chip.", number)
    if not pins:
        raise CircuitError("Declare main's external input/output signals.", line)
    occupied = set(widths)
    count = 0
    validated = set()

    def validate_chip(chip):
        if chip.name in validated:
            return
        # Validate each definition in its own scope. A missing child name must
        # never accidentally bind to a main signal after flattening. Nested
        # instance outputs act as declared drivers for this local check; their
        # real behavior is checked when that definition is instantiated.
        local_body, depth = [], 0
        for text, number, _ in chip.body:
            call = CALL.fullmatch(text) if depth == 0 else None
            if call:
                if call[1] not in chips:
                    raise CircuitError(f'Unknown behavioral chip "{call[1]}".', number)
                child = chips[call[1]]
                arguments = tuple(a.strip() for a in call[2].split(",")) if call[2].strip() else ()
                if len(arguments) != len(child.pins):
                    raise CircuitError(f'{child.name} expects {len(child.pins)} signal arguments.', number)
                for formal, argument in zip(child.pins, arguments):
                    if argument not in chip.signals:
                        raise CircuitError(f'{child.name}: undeclared local signal "{argument}".', number)
                    if child.directions[formal] == "output":
                        local_body.append((f"{argument} = 0", number))
            else:
                local_body.append((text, number))
            depth += int(text.endswith("{")) - int(text == "}")
        _module(chip.name, chip.pins, local_body, chip.line)
        validated.add(chip.name)

    def rename(row, mapping):
        def substitute(token):
            word = token[0]
            # A state named clock/every/const remains legal. Rename references,
            # while preserving the same words in their syntax positions.
            prefix, suffix = row[:token.start()], row[token.end():]
            if not prefix.strip() and (
                (word in {"clock", "every"} and suffix.lstrip().startswith("("))
                or (word == "const" and suffix[:1].isspace())
            ):
                return word
            if word in {"Hz", "kHz", "MHz", "GHz", "s", "ms", "us", "ns"} and re.search(r"[0-9]\s*$", prefix):
                return word
            return mapping.get(word, word)
        return IDENTIFIER.sub(substitute, row)

    def emit(text, number):
        # Count expanded chip bodies as well as main's generated statements.
        budget.emitted((text, number, None))
        output.append((text, number))

    def instantiate(text, number, context, parent=None, ancestors=()):
        nonlocal count
        call = CALL.fullmatch(text)
        name = call[1]
        if name not in chips:
            raise CircuitError(f'Unknown behavioral chip "{name}".', number)
        if name in ancestors or len(ancestors) >= MAX_DEPTH:
            raise CircuitError("Digital chip instances cannot recurse or nest beyond 32 levels.", number)
        chip = chips[name]
        validate_chip(chip)
        actual = tuple(a.strip() for a in call[2].split(",")) if call[2].strip() else ()
        if len(actual) != len(chip.pins):
            raise CircuitError(f'{name} expects {len(chip.pins)} signal arguments.', number)
        mapping = {}
        for formal, argument in zip(chip.pins, actual):
            if argument not in context:
                raise CircuitError(f'{name}: declare signal "{argument}" before connecting it.', number)
            net = context[argument]
            if widths[net] != chip.signals[formal]:
                raise CircuitError(f'{name}.{formal}: connection "{argument}" has a different width or scalar/vector shape.', number)
            if chip.directions[formal] == "output" and kinds[net] not in {"output", "wire"}:
                raise CircuitError(f'{name}.{formal}: connect an output to an output or wire, not "{argument}".', number)
            mapping[formal] = net
        count += 1
        if count > MAX_INSTANCES:
            raise CircuitError("Digital assembly exceeds 1024 chip instances.", number)
        prefix = f"{name}_{count}_"
        for local, width in chip.signals.items():
            if local in mapping:
                continue
            full = prefix + local
            while full in occupied:
                full = "ooc_" + full
            _name(full, number)
            occupied.add(full)
            widths[full], kinds[full] = width, chip.directions[local]
            mapping[local] = full
        own_index = None
        if instances is not None:
            own_index = len(instances)
            instances.append(MethodInstance(
                "chip", name, parent, tuple((p, mapping[p]) for p in chip.pins),
                tuple(row[0] for row in chip.body), line=number,
                arguments=tuple(zip(actual, (mapping[p] for p in chip.pins))),
            ))
        depth = 0
        for row, source_line, _ in chip.body:
            declaration = DECLARATION.fullmatch(row) if depth == 0 else None
            if declaration and declaration[2].lower() in {"input", "output"}:
                continue
            if depth == 0 and CALL.fullmatch(row):
                instantiate(row, source_line, mapping, own_index, (*ancestors, name))
                continue
            if re.match(r"(?:for|while|do)\b", row):
                raise CircuitError("Loops generate hardware in main; chip behavior uses if/else and clock/every blocks.", source_line)
            emit(rename(row, mapping), source_line)
            depth += int(row.endswith("{")) - int(row == "}")

    context = {name: name for name in widths}
    for text, number, _ in main:
        if CALL.fullmatch(text):
            instantiate(text, number, context)
    if not count:
        raise CircuitError("Main must instantiate at least one behavioral chip.", line)
    return top, tuple(pins), output, line
