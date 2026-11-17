"""Inspect incomplete OOC source for editors without constructing a Qt widget.

Locations use zero-based lines and UTF-16 columns, matching both VS Code and
QTextCursor. Wire locations retain the compiler's one-based source-line numbers.
This tolerant inspection describes source; it does not compile or simulate it.
"""

import re

from circuitlab.language.control_flow import is_control_block
from circuitlab.language.digital_declarations import INLINE_WIRE, normalize_wire_declarations
from circuitlab.language.gates import GATE_HELP, gate_accepts, gate_call_help
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.model.graph import CircuitError
from circuitlab.language.syntax import (
    CALL, CONNECTOR, DEFAULT_TERMINALS, DIGITAL_MAIN, INCLUDE, MAIN, MARKER, OP, SECTION,
    TRANS_CALL, source_rows,
)


IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
TIMED_BLOCK = re.compile(r"(?:^|(?<=[;{}]))\s*(clock|every)\s*\([^()]*\)\s*(?=\{)")


def timed_block_at(code, column):
    """Distinguish HDL block headers from ordinary electrical method calls."""
    for match in TIMED_BLOCK.finditer(code):
        if match.start(1) <= column < match.end():
            return match[1]
    return None


def _pin_parameters(kind, parameters, line):
    if kind.lower() == 'chip' and parameters is not None:
        try:
            return expand_pin_banks(parameters, line)
        except CircuitError:
            pass  # Keep incomplete editor signatures available while typing.
    return parameters


def methods(source):
    """Collect top-level signatures, including unfinished editor documents."""
    result = {}
    depth = 0
    for text, line, _ in source_rows(source):
        if depth == 0 and (match := SECTION.fullmatch(text)):
            kind, name, parameters = match.groups()
            parameters = _pin_parameters(kind, parameters, line)
            if not OP.fullmatch(name):
                pins = DEFAULT_TERMINALS if parameters is None else parameters.split(",")
                result.setdefault(
                    name.casefold(),
                    (kind.lower(), name, ", ".join(pin.strip() for pin in pins)),
                )
        depth = max(0, depth + text.count("{") - text.count("}"))
    return result


def accepts_calls(source):
    """Whether the end of this source is inside a block accepting method calls."""
    blocks = []
    for text, _, _ in source_rows(source):
        if text.endswith("{"):
            section = SECTION.fullmatch(text)
            blocks.append(bool(
                MAIN.fullmatch(text)
                or DIGITAL_MAIN.fullmatch(text)
                or section
                or (
                    blocks and blocks[-1]
                    and (re.match(r"(?:series|parallel)\b", text, re.I) or is_control_block(text))
                )
            ))
        elif text == "}" and blocks:
            blocks.pop()
        elif MARKER.fullmatch(text):
            if text[1:].lstrip().lower().startswith("end "):
                if blocks:
                    blocks.pop()
            else:
                blocks.append(False)
    return bool(blocks and blocks[-1])


def gate_inputs(name, arguments):
    """Infer displayed input count using the existing gate shorthand rules."""
    if "(" in arguments:
        return arguments.partition("(")[2].partition(")")[0].count(",") + 1
    count = arguments.count(",") + 1
    return count - 3 if gate_accepts(name, count - 3) else max(1, count - 2)


