"""Typed unsigned expressions and rising-edge blocks for the digital exporter."""

import re
from fractions import Fraction

from circuitlab.model.graph import CircuitError

TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[0-9]+(?:_[0-9]+)*|==|!=|<=|>=|&&|\|\||[!+\-&^|<>()\[\]])")
PRECEDENCE = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5,
              "==": 6, "!=": 6, "<": 7, "<=": 7, ">": 7, ">=": 7,
              "+": 8, "-": 8}
DECIMAL = r"[0-9]+(?:_[0-9]+)*(?:\.[0-9]+(?:_[0-9]+)*)?"
FREQUENCY = rf"({DECIMAL})\s*(Hz|kHz|MHz|GHz)"


def interval_cycles(text, rate, line):
    if rate is None:
        raise CircuitError("Declare the input frequency with clock(clk, 50MHz) before using every(...).", line)
    match = re.fullmatch(rf"({DECIMAL})\s*(s|ms|us|µs|μs|ns)", text.strip())
    if not match or len(match[1]) > 128:
        raise CircuitError("Use a positive interval such as every(500ms), every(1s) or every(2.5us).", line)
    duration = Fraction(match[1].replace("_", "")) / {"s": 1, "ms": 1000, "us": 1000000, "µs": 1000000, "μs": 1000000, "ns": 1000000000}[match[2]]
    cycles = duration * rate
    if cycles < 1:
        raise CircuitError("The every interval must be at least one input clock period.", line)
    return (2 * cycles.numerator + cycles.denominator) // (2 * cycles.denominator)


def frequency(text, line):
    """Parse physical frequency exactly; never round through a float."""
    match = re.fullmatch(FREQUENCY, text.strip())
    if not match or len(match[1]) > 128:
        raise CircuitError("Use a frequency such as 440Hz, 1.5kHz or 50MHz.", line)
    return Fraction(match[1].replace("_", "")) * {"Hz": 1, "kHz": 1000, "MHz": 1000000, "GHz": 1000000000}[match[2]]


def phase_increment(expression, target, states, rate, line):
    """Frequency literals describe a self-incrementing phase accumulator only."""
    if not re.search(rf"(?<![A-Za-z0-9_]){FREQUENCY}\b", expression):
        return expression
    match = re.fullmatch(rf"{re.escape(target)}\s*\+\s*({FREQUENCY})", expression)
    if not match:
        raise CircuitError("Use phase += 440Hz; or phase = phase + 440Hz; for a frequency-based phase increment.", line)
    if rate is None:
        raise CircuitError("Declare the input frequency with clock(clk, 50MHz) before using Hz increments.", line)
    width, _, vector = states[target]
    if not vector or width < 2:
        raise CircuitError("Frequency increments require a phase vector of at least two bits.", line)
    desired = frequency(match[1], line)
    if desired > rate / 2:
        raise CircuitError("Output frequency cannot exceed half the phase update frequency (including every intervals).", line)
    scaled = desired * (1 << width) / rate
    step = (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)
    if desired and step == 0:
        raise CircuitError("Frequency is too small for this phase width; increase the vector width.", line)
    return f"{target} + {step}"


class Expressions:
    def __init__(self, signals):
        self.signals = signals
        self.names = set(signals)
        self.wires = []
        self.count = 0

    def name(self, prefix):
        while True:
            name = f"{prefix}_{self.count}"
            self.count += 1
            if name not in self.names:
                break
        self.names.add(name)
        return name

    def temporary(self, code, width):
        name = self.name("ooc_expr")
        self.wires.append(f"    wire [{width - 1}:0] {name} = {code};")
        return name, width

    def parse(self, text, line):
        self.line = line
        self.tokens = []
        offset = 0
        text = text.strip()
        while offset < len(text):
            match = TOKEN.match(text, offset)
            if not match:
                raise CircuitError("Use unsigned signals, literals, bit reads, +, -, comparisons or Boolean operators.", line)
            self.tokens.append(match[1])
            offset = match.end()
        if len(self.tokens) > 256:
            raise CircuitError("Digital expression is too long.", line)
        self.index = 0
        value = self.binary(1)
        if self.index != len(self.tokens):
            raise CircuitError("Unexpected token in digital expression.", line)
        return value

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self):
        token = self.peek()
        if token is None:
            raise CircuitError("Complete the digital expression.", self.line)
        self.index += 1
        return token

    def primary(self):
        token = self.take()
        if token == "!":
            code, _ = self.primary()
            return self.temporary(f"!{code}", 1)
        if token == "(":
            value = self.binary(1)
            if self.take() != ")":
                raise CircuitError("Close the expression parentheses.", self.line)
            return value
        if token[0].isdigit():
            digits = token.replace("_", "").lstrip("0") or "0"
            if len(digits) > 1234:
                raise CircuitError("Digital literal exceeds 4096 bits.", self.line)
            value = int(digits)
            width = max(1, value.bit_length())
            if width > 4096:
                raise CircuitError("Digital literal exceeds 4096 bits.", self.line)
            return f"{width}'d{value}", width
        if token not in self.signals:
            raise CircuitError(f'Unknown digital signal "{token}".', self.line)
        width = self.signals[token]
        if self.peek() == "[":
            self.take()
            bit = self.take()
            if not re.fullmatch(r"[0-9]+(?:_[0-9]+)*", bit) or len(bit) > 16:
                raise CircuitError("Use a constant nonnegative bit index.", self.line)
            index = int(bit.replace("_", ""))
            if self.take() != "]" or width is None or index >= width:
                raise CircuitError("Bit index must select a bit inside a declared vector.", self.line)
            return f"{token}[{index}]", 1
        return token, width or 1

    def binary(self, minimum):
        left, left_width = self.primary()
        while PRECEDENCE.get(self.peek(), 0) >= minimum:
            op = self.take()
            right, right_width = self.binary(PRECEDENCE[op] + 1)
            width = 1 if op in {"==", "!=", "<", "<=", ">", ">=", "&&", "||"} else max(left_width, right_width)
            left, left_width = self.temporary(f"({left} {op} {right})", width)
        return left, left_width


