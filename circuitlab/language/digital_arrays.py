"""Lower locally declared digital state arrays to addressable word registers."""

from dataclasses import dataclass
import re

from circuitlab.exporters.clocked import TOKEN
from circuitlab.language.control_flow import MAX_EXPANDED_ROWS
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.model.graph import CircuitError


NAME = r"[A-Za-z_][A-Za-z0-9_]*"
IDENTIFIER = re.compile(NAME)
DECIMAL = re.compile(r"[0-9]+(?:_[0-9]+)*")
DECLARATION = re.compile(
    rf"({NAME})\s*\[([^\[\]]*)\]\s*\[([^\[\]]*)\]\s*(?:=(?!=)\s*(.+))?"
)
SIGNAL_DECLARATION = re.compile(r"(.+?)\s*=\s*(input|output|wire)", re.I)
ASSIGNMENT = re.compile(r"(<<=|>>=|[+\-&|^]=|=(?!=))\s*(.+)")
MAX_ARRAY_WORDS = 256
MAX_ARRAY_BITS = 65_536
MAX_SELECTOR_DEPTH = 64
MUX_TOKEN_BUDGET = 240


@dataclass(frozen=True)
class _Array:
    words: tuple[str, ...]
    width: int
    initial: int


def _brackets(text, start, line):
    """Find one selection, allowing a vector slice inside its index."""
    depth = 1
    for end in range(start + 1, len(text)):
        token = text[end]
        depth += (token == "[") - (token == "]")
        if depth > MAX_SELECTOR_DEPTH:
            raise CircuitError("Array indexing is nested too deeply.", line)
        if depth == 0:
            index = text[start + 1:end].strip()
            if not index:
                raise CircuitError("Select an array word with a nonempty index.", line)
            return index, end + 1
    raise CircuitError("Close the array selection with ].", line)


