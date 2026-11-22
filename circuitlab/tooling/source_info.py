"""Inspect incomplete OOC source for editors without constructing a Qt widget.

Locations use zero-based lines and UTF-16 columns, matching both VS Code and
QTextCursor. Wire locations retain the compiler's one-based source-line numbers.
This tolerant inspection describes source; it does not compile or simulate it.
"""
# Editor inspection must remain useful while a document is syntactically incomplete.
# Recognize names and signatures without running the compiler, and translate UTF-16 editor
# columns carefully so non-ASCII characters do not shift hover or navigation targets.

import re
from functools import cached_property

from circuitlab.model.diode import DIODE_NAME
from circuitlab.language.comments import mask_comments
from circuitlab.language.control_flow import is_control_block
from circuitlab.language.wire_counts import COUNT_REFERENCE, is_count_reference
from circuitlab.language.digital_declarations import INLINE_WIRE, normalize_wire_declarations
from circuitlab.language.gates import GATE_HELP, gate_accepts, gate_call_help
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.model.graph import CircuitError
from circuitlab.model.properties import MODEL_PROPERTIES
from circuitlab.language.opamps import MODEL_ASSIGNMENT
from circuitlab.language.properties import PROPERTIES_BRACE, PROPERTY_DECLARATION, declared_property_value
from circuitlab.language.syntax import (
    CALL, CONNECTOR, DEFAULT_TERMINALS, DIGITAL_MAIN, INCLUDE, MAIN, MARKER, OP, SECTION,
    TRANS_CALL, source_rows,
)


IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")
TIMED_BLOCK = re.compile(r"(?:^|(?<=[;{}]))\s*(clock|every)\s*\([^()]*\)\s*(?=\{)")
WIRE_COUNT_DECLARATION = re.compile(r"(?:^|(?<=[;{}]))\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(count)\b(?=\s*(?:[;}]|$))", re.I)

