"""Typed unsigned expressions and rising-edge blocks for the digital exporter."""

import re
from fractions import Fraction

from circuitlab.model.graph import CircuitError

TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[0-9]+(?:_[0-9]+)*|==|!=|<=|>=|<<|>>|&&|\|\||[!+\-&^|<>()\[\]:?,])")
PRECEDENCE = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5,
              "==": 6, "!=": 6, "<": 7, "<=": 7, ">": 7, ">=": 7,
              "<<": 8, ">>": 8, "+": 9, "-": 9}
DECIMAL = r"[0-9]+(?:_[0-9]+)*(?:\.[0-9]+(?:_[0-9]+)*)?"
FREQUENCY = rf"({DECIMAL})\s*(Hz|kHz|MHz|GHz)"
SELECTOR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*([0-9]+(?:_[0-9]+)*)\s*(?::\s*([0-9]+(?:_[0-9]+)*)\s*)?\]")
# Active-low a,b,c,d,e,f,g,decimal-point bits; hexadecimal glyphs 0..F.
SEVEN_SEGMENTS = (192, 249, 164, 176, 153, 146, 130, 248,
                  128, 144, 136, 131, 198, 161, 134, 142)


def selected_bits(text, signals, line):
    """Resolve a vector selection or a bank of numbered scalar pins, low bit first."""
    match = SELECTOR.fullmatch(text)
    if not match or any(len(value or "") > 16 for value in match.groups()[1:]):
        raise CircuitError("Select a constant bit or descending slice: name[3] or name[7:0].", line)
    name, high, low = match.groups()
    high = int(high.replace("_", ""))
    low = high if low is None else int(low.replace("_", ""))
    if not 0 <= low <= high < 4096:
        raise CircuitError("Select bits in descending order within the 4096-bit limit.", line)
    if name in signals:
        width = signals[name]
        if width is None or high >= width:
            raise CircuitError(f'Bit selection is outside declared vector "{name}".', line)
        return [(name, bit) for bit in range(low, high + 1)]
    bits = [(f"{name}{bit}", None) for bit in range(low, high + 1)]
    if any(pin not in signals or signals[pin] is not None for pin, _ in bits):
        raise CircuitError(f'Declare vector "{name}" or each numbered scalar pin in "{text}".', line)
    return bits


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
        self._wires = {}
        self.additions = {}
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
        self._wires[name] = f"    wire [{width - 1}:0] {name} = {code};"
        return name, width

    def pack(self, bits):
        return self.temporary("{" + ", ".join(reversed(bits)) + "}", len(bits))

    @property
    def wires(self):
        return list(self._wires.values())

    def parse(self, text, line):
        self.line = line
        self.tokens = []
        offset = 0
        text = text.strip()
        while offset < len(text):
            match = TOKEN.match(text, offset)
            if not match:
                raise CircuitError("Use unsigned signals, literals, bit reads, +, -, shifts, comparisons or Boolean operators.", line)
            self.tokens.append(match[1])
            offset = match.end()
        if len(self.tokens) > 256:
            raise CircuitError("Digital expression is too long.", line)
        self.index = 0
        value = self.conditional()
        if self.index != len(self.tokens):
            raise CircuitError("Unexpected token in digital expression.", line)
        return value

    def conditional(self):
        condition, width = self.binary(1)
        if self.peek() != "?":
            return condition, width
        self.take()
        yes, yes_width = self.conditional()
        if self.take() != ":":
            raise CircuitError("Complete the conditional expression: condition ? yes : no.", self.line)
        no, no_width = self.conditional()
        return self.temporary(f"({condition} ? {yes} : {no})", max(yes_width, no_width))

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
        if token == "sevenseg" and self.peek() == "(":
            return self.seven_segment()
        if token == "!":
            code, _ = self.primary()
            return self.temporary(f"!{code}", 1)
        if token == "(":
            value = self.conditional()
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
        if self.peek() == "[":
            self.take()
            selection = self.take()
            if self.peek() == ":":
                selection += self.take() + self.take()
            if self.take() != "]":
                raise CircuitError("Close the bit selection with ].", self.line)
            bits = selected_bits(f"{token}[{selection}]", self.signals, self.line)
            if token in self.signals:
                low, high = bits[0][1], bits[-1][1]
                return f"{token}[{high}:{low}]" if high != low else f"{token}[{low}]", len(bits)
            return (bits[0][0], 1) if len(bits) == 1 else self.pack([name for name, _ in bits])
        if token not in self.signals:
            raise CircuitError(f'Unknown digital signal "{token}".', self.line)
        width = self.signals[token]
        return token, width or 1

    def seven_segment(self):
        """Decode the low nibble; optional nonzero point lights the decimal dot."""
        self.take()  # Opening parenthesis after sevenseg.
        if self.peek() in {None, ")", ","}:
            raise CircuitError("Use sevenseg(value) or sevenseg(value, point).", self.line)
        value, width = self.conditional()
        point = None
        if self.peek() == ",":
            self.take()
            if self.peek() in {None, ")", ","}:
                raise CircuitError("Give sevenseg a decimal-point expression after the comma.", self.line)
            point, _ = self.conditional()
        if self.peek() != ")":
            raise CircuitError("Use sevenseg(value) or sevenseg(value, point).", self.line)
        self.take()
        if width > 4:
            value, _ = self.temporary(value, width)
            value = f"{value}[3:0]"
        nibble, _ = self.temporary(value, 4)
        code = f"8'd{SEVEN_SEGMENTS[-1]}"
        for digit in range(14, -1, -1):
            condition, _ = self.temporary(f"({nibble} == 4'd{digit})", 1)
            code, _ = self.temporary(f"({condition} ? 8'd{SEVEN_SEGMENTS[digit]} : {code})", 8)
        if point is not None:
            mask, _ = self.temporary(f"({point} ? 8'd127 : 8'd255)", 8)
            code, _ = self.temporary(f"({code} & {mask})", 8)
        return code, 8

    def binary(self, minimum):
        left, left_width = self.primary()
        while PRECEDENCE.get(self.peek(), 0) >= minimum:
            op = self.take()
            right, right_width = self.binary(PRECEDENCE[op] + 1)
            width = (1 if op in {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}
                     else left_width if op in {"<<", ">>"} else max(left_width, right_width))
            operands = (left, left_width, right, right_width)
            left, left_width = self.temporary(f"({left} {op} {right})", width)
            if op == "+":
                self.additions[left] = operands
        return left, left_width

    def select_increment(self, condition, yes, no, indent):
        """Share the adder for two exclusive increments of the same state.

        Requiring the common operand to be the assignment target preserves
        modular truncation even when the two increments have different widths.
        Other assignments and conditionals retain their original lowering.
        """
        if len(yes) != 1 or len(no) != 1:
            return None
        first = re.fullmatch(r"\s*(\w+) <= (\w+);", yes[0])
        second = re.fullmatch(r"\s*(\w+) <= (\w+);", no[0])
        if first is None or second is None:
            return None
        target, add_yes, add_no = first[1], first[2], second[2]
        if second[1] != target:
            return None
        left, right = self.additions.get(add_yes), self.additions.get(add_no)
        if left is None or right is None or left[0] != target or right[0] != target:
            return None
        step_width = max(left[3], right[3])
        width = max(left[1], step_width)
        # Reuse the two reserved helper names so later timer identities stay
        # stable. Emit both after the operand helpers, with selection first.
        del self._wires[add_yes], self._wires[add_no]
        self._wires[add_no] = f"    wire [{step_width - 1}:0] {add_no} = ({condition} ? {left[2]} : {right[2]});"
        self._wires[add_yes] = f"    wire [{width - 1}:0] {add_yes} = ({target} + {add_no});"
        self.additions[add_yes] = (target, left[1], add_no, step_width)
        del self.additions[add_no]
        return [f"{indent}{target} <= {add_yes};"]


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
                yes = branch(indent + "    ", True, update_rate)
                output.extend(yes)
                output.append(f"{indent}end")
                if index >= len(rows) or not re.match(r"else\b", rows[index][0]):
                    break
                other, other_line = rows[index]
                index += 1
                if re.fullmatch(r"else\s*\{", other):
                    no = branch(indent + "    ", True, update_rate)
                    shared = (expressions.select_increment(code, yes, no, indent)
                              if prefix == indent else None)
                    if shared is not None:
                        return shared
                    output.append(f"{indent}else begin")
                    output.extend(no)
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
                assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*(?:\s*\[[^]]+\])?)\s*(<<=|>>=|[+\-&|^]=|=)\s*(.+)", statement)
                target = assignment[1].partition("[")[0].strip() if assignment else ""
                if not assignment or target not in states:
                    raise CircuitError("Clock blocks assign declared state and support if / else and every blocks.", number)
                selected = selected_bits(assignment[1], signals, number) if "[" in assignment[1] else None
                destination = (f"{target}[{selected[-1][1]}:{selected[0][1]}]" if selected else target)
                if target in owners:
                    raise CircuitError(f'State "{target}" is driven by more than one clock block.', number)
                expression = assignment[3].strip()
                if assignment[2] != "=":
                    operator = assignment[2][:-1]
                    operand = expression if re.fullmatch(FREQUENCY, expression) else f"({expression})"
                    expression = f"{destination} {operator} {operand}"
                expression = phase_increment(expression, target, states, update_rate, number)
                code, _ = expressions.parse(expression, number)
                output.append(f"{indent}{destination} <= {code};")
                written.add(target)
            if nested:
                raise CircuitError("Close the conditional block with }.", line)
            return output

        blocks.append(f"    always @(posedge {clock}) begin")
        blocks.extend(branch("        "))
        blocks.append("    end")
        owners.update(written)
    return timers, expressions.wires, blocks