def _skip_space(text, start):
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def lower_digital_arrays(rows, pins=()):
    """Lower name[count][width] state and its selections, retaining row origins."""
    # The ordinary digital declarations and literals use the exporter's
    # validators. Import lazily because that exporter also calls this pass.
    from circuitlab.exporters.verilog import MAX_STATE_WIDTH, _name, _unsigned_decimal

    rows = list(rows)
    declarations, counts = {}, {}
    depth = 0
    for position, row in enumerate(rows):
        text, line = row[:2]
        if depth == 0 and (match := DECLARATION.fullmatch(text)):
            name = _name(match[1], line)
            if name in counts:
                # A second selector on an existing array may be a word
                # bit/slice assignment. Let the clock-only write rule report it.
                index = match[2].strip()
                if (match[4] is not None and DECIMAL.fullmatch(index)
                        and _unsigned_decimal(index, line, "array index") < counts[name]):
                    continue
            if name in pins or name in counts:
                raise CircuitError(f'Array "{name}" must have a unique local state name.', line)
            count = _unsigned_decimal(match[2].strip(), line, "array word count")
            width = _unsigned_decimal(match[3].strip(), line, "array word width")
            if not 1 <= count <= MAX_ARRAY_WORDS:
                raise CircuitError(f"Array word count must be between 1 and {MAX_ARRAY_WORDS}.", line)
            if not 1 <= width <= MAX_STATE_WIDTH or count * width > MAX_ARRAY_BITS:
                raise CircuitError(
                    f"Array words must have 1 to {MAX_STATE_WIDTH} bits, with at most {MAX_ARRAY_BITS} bits per array.",
                    line,
                )
            initial = _unsigned_decimal(match[4].strip(), line, "array initializer") if match[4] else 0
            if initial.bit_length() > width:
                raise CircuitError(f'Initial value does not fit the {width}-bit words of "{name}".', line)
            declarations[position] = name, count, width, initial
            counts[name] = count
        depth += text.endswith("{") - (text == "}")
    if not declarations:
        return rows

    # Reserve references as well as declarations, so an undeclared user name
    # cannot accidentally start resolving after a compiler name is allocated.
    occupied = set(pins)
    for text, line, *_ in rows:
        occupied.update(IDENTIFIER.findall(text))
        if match := SIGNAL_DECLARATION.fullmatch(text):
            # An explicit bank declaration also reserves the scalar names
            # implied by its range, even if none of them is spelled out.
            for signal in expand_pin_banks(match[1], line).split(","):
                declared = signal.strip().partition("[")[0].rstrip()
                if declared in counts:
                    raise CircuitError(f'Array "{declared}" conflicts with a signal declaration.', line)
                occupied.add(declared)

    def reserve(name):
        while name in occupied:
            name = "ooc_" + name
        occupied.add(name)
        return name

    arrays = {}
    for name, count, width, initial in declarations.values():
        words = tuple(reserve(f"{name}_word{index}") for index in range(count))
        arrays[name] = _Array(words, width, initial)

    lowered, helpers, reads = [], [], {}
    helper_count = 0

    def emit(target, text, row):
        if len(lowered) + len(helpers) >= MAX_EXPANDED_ROWS:
            raise CircuitError(f"Array lowering exceeds {MAX_EXPANDED_ROWS:,} digital statements.", row[1])
        target.append((text, *row[1:]))

    def literal_index(name, index, line):
        if not DECIMAL.fullmatch(index):
            return None
        value = _unsigned_decimal(index, line, "array index")
        if value >= len(arrays[name].words):
            raise CircuitError(f'Array index {value} is outside "{name}" (0..{len(arrays[name].words) - 1}).', line)
        return value

    def read(name, index, row):
        words = arrays[name].words
        if (constant := literal_index(name, index, row[1])) is not None:
            return words[constant]
        index = rewrite(index, row)
        key = name, index
        if key in reads:
            return reads[key]

        def temporary(expression):
            nonlocal helper_count
            helper_count += 1
            temporary_name = reserve(f"ooc_array_read_{helper_count}")
            emit(helpers, f"{temporary_name}[{arrays[name].width}] = wire", row)
            emit(helpers, f"{temporary_name} = {expression}", row)
            return temporary_name

        def mux(start, stop):
            if stop - start == 1:
                return words[start]
            middle = (start + stop) // 2
            return select(middle, mux(start, middle), mux(middle, stop))

        def select(bound, below, above):
            expression = f"(({index}) < {bound} ? {below} : {above})"
            # Keep small mux trees in a single expression. Break larger ones
            # into ordinary word-width wires before the digital token limit.
            if len(TOKEN.findall(expression)) > MUX_TOKEN_BUDGET:
                below = below if IDENTIFIER.fullmatch(below) else temporary(below)
                above = above if IDENTIFIER.fullmatch(above) or above == "0" else temporary(above)
                expression = f"(({index}) < {bound} ? {below} : {above})"
            return expression

        result = temporary(select(len(words), mux(0, len(words)), "0"))
        reads[key] = result
        return result

    def rewrite(text, row):
        cursor, parts = 0, []
        while match := IDENTIFIER.search(text, cursor):
            start, end = match.span()
            name = match[0]
            opening = _skip_space(text, end)
            parts.append(text[cursor:start])
            invocation = text[opening:opening + 1] == "(" and (start == 0 or name == "sevenseg")
            if name not in arrays or invocation:
                parts.append(name)
                cursor = end
                continue
            if text[opening:opening + 1] != "[":
                raise CircuitError(f'Select one word of array "{name}" with {name}[index].', row[1])
            index, cursor = _brackets(text, opening, row[1])
            parts.append(read(name, index, row))
        parts.append(text[cursor:])
        return "".join(parts)

    def write(match, row, in_clock):
        text, line = row[:2]
        name = match[0]
        opening = _skip_space(text, match.end())
        if text[opening:opening + 1] != "[":
            return False
        index, end = _brackets(text, opening, line)
        selection = ""
        next_token = _skip_space(text, end)
        if text[next_token:next_token + 1] == "[":
            _, end = _brackets(text, next_token, line)
            selection = text[next_token:end]
        assignment = ASSIGNMENT.fullmatch(text[end:].lstrip())
        if assignment is None:
            return False
        if not in_clock:
            raise CircuitError("Assign array words inside clock(...).", line)
        operator, expression = assignment.groups()
        expression = rewrite(expression, row)
        words = arrays[name].words
        if (constant := literal_index(name, index, line)) is not None:
            emit(lowered, f"{words[constant]}{selection} {operator} {expression}", row)
            return True

        # Guards read the same pre-edge index as native clock assignments.
        # Keep all writes in their original order so the last matching write
        # wins and an out-of-range index never modifies a word.
        index = rewrite(index, row)
        for index_value, word in enumerate(words):
            emit(lowered, f"if (({index}) == {index_value}) {{", row)
            emit(lowered, f"{word}{selection} {operator} {expression}", row)
            emit(lowered, "}", row)
        return True

    depth, in_clock = 0, False
    for position, row in enumerate(rows):
        text = row[0]
        if position in declarations:
            array = arrays[declarations[position][0]]
            for word in array.words:
                emit(lowered, f"{word}[{array.width}] = {array.initial}", row)
        elif not ((match := IDENTIFIER.match(text)) and match[0] in arrays and write(match, row, in_clock)):
            emit(lowered, rewrite(text, row), row)
        if depth == 0 and re.match(r"clock\s*\(", text) and text.endswith("{"):
            in_clock = True
        depth += text.endswith("{") - (text == "}")
        if depth == 0:
            in_clock = False
    return helpers + lowered
