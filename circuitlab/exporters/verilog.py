"""Compile digital .ckt declarations, logic and clock blocks to Verilog-2005."""

import re
from graphlib import CycleError, TopologicalSorter

from circuitlab.exporters.clocked import Expressions, compile_clocks
from circuitlab.model.graph import CircuitError
from circuitlab.language.syntax import CHIP, source_rows

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DECIMAL = r"[0-9]+(?:_[0-9]+)*"
STATE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[\s*([^\]]*)\s*\])?\s*(?:=\s*(.*))?"
)
MAX_STATE_WIDTH = 4096
STATEMENT = re.compile(r"([^=]+)\s*=\s*(.*)")
TOKEN = re.compile(rf"[A-Za-z_][A-Za-z0-9_]*|{DECIMAL}|[^\s]")
DIRECTIONS = {"input", "output"}
BINARY_OPERATORS = {"&", "^", "|"}
VERILOG_KEYWORDS = frozenset("""
    always and assign automatic begin buf bufif0 bufif1 case casex casez
    cell cmos config deassign default defparam design disable edge else end
    endcase endconfig endfunction endgenerate endmodule endprimitive
    endspecify endtable endtask event for force forever fork function
    generate genvar highz0 highz1 if ifnone incdir include initial inout
    input instance integer join large liblist library localparam macromodule
    medium module nand negedge nmos nor noshowcancelled not notif0 notif1 or
    output parameter pmos posedge primitive pull0 pull1 pulldown pullup
    pulsestyle_ondetect pulsestyle_onevent rcmos real realtime reg release
    repeat rnmos rpmos rtran rtranif0 rtranif1 scalared showcancelled signed
    small specify specparam strong0 strong1 supply0 supply1 table task time
    tran tranif0 tranif1 tri tri0 tri1 triand trior trireg unsigned use
    uwire vectored wait wand weak0 weak1 while wire wone wor xnor xor
""".split())


def _name(name, line):
    if not IDENTIFIER.fullmatch(name):
        raise CircuitError(f'Use a Verilog identifier, got "{name}".', line)
    if name in VERILOG_KEYWORDS:
        raise CircuitError(f'"{name}" is a reserved Verilog keyword.', line)
    return name


def _pin_list(raw, line):
    pins = tuple(_name(pin.strip(), line) for pin in raw.split(","))
    if len(set(pins)) != len(pins):
        raise CircuitError("List each pin once.", line)
    return pins


def _unsigned_decimal(raw, line, description):
    if not re.fullmatch(DECIMAL, raw):
        raise CircuitError(f"Use a nonnegative decimal integer for {description}.", line)
    # Bound conversion before int(): malformed huge literals must remain source
    # diagnostics, not Python conversion errors or unbounded allocations.
    digits = raw.replace("_", "").lstrip("0") or "0"
    if len(digits) > 1234:  # Enough for every value fitting MAX_STATE_WIDTH.
        raise CircuitError(f"The {description} is too large.", line)
    return int(digits)


def _state(statement, pins, states, line):
    match = STATE.fullmatch(statement)
    if not match:
        raise CircuitError("Use name, name[width], or name[width] = initial_value for internal state.", line)
    name, raw_width, raw_value = match.groups()
    _name(name, line)
    if name in pins:
        raise CircuitError(f'Pin "{name}" cannot also be declared as internal state.', line)
    if name in states:
        raise CircuitError(f'State "{name}" is already declared. Put its initializer in the declaration.', line)
    width = _unsigned_decimal(raw_width.strip(), line, "state width") if raw_width is not None else 1
    if not 1 <= width <= MAX_STATE_WIDTH:
        raise CircuitError(f"State width must be between 1 and {MAX_STATE_WIDTH} bits.", line)
    value = _unsigned_decimal(raw_value.strip(), line, "initial value") if raw_value is not None else 0
    if value.bit_length() > width:
        raise CircuitError(f'Initial value {value} does not fit in the {width}-bit state "{name}".', line)
    states[name] = width, value, raw_width is not None