STATE_FUNCTION_HELP = {
    "integrate": "integrate(rate, [initial])\nBackward-Euler integration in native time simulation. The initial value defaults to 0; time as the second argument also selects 0. History advances only after an accepted electrical step. DC holds the initial value.",
    "delay": "delay(value, duration)\nInterpolated transport delay with a constant nonnegative duration. The first observed value initializes history; changing duration requires Reset. delay(value, rise, fall) is the Boolean transition form.",
    "transition": "transition(condition, rise, fall)\nInertial Boolean latency with constant nonnegative durations. A pending change is cancelled if the condition returns before its deadline. The first observed level initializes immediately; choose a smaller simulation step than the latency.",
    "square": "square(phase, duty)\nReturn 1 while the fractional phase (in cycles) is below duty, otherwise 0. Use integrate(frequency, time) to accumulate oscillator phase.",
}


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

    @cached_property
    def code_lines(self):
        # Keep the original rows for UTF-16 position conversion. A masked
        # comment may include astral characters, but regex offsets use Python.
        return mask_comments(self.source).split("\n")

    # VS Code and QTextCursor count UTF-16 code units, while Python indexes Unicode code points.
    # Reject a position inside a surrogate pair rather than selecting the wrong token.
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

    def code_position(self, line, column):
        location = self.position(line, column)
        if location is None:
            return None
        text, index = location
        code = self.code_lines[line]
        if index < len(text) and text[index] != code[index]:
            return None
        return code, index

    # Reread included signatures on demand so changed library declarations are visible
    # without treating an old hover/completion cache as current source.
    def method_signatures(self):
        signatures = {}
        if self.read_imports and "include" in self.source.casefold():
            for included in self.read_imports(self.source):
                signatures.update(methods(included))
        signatures.update(methods(self.source))
        return signatures

    def custom_property_help(self, name, target_line):
        """Describe the property in this enclosing method, without solving it."""
        depth, owner, target_owner, property_depth = 0, None, None, None
        declarations = {}
        for text, line, _ in source_rows(self.source):
            if depth == 0:
                owner = line if SECTION.fullmatch(text) or MAIN.fullmatch(text) else None
            if line <= target_line + 1:
                target_owner = owner
            if PROPERTIES_BRACE.fullmatch(text):
                property_depth = depth + 1
            elif declaration := PROPERTY_DECLARATION.fullmatch(text):
                declarations.setdefault((owner, declaration[1]), (declaration[3], declaration[2]))
            elif property_depth is not None and (assignment := MODEL_ASSIGNMENT.fullmatch(text)):
                declarations.setdefault((owner, assignment[1]), (assignment[2], None))
            elif text == "}" and depth == property_depth:
                property_depth = None
            depth += int(text.endswith("{")) - int(text == "}")
        declaration = declarations.get((target_owner, name))
        if declaration is None:
            return ""
        raw, annotation = declaration
        try:
            _, unit = declared_property_value(raw, annotation)
        except CircuitError:
            unit = "incomplete value"
        return f"{name} = {raw}\nCustom model property; inferred unit: {unit or 'dimensionless'}. Local to each chip/method instance."

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
        source_column = column
        location = self.code_position(line, column)
        if location is None:
            return ""
        code, column = location
        # Describe syntax without executing generation or advancing a counter.
        # Actual net names come from the compiler-backed wire trace instead.
        for declaration in WIRE_COUNT_DECLARATION.finditer(code):
            if declaration.start(1) <= column <= declaration.end(2):
                return (f"{declaration[1]} = count;\n"
                        "Declare an independent wire counter in electrical main, initially 0. "
                        f"{declaration[1]}* advances it before using the number; "
                        f"{declaration[1]}^ reuses its current number. "
                        "Counters expand left to right during circuit generation and cannot be assigned manually.")
        wire = self.wire_at(line, source_column)
        if wire and is_count_reference(wire[3]):
            reference = COUNT_REFERENCE.fullmatch(wire[3])
            prefix, operation, counter = (reference[group] for group in ("prefix", "operator", "counter"))
            if operation == "*":
                action = f"Advance counter {counter or prefix}, then use {prefix} plus its new number (first advance gives {prefix}1)."
            elif counter:
                action = f"Reuse counter {counter}'s current number without advancing it; initially this is {prefix}0."
            else:
                action = (f"Reuse counter {prefix}'s current number without advancing it. "
                          f"If {prefix} is not a declared counter, reuse the last counter used with that prefix.")
            return f"{wire[3]}\n{action}\nWire counters apply to electrical main connections; numerical expressions retain their ordinary operators."
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
                "A named digital main sets the exported top-level module name. "
                "Use wi = count; with wi* advancing and wi^ reusing a generated wire number."
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
        diode = re.search(rf"\b({DIODE_NAME})(?=\s*(?:\(|between\b))", code, re.I)
        if (diode and diode.start(1) <= column <= diode.end(1)
                and ("." in diode[1] or re.match(r"\s*(?:\([^()]*\)\s*)?between\b", code[diode.end(1):], re.I))):
            return (f"{diode[1]} between(anode, cathode)\n"
                    "The first wire is the anode; the second is the cathode. Generic room-temperature model. "
                    "Types: rect, Tun, Schot, Zener, LED, PIN, Varactor, Photo, Avalanche. "
                    "Optional forward voltage specifies the PN branch at 1 mA; Zener/Avalanche voltage sets breakdown. "
                    "Overrides: is, n, rs, bv, ibv, cj, vj, m, light, ip, vp.")
        if code.strip().casefold() == "@override":
            return (
                "Put @override above a built-in gate definition, an analog/digital block, "
                "or chip Name { ... } to patch an existing chip's model without changing its pins. "
                "Individual model targets take precedence over grouped targets; conflicting equal-priority settings are errors."
            )
        for match in IDENTIFIER.finditer(code):
            if match.start() <= column <= match.end():
                name = match[0]
                custom_help = self.custom_property_help(name, line)
                if custom_help:
                    return custom_help
                if name.casefold() == "properties" and re.match(r"\s*\{", code[match.end():]):
                    return "properties { name = value; ... }\nDeclare custom model constants with inferred SI units. Use them in analog/digital equations; @override can patch their values."
                if name.casefold() == "property":
                    return "property name = value; or property name: V = value;\nDeclare a local model constant. Units are inferred unless an explicit base SI unit or int/float/bool type is supplied."
                if name in STATE_FUNCTION_HELP and re.match(r"\s*\(", code[match.end():]):
                    return STATE_FUNCTION_HELP[name]
                property_name = name.casefold().replace("_", ".")
                metadata = next((value for key, value in MODEL_PROPERTIES.items() if key.casefold() == property_name), None)
                if metadata:
                    prefix = '\n'.join([*self.code_lines[:line], code[:match.start()]])
                    if (re.search(r'(?:opamp|analog(?:\s*:\s*math)?)\s+(?:[A-Za-z_][A-Za-z0-9_]*|\([^()]*\))\s*\{[^{}]*$', prefix, re.I)
                            or (property_name == "supply.current.total" and re.search(r'analog\s*\{[^{}]*$',prefix,re.I))):
                        return metadata.description
                if name.casefold() in {"analog", "digital"} and re.match(r"\s*(?::\s*math\b|\{|\(|[A-Za-z_])", code[match.end():], re.I):
                    return ("Optional mathematical behavior: equations affect simulation, not schematic symbols. "
                            "analog V(out)=V(in)*10; or analog { ... }. "
                            "analog op1 { ... } or analog (op1,op2) { ... } configures existing amplifiers. "
                            "digital { ... } defines logic; "
                            "electrical voltage comparisons produce 0/1 V. Existing chip/circuit/main syntax is unchanged.")
                if name.casefold() == "vneg" and re.match(r"\s*=", code[match.end():]):
                    prefix = '\n'.join([*self.code_lines[:line], code[:match.start()]])
                    prefix = re.sub(r'\(\s*end\s+battery\s*\)', '}', prefix, flags=re.I)
                    if re.search(r'(?:battery\s+[A-Za-z][A-Za-z0-9_]*\s*\{|\(\s*battery\b[^()]*\))[^{}]*$', prefix, re.I):
                        return ("Vneg = 5;\nNegative battery supply: sets this battery to -5 V. "
                                "Both 5 and -5 mean -5 V. Use V or Vneg once per battery block; "
                                "the wire and ground names keep their usual order.")
                if OP.fullmatch(name):
                    return (
                        f"{name}(in+, in-, out)\n"
                        f"{name}(in+, in-, out, vplus, vminus)\n"
                        f"{name}(in+, in-, out, vplus, vminus, gain1, gain8, bypass)\n"
                        "1. Non-inverting input (+)\n"
                        "2. Inverting input (-)\n"
                        "3. Output\n\n"
                        "The eight-argument form selects the audio model: default gain 20, maximum gain 200, "
                        "midrail output bias, and gain/bypass contacts. Configure it with analog " + name + " { ... }.\n\n"
                        "Optionally add both positive and negative supply wires as pins 4 and 5. "
                        "They appear above and below the triangle. Supply pins identify connections; "
                        "this ideal model does not simulate rail clipping.\n\n"
                        f"Optionally declare opamp {name}; for ideal defaults, or use "
                        f"opamp {name} {{ gain = 19e6; bandwidth = 7.2MHz; slew = 2.5V/us; "
                        "offset = 20uV; supply.current = 2.75mA; } to override individual defaults.\n\n"
                        "An op amp drives its output in response to the difference between "
                        "its inputs. With negative feedback, the default ideal model keeps the two "
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
                if name.upper().startswith("S") and re.match(
                    r"\s*(?:(?:\([^()]*\)\s*)?between\s*\(|"
                    r"=\s*(?:open|closed|on|off|[01])\b|"
                    r"\(\s*(?:open|closed|on|off|[01])?\s*\)\s*(?=[;}]|$))",
                    code[match.end():], re.I,
                ):
                    return (f"{name}(open) between(a, b)\nManual switch with two terminals. "
                            "Use open/closed, off/on, or 0/1; omitting the state means open. "
                            f"Inside a series or parallel group, write {name}(open); to use the group's wiring. "
                            "Click the switch in the schematic to toggle it and update the source; Undo restores its previous state.")
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
        location = self.code_position(line, column)
        if location is None:
            return None
        code, column = location
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

    # Return the compiler-style source line, call name, terminal index, and wire spelling.
    # The hover renderer uses that identity to trace the selected physical connection.
    def wire_at(self, line, column):
        location = self.code_position(line, column)
        if location is None:
            return None
        code, column = location
        if timed_block_at(code, column):
            return None
        start = max(code.rfind(mark, 0, column) for mark in ";{}") + 1
        code, column = re.split(r"[;{}]", code[start:], maxsplit=1)[0], column - start
        match = re.fullmatch(
            rf"\s*({DIODE_NAME}|[A-Za-z][\w]*)\s*(?:(?:\([^()]*\)\s*)?between\s*)?"
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
            match = re.match(r"\s*([RCLZVIS][\w]*)\s+(\S+)\s+(\S+)\s+\S", code, re.I)
            if match:
                for index in (0, 1):
                    if match.start(index + 2) <= column < match.end(index + 2):
                        return line + 1, match[1], index, match[index + 2]
        return None