def compile_clocks(clocks, signals, states, directions):
    expressions = Expressions(signals)
    blocks = []
    timers = []
    owners = set()
    rates = {}
    for header, line, rows in clocks:
        match = re.fullmatch(r"clock\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*([^()]+))?\)\s*\{", header)
        if not match or directions.get(match[1]) != "input":
            raise CircuitError("Use clock(input_pin) or clock(input_pin, 50MHz) with a declared input pin.", line)
        clock = match[1]
        rate = frequency(match[2], line) if match[2] else None
        if rate is not None:
            if rate <= 0:
                raise CircuitError("Clock frequency must be positive.", line)
            if clock in rates and rates[clock] != rate:
                raise CircuitError(f'Conflicting frequencies for clock "{clock}".', line)
            rates[clock] = rate
        written = set()
        index = 0

        def conditional(indent, condition, number, update_rate):
            nonlocal index
            output, prefix = [], indent
            # Else-if chains are sequential syntax, not deeper source nesting.
            # Lower them iteratively so long generated chains cannot exhaust Python.
            while True:
                code, _ = expressions.parse(condition, number)
                output.append(f"{prefix}if ({code}) begin")
                output.extend(branch(indent + "    ", True, update_rate))
                output.append(f"{indent}end")
                if index >= len(rows) or not re.match(r"else\b", rows[index][0]):
                    break
                other, other_line = rows[index]
                index += 1
                if re.fullmatch(r"else\s*\{", other):
                    output.append(f"{indent}else begin")
                    output.extend(branch(indent + "    ", True, update_rate))
                    output.append(f"{indent}end")
                    break
                match = re.fullmatch(r"else\s+if\s*\((.*)\)\s*\{", other)
                if not match:
                    raise CircuitError("Use else { ... } or else if (...) { ... }.", other_line)
                prefix = f"{indent}else "
                condition, number = match[1], other_line
            return output

        def branch(indent, nested=False, update_rate=rate):
            nonlocal index
            output = []
            while index < len(rows):
                statement, number = rows[index]
                index += 1
                if statement == "}":
                    if not nested:
                        raise CircuitError("Unexpected closing brace in clock block.", number)
                    return output
                interval = re.fullmatch(r"every\s*\((.*)\)\s*\{", statement)
                if interval:
                    if nested:
                        raise CircuitError("Place every(...) directly inside a clock block; put conditions inside every instead.", number)
                    cycles = interval_cycles(interval[1], rate, number)
                    if cycles == 1:
                        output.extend(branch(indent, True, update_rate))
                        continue
                    width = (cycles - 1).bit_length()
                    timer = expressions.name("ooc_every")
                    timers.append(f"    reg [{width - 1}:0] {timer} = {width}'d0;")
                    output.append(f"{indent}if ({timer} == {width}'d{cycles - 1}) begin")
                    output.append(f"{indent}    {timer} <= {width}'d0;")
                    output.extend(branch(indent + "    ", True, update_rate / cycles))
                    output.extend([
                        f"{indent}end",
                        f"{indent}else begin",
                        f"{indent}    {timer} <= {timer} + {width}'d1;",
                        f"{indent}end",
                    ])
                    continue
                condition = re.fullmatch(r"if\s*\((.*)\)\s*\{", statement)
                if condition:
                    output.extend(conditional(indent, condition[1], number, update_rate))
                    continue
                assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*(\+=|=)\s*(.+)", statement)
                if not assignment or assignment[1] not in states:
                    raise CircuitError("Clock blocks assign declared state and support if / else and every blocks.", number)
                target = assignment[1]
                if target in owners:
                    raise CircuitError(f'State "{target}" is driven by more than one clock block.', number)
                expression = assignment[3].strip()
                if assignment[2] == "+=":
                    operand = expression if re.fullmatch(FREQUENCY, expression) else f"({expression})"
                    expression = f"{target} + {operand}"
                expression = phase_increment(expression, target, states, update_rate, number)
                code, _ = expressions.parse(expression, number)
                output.append(f"{indent}{target} <= {code};")
                written.add(target)
            if nested:
                raise CircuitError("Close the conditional block with }.", line)
            return output

        blocks.append(f"    always @(posedge {clock}) begin")
        blocks.extend(branch("        "))
        blocks.append("    end")
        owners.update(written)
    return timers, expressions.wires, blocks