def _logic(expression, signals, line):
    """Check the allowed tokens and keep Verilog's expression precedence."""
    output, references = [], {}
    expect_operand = True
    depth = 0
    tokens = TOKEN.findall(expression)
    position = 0
    while position < len(tokens):
        token = tokens[position]
        position += 1
        if expect_operand:
            if token in {"!", "("}:
                depth += token == "("
                output.append(token)
                continue
            if token in {"0", "1"}:
                output.append(f"1'b{token}")
            elif token in signals:
                references[token] = None
                width = signals[token]
                value = token
                if position < len(tokens) and tokens[position] == "[":
                    if position + 2 >= len(tokens) or tokens[position + 2] != "]":
                        raise CircuitError("Select one bit with name[index].", line)
                    index = _unsigned_decimal(tokens[position + 1], line, "bit index")
                    if width is None:
                        raise CircuitError(f'Signal "{token}" is scalar; declare [1] to use bit indexing.', line)
                    if index >= width:
                        raise CircuitError(f'Bit index {index} is outside "{token}" (0..{width - 1}).', line)
                    value = f"{token}[{index}]"
                    position += 3
                elif width is not None and width > 1:
                    raise CircuitError(f'Select one bit of "{token}" with name[index] in a one-bit expression.', line)
                output.append(value)
            elif IDENTIFIER.fullmatch(token):
                raise CircuitError(f'Unknown pin "{token}" in logic expression.', line)
            elif token == "~":
                raise CircuitError("Use ! for logical NOT.", line)
            else:
                raise CircuitError(f'Expected a pin or 0 or 1, got "{token}".', line)
            expect_operand = False
        elif token in BINARY_OPERATORS:
            output.append(f" {token} ")
            expect_operand = True
        elif token == ")" and depth:
            output.append(token)
            depth -= 1
        else:
            raise CircuitError(f'Unexpected token "{token}" in logic expression.', line)
    if expect_operand or depth:
        raise CircuitError("Complete the logic expression and close its parentheses.", line)
    return "".join(output), tuple(references)


def _signal_list(raw, line):
    """Parse scalar/vector port and wire declarations without changing header order."""
    result = {}
    for item in raw.split(","):
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[([^\]]*)\])?\s*", item)
        if not match:
            raise CircuitError("Declare a signal as name or name[width].", line)
        name = _name(match[1], line)
        width = _unsigned_decimal(match[2].strip(), line, "signal width") if match[2] is not None else None
        if width is not None and not 1 <= width <= MAX_STATE_WIDTH:
            raise CircuitError(f"Signal width must be between 1 and {MAX_STATE_WIDTH} bits.", line)
        if name in result:
            raise CircuitError(f'Signal "{name}" is declared more than once.', line)
        result[name] = width
    return result


def _combinational(rows, targets):
    """Build one complete expression per driven output/net; never infer storage."""
    index = 0

    def add(result, incoming, line):
        overlap = result.keys() & incoming.keys()
        if overlap:
            raise CircuitError(f'Signal "{sorted(overlap)[0]}" already has a logic assignment.', line)
        result.update(incoming)

    def conditional(condition, line, chain_depth=0):
        nonlocal index
        if chain_depth >= 64:
            raise CircuitError("Combinational else-if chains may contain at most 64 branches.", line)
        yes = block(True)
        if index >= len(rows):
            raise CircuitError("Combinational if requires an else assigning every output on every path.", line)
        other, other_line = rows[index]
        index += 1
        if re.fullmatch(r"else\s*\{", other):
            no = block(True)
        elif match := re.fullmatch(r"else\s+if\s*\((.*)\)\s*\{", other):
            no = conditional(match[1], other_line, chain_depth + 1)
        else:
            raise CircuitError("Combinational if requires an else assigning every output on every path.", line)
        if yes.keys() != no.keys():
            missing = ", ".join(sorted(yes.keys() ^ no.keys()))
            raise CircuitError(f"Assign {missing} on every combinational if/else path; use clock(...) for stored state.", line)
        return {name: ("mux", condition, yes[name], no[name], line) for name in yes}

    def block(nested=False):
        nonlocal index
        result = {}
        while index < len(rows):
            statement, line = rows[index]
            index += 1
            if statement == "}" and nested:
                return result
            if match := re.fullmatch(r"if\s*\((.*)\)\s*\{", statement):
                add(result, conditional(match[1], line), line)
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)", statement)
            if not match or match[1] not in targets:
                raise CircuitError("Combinational behavior assigns declared outputs/wires using expressions or complete if/else branches.", line)
            add(result, {match[1]: ("value", match[2], line)}, line)
        if nested:
            raise CircuitError("Close the combinational block with }.", rows[-1][1])
        return result

    return block()