class SourceInfo:
    """Source and optional saved-include reader used by completion and hovers.

    Includes are read when signatures are requested, so changing an included
    chip file is visible even if the entry document itself has not changed.
    """

    def __init__(self, source, read_imports=None):
        # QTextDocument normalizes line endings; use the same convention for IDE
        # requests without changing the source passed to the compiler.
        self.source = source.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = self.source.split("\n")
        self.read_imports = read_imports

    def position(self, line, column):
        """Return (line text, Python string index), or None outside the source."""
        if not isinstance(line, int) or not isinstance(column, int):
            return None
        if not 0 <= line < len(self.lines) or column < 0:
            return None
        text = self.lines[line]
        units = 0
        for index, char in enumerate(text):
            if units == column:
                return text, index
            units += 2 if ord(char) > 0xFFFF else 1
            if units > column:
                return None  # A position inside a UTF-16 surrogate pair.
        return (text, len(text)) if units == column else None

    def method_signatures(self):
        signatures = {}
        if self.read_imports and "include" in self.source.casefold():
            for included in self.read_imports(self.source):
                signatures.update(methods(included))
        signatures.update(methods(self.source))
        return signatures

    def _computed_wire_help(self, line, column, code):
        start = max(code.rfind(mark, 0, column) for mark in ';{}') + 1
        raw = re.split(r'[;{}]', code[start:], maxsplit=1)[0]
        statement = raw.strip()
        column -= start + len(raw) - len(raw.lstrip())
        candidate = INLINE_WIRE.fullmatch(statement)
        if (not candidate or not 0 <= column <= candidate.end(2) + 1
                or candidate[3].strip().lower() in {'input', 'output', 'wire'}):
            return ''
        # Use compiler classification in the complete enclosing chip scope.
        # In particular, an existing signal's [index] is not a new [width].
        body, pins, target, depth = None, (), None, 0
        for index, (text, number, _) in enumerate(source_rows(self.source)):
            section = SECTION.fullmatch(text) if depth == 0 else None
            if section and section[1].lower() == 'chip':
                body, target = [], None
                parameters = _pin_parameters('chip', section[3], number) or ''
                pins = tuple(pin.strip() for pin in parameters.split(',') if pin.strip())
            elif body is not None:
                if text == '}' and depth == 1:
                    if target is not None:
                        break
                    body = None
                else:
                    body.append((text, number, index))
                    if depth == 1 and number == line + 1 and text == statement:
                        target = index
            depth = max(0, depth + text.count('{') - text.count('}'))
        if body is None or target is None:
            return ''
        try:
            normalized = normalize_wire_declarations(body, pins)
        except CircuitError:
            return ''
        declaration = re.compile(re.escape(candidate[1]) + r'\s*\[\s*([^]]+)\s*\]\s*=\s*wire', re.I)
        for text, _, origin in normalized:
            if origin == target and (match := declaration.fullmatch(text)):
                return (f'{statement}\nComputed wire: {match[1].strip()} bits.\n'
                        'Declares an unsigned combinational signal that follows the expression as its inputs change. '
                        'It does not store state or wait for a clock edge.')
        return ''

    def hover_help(self, line, column):
        location = self.position(line, column)
        if location is None:
            return ""
        text, column = location
        code = text.partition("#")[0]
        if computed := self._computed_wire_help(line, column, code):
            return computed
        section = SECTION.match(code.strip())
        direction = re.fullmatch(r'.+?=\s*(input|output)\s*;?', code.strip(), re.I)
        if direction or (section and section[1].lower() == 'chip'):
            for bank in re.finditer(r'\b[A-Za-z_]\w*\s*\[[^\]\n]*:[^\]\n]*\]', code):
                if bank.start() <= column < bank.end():
                    try:
                        pins = expand_pin_banks(bank[0], line + 1)
                    except CircuitError:
                        return ''
                    return (f'{bank[0]}\nPin bank: {pins}.\n'
                            'Expands to separate numbered scalar pins in descending, inclusive order. '
                            'Use [width] without a colon to declare one vector signal instead.')
        main = DIGITAL_MAIN.match(code.lstrip())
        if main and column <= len(code) - len(code.lstrip()) + main.end():
            return (
                "main [Name] { ... }\n"
                "Assemble circuit components or behavioral digital chips. "
                "Conditions and loops here run during circuit generation; they do not run on clock edges. "
                "A named digital main sets the exported top-level module name."
            )
        timed = timed_block_at(code, column)
        if timed == "clock":
            return (
                "clock(input_pin, frequency) { ... }\n"
                "Update state on each rising edge of the input clock. The frequency "
                "annotation describes that external clock for Hz increments and every timers; "
                "it does not generate a clock. Assignments read the previous state."
            )
        if timed == "every":
            return (
                "every(interval) { ... }\n"
                "Run these updates periodically inside a clock block with a declared frequency. "
                "The compiler creates a counter; the first update follows one full interval. "
                "For example, toggling a state every(500ms) gives a one-second on/off cycle."
            )
        if CONNECTOR.fullmatch(code.strip()) and column <= code.lower().index("connector") + 9:
            return "connector(pin, ...)\nShare these nets by name with files in the same build."
        if INCLUDE.fullmatch(code.strip()):
            return 'include("74HC00")\nLoad a bundled chip by name, or use a relative .ckt or .txt file path.'
        if code.strip().casefold() == "@override":
            return (
                "Put @Override above a built-in gate definition. Calls with the same "
                "number of pins use your gate in this file."
            )
        for match in IDENTIFIER.finditer(code):
            if match.start() <= column <= match.end():
                name = match[0]
                if OP.fullmatch(name):
                    return (
                        f"{name}(in+, in-, out)\n"
                        "1. Non-inverting input (+)\n"
                        "2. Inverting input (-)\n"
                        "3. Output\n\n"
                        "An op amp drives its output in response to the difference between "
                        "its inputs. With negative feedback, this ideal model keeps the two "
                        "input voltages equal and draws zero input current. Connect out "
                        "back to in- to make a voltage follower."
                    )
                method = self.method_signatures().get(name.casefold())
                if method:
                    kind, declared, pins = method
                    return f"{declared}({pins})\nCall this {kind} section with wires in the listed order."
                if name == "sevenseg" and re.match(r"\s*\(", code[match.end():]):
                    return (
                        "sevenseg(value, [point])\n"
                        "Decode the lowest 4 bits as a hexadecimal digit (0-F). Returns an 8-bit "
                        "active-low segment pattern: bit 0 = a, 1 = b, 2 = c, 3 = d, 4 = e, "
                        "5 = f, 6 = g, 7 = decimal point. A zero lights a segment. "
                        "The decimal point is off unless the optional point expression is nonzero. "
                        "For example: HEX0[7:0] = sevenseg(SW[3:0]);"
                    )
                if name.upper().startswith("Z") and re.match(r"\s*(?:\([^()]*\)\s*)?between\s*\(", code[match.end():], re.I):
                    return (
                        f"{name}(R + jX) between(positive, negative)\n"
                        "Theoretical complex impedance in ohms for AC phasor analysis. "
                        "R is resistance; X is reactance (positive inductive, negative capacitive). "
                        "This fixed value does not vary with frequency. "
                        "Omit the value parentheses for an unspecified schematic block; fill in a value before numerical analysis. "
                        "Use physical R/L/C parts for DC, transient simulation, or SPICE export."
                    )
                if name.upper().startswith("P") and re.match(r"\s*(?:\([^()]*\)\s*)?between\s*\(", code[match.end():], re.I):
                    return (
                        f"{name}(resistance, position) between(high, wiper, low)\n"
                        "Three-terminal linear potentiometer. Resistance is the total resistance in ohms. "
                        "Position is measured from low: 0% connects the wiper to low, and 100% to high. "
                        "For example, 10k,25% gives 2.5k ohms from wiper to low and 7.5k ohms from high to wiper. "
                        "Omit position for the 50% midpoint. Omit the value parentheses for an unspecified schematic part. "
                        "Right-click the rendered part and choose Edit value to change resistance and position together."
                    )
                if TRANS_CALL.fullmatch(name) and re.match(r"\s*\(", code[match.end():]):
                    return (
                        f"{name}(base, collector, emitter, npn or pnp, [model])\n"
                        "1. Base controls conduction.\n"
                        "2. Collector and 3. emitter carry the main current.\n"
                        "4. Choose npn or pnp. A fifth argument can name a SPICE model."
                    )
                if name.casefold() in GATE_HELP and re.match(r"\s*\(", code[match.end():]):
                    key = name.casefold()
                    return (
                        f"{gate_call_help(key)}\n"
                        "Omit ground to use the enclosing gnd pin or 0. "
                        "Group multiple inputs on a variable-input gate to omit ground.\n"
                        f"Built-in behavior: {GATE_HELP[key]} This gate expands to transistor circuitry."
                    )
                break
        return ""

    def device_at(self, line, column):
        location = self.position(line, column)
        if location is None:
            return None
        text, column = location
        code = text.partition("#")[0]
        if timed_block_at(code, column):
            return None
        start = max(code.rfind(mark, 0, column) for mark in ";{}") + 1
        statement = re.split(r"[;{}]", code[start:], maxsplit=1)[0]
        column -= start + len(statement) - len(statement.lstrip())
        declaration = SECTION.fullmatch(statement.strip() + " {") if (
            start + len(statement) < len(code) and code[start + len(statement)] == "{"
        ) else None
        call = CALL.fullmatch(statement.strip())
        match = declaration or call
        if not match:
            return None
        if declaration:
            kind, name, raw = match.groups()
            if kind.lower() not in {"gate", "chip"} or not 0 <= column <= match.end(2):
                return None
            raw = _pin_parameters(kind, raw, line + 1)
            return kind, name, tuple(pin.strip() for pin in (raw or "").split(",") if pin.strip())
        name, raw = match.groups()
        if not 0 <= column < len(name):
            return None
        method = self.method_signatures().get(name.casefold())
        if method and method[0] in {"chip", "gate"}:
            kind, name, pins = method
            return kind, name, tuple(pin.strip() for pin in pins.split(",") if pin.strip())
        if name.casefold() in GATE_HELP and not method:
            return "builtin", name.upper(), gate_inputs(name.casefold(), raw)
        return None

    def wire_at(self, line, column):
        location = self.position(line, column)
        if location is None:
            return None
        text, column = location
        code = text.partition("#")[0]
        if timed_block_at(code, column):
            return None
        start = max(code.rfind(mark, 0, column) for mark in ";{}") + 1
        code, column = re.split(r"[;{}]", code[start:], maxsplit=1)[0], column - start
        match = re.fullmatch(
            r"\s*([A-Za-z][\w]*)\s*(?:(?:\([^()]*\)\s*)?between\s*)?"
            r"\(((?:[^()]|\([^()]*\))*)\)\s*", code, re.I,
        )
        if match:
            # Parenthesized gate inputs flatten into the compiler's arguments.
            # Whitespace between a comma and '(' is not an extra argument.
            tokens = (token for token in re.finditer(r"[^,()]+", match[2]) if token[0].strip())
            for index, token in enumerate(tokens):
                first = match.start(2) + token.start() + len(token[0]) - len(token[0].lstrip())
                last = match.start(2) + token.start() + len(token[0].rstrip())
                if first <= column < last:
                    return line + 1, match[1], index, token[0].strip()
        else:
            match = re.match(r"\s*([RCLZVI][\w]*)\s+(\S+)\s+(\S+)\s+\S", code, re.I)
            if match:
                for index in (0, 1):
                    if match.start(index + 2) <= column < match.end(index + 2):
                        return line + 1, match[1], index, match[index + 2]
        return None