class _CombinationalExpressions(Expressions):
    def name(self, prefix):
        # Clock expression helpers have their own counter and ooc_expr prefix.
        return super().name("ooc_comb")


def _module(name, pins, body, header_line):
    directions, port_widths, wires, states, constants = {}, {}, {}, {}, {}
    declarations_body, clocks = [], []
    index = outer_depth = 0
    while index < len(body):
        statement, line = body[index]
        if re.match(r"clock\s*\(", statement) and statement.endswith("{"):
            if outer_depth:
                raise CircuitError("Place clock(...) directly inside the chip, not inside combinational behavior.", line)
            block, depth = [], 1
            index += 1
            while index < len(body) and depth:
                row = body[index]
                depth += int(row[0].endswith("{")) - int(row[0] == "}")
                if depth:
                    block.append(row)
                index += 1
            if depth:
                raise CircuitError("Close the clock block with }.", line)
            clocks.append((statement, line, block))
        else:
            declarations_body.append((statement, line))
            outer_depth += int(statement.endswith("{")) - int(statement == "}")
            index += 1

    # Collect signal directions first so forward assignments remain supported.
    remaining, depth = [], 0
    for statement, line in declarations_body:
        match = STATEMENT.fullmatch(statement)
        if depth == 0 and match and match[2].strip().lower() in DIRECTIONS | {"wire"}:
            direction = match[2].strip().lower()
            for pin, width in _signal_list(match[1], line).items():
                if direction == "wire":
                    if pin in pins or pin in wires:
                        raise CircuitError(f'Signal "{pin}" is already declared or is a header pin.', line)
                    wires[pin] = width
                else:
                    if pin not in pins:
                        raise CircuitError(f'Pin "{pin}" is missing from the chip header.', line)
                    if pin in directions:
                        raise CircuitError(f'Pin "{pin}" already has a direction.', line)
                    directions[pin], port_widths[pin] = direction, width
        else:
            remaining.append((statement, line))
        depth += int(statement.endswith("{")) - int(statement == "}")

    behavior, depth = [], 0
    for statement, line in remaining:
        match = STATEMENT.fullmatch(statement)
        constant = re.fullmatch(r"const\s+(.+)", statement)
        if depth == 0 and constant:
            if "=" not in statement:
                raise CircuitError("Give each constant an explicit value.", line)
            _state(constant[1], (*pins, *states, *wires), constants, line)
        elif depth == 0 and not statement.endswith("{") and (
                not match or match[1].strip() not in (*pins, *wires)):
            _state(statement, (*pins, *constants, *wires), states, line)
        else:
            behavior.append((statement, line))
        depth += int(statement.endswith("{")) - int(statement == "}")

    for pin in pins:
        if pin not in directions:
            raise CircuitError(f'Declare "{pin}" as input or output.', header_line)
    targets = {pin for pin, direction in directions.items() if direction == "output"} | wires.keys()
    trees = _combinational(behavior, targets)
    for target in targets:
        if target not in trees:
            raise CircuitError(f'Signal "{target}" needs a logic assignment.', header_line)

    signals = {**port_widths, **wires}
    signals.update((state, width if vector else None) for state, (width, _, vector) in {**states, **constants}.items())
    for header, line, _ in clocks:
        match = re.match(r"clock\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", header)
        if match and signals.get(match[1]) not in (None, 1):
            raise CircuitError("A clock input must be one bit wide.", line)
    expressions = _CombinationalExpressions(signals)

    def lower(tree):
        line = tree[-1]
        if tree[0] == "value":
            expression = tree[1]
            if "~" in expression:
                raise CircuitError("Use ! for logical NOT.", line)
            # Keep existing single-bit HDL stable, including its spelling.
            try:
                code, reads = _logic(expression, signals, line)
                return code, 1, set(reads)
            except CircuitError:
                code, width = expressions.parse(expression, line)
                reads = {token for token in TOKEN.findall(expression) if token in signals}
                return code, width, reads
        condition, yes, no = tree[1:4]
        code, _ = expressions.parse(condition, line)
        yes_code, yes_width, yes_reads = lower(yes)
        no_code, no_width, no_reads = lower(no)
        code, width = expressions.temporary(f"({code} ? {yes_code} : {no_code})", max(yes_width, no_width))
        reads = {token for token in TOKEN.findall(condition) if token in signals}
        return code, width, reads | yes_reads | no_reads

    drivers = {}
    for target, tree in trees.items():
        code, width, reads = lower(tree)
        if signals[target] is None and width != 1:
            raise CircuitError(f'Assign a one-bit expression to scalar "{target}" or declare a vector output/wire.', tree[-1])
        drivers[target] = code, reads, tree[-1]
    dependencies = {pin: reads & drivers.keys() for pin, (_, reads, _) in drivers.items()}
    try:
        TopologicalSorter(dependencies).prepare()
    except CycleError as error:
        cycle = error.args[1]
        raise CircuitError(f"Combinational loop through {' -> '.join(cycle)}.", drivers[cycle[0]][2]) from None

    clock_timers, clock_wires, clock_blocks = compile_clocks(clocks, signals, states, directions)
    def size(width):
        return f" [{width - 1}:0]" if width is not None else ""
    ports = ",\n".join(f"    {directions[pin]} wire{size(port_widths[pin])} {pin}" for pin in pins)
    declarations = []
    for state, (width, value, vector) in states.items():
        initial = f"{width}'d{value}" if vector else f"1'b{value}"
        declarations.append(f"    reg{size(width if vector else None)} {state} = {initial};")
    for constant, (width, value, vector) in constants.items():
        declarations.append(f"    localparam{size(width if vector else None)} {constant} = {width}'d{value};")
    for wire, width in wires.items():
        declarations.append(f"    wire{size(width)} {wire} = {drivers[wire][0]};")
    assignments = [f"    assign {pin} = {value};" for pin, (value, _, _) in drivers.items() if pin not in wires]
    verilog = "\n".join([f"module {name} (", ports, ");", *declarations, *expressions.wires,
                           *clock_timers, *clock_wires, *assignments, *clock_blocks, "endmodule"])
    return verilog, tuple((pin, directions[pin]) for pin in pins)


def compile_chips(source: str):
    """Return each validated chip's pins, body, and Verilog."""
    from circuitlab.language.digital_structure import assemble_digital

    assembled = assemble_digital(source)
    if assembled is not None:
        name, pins, body, header_line = assembled
        verilog, directions = _module(name, pins, body, header_line)
        return [(name, directions, tuple(text for text, _ in body), verilog)]
    modules, names = [], set()
    rows = iter(source_rows(source, legacy_markers=False))
    for statement, line, _ in rows:
        header = CHIP.fullmatch(statement)
        if not header:
            raise CircuitError("Use chip Name(pin1, pin2) { ... } for Verilog export.", line)
        name = _name(header[1], line)
        if name in names:
            raise CircuitError(f'Chip "{name}" is already defined.', line)
        names.add(name)
        pins = _pin_list(header[2], line)
        body, depth = [], 1
        for statement, body_line, _ in rows:
            depth += int(statement.endswith("{")) - int(statement == "}")
            if depth > 64:
                raise CircuitError("Digital blocks may nest at most 64 levels.", body_line)
            if not depth:
                break
            body.append((statement, body_line))
        else:
            raise CircuitError(f'Close chip "{name}" with }}.', line)
        verilog, directions = _module(name, pins, body, line)
        statements = tuple(text for text, _ in body)
        modules.append((name, directions, statements, verilog))
    if not modules:
        raise CircuitError("Enter a chip block to export Verilog.")
    return modules


def export_verilog(source: str) -> str:
    """Emit one Verilog module per chip."""
    return "\n\n".join(verilog for _, _, _, verilog in compile_chips(source)) + "\n"
