"""Compile digital .ckt declarations, logic and clock blocks to Verilog-2005."""
# The digital path validates ports, widths, drivers, and state before emitting modules.
# Combinational branches must assign their outputs completely; clocked state is handled
# separately so missing assignments do not accidentally infer unsupported storage.

from dataclasses import dataclass, replace
from graphlib import CycleError, TopologicalSorter
import re

from circuitlab.exporters.clocked import Expressions, SELECTOR, selected_bits, compile_clocks
from circuitlab.language.control_flow import expand_digital_loops
from circuitlab.language.digital_arrays import lower_digital_arrays
from circuitlab.language.digital_switch import expand_switches, normalize_switch_labels
from circuitlab.language.digital_declarations import normalize_port_blocks, normalize_wire_declarations
from circuitlab.language.digital_argument_lists import expand_argument_lists
from circuitlab.language.digital_constants import normalize_integer_constants
from circuitlab.language.digital_clear import expand_clear_blocks
from circuitlab.language.linked import includes_files, read_includes
from circuitlab.language.pin_banks import expand_pin_banks, pin_bank_names
from circuitlab.language.math_blocks import digital_math_rows
from circuitlab.model.graph import CircuitError, MethodInstance
from circuitlab.language.syntax import CHIP, source_rows
from circuitlab.language.thumb_rom import lower_thumb_roms

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
    pins = tuple(_name(pin.strip(), line) for pin in expand_pin_banks(raw, line).split(","))
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
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\s*\([^()]*\)", statement):
            raise CircuitError(
                "This chip contains child-chip calls. Include its library from an entry "
                "document with main { ... } to export or simulate it.", line,
            )
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
    for item in expand_pin_banks(raw, line).split(","):
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


# Track complete driven bits along every branch. Reject missing paths or feedback
# instead of inferring latches or emitting an ambiguous combinational network.
def _combinational(rows, targets, signals):
    """Build one complete expression per driven output/net; never infer storage."""
    index = 0
    split = {match[1] for text, _ in rows
             if (match := re.match(r"(\w+)\s*\[", text)) and match[1] in signals}

    def assignments(target, expression, line):
        if "[" in target:
            bits = selected_bits(target, signals, line)
            if any(name not in targets for name, _ in bits):
                raise CircuitError("Combinational assignments must target outputs or wires.", line)
        else:
            if target not in targets:
                raise CircuitError("Combinational assignments must target outputs or wires.", line)
            if target not in split:
                return {target: ("value", expression, line)}
            bits = [(target, bit) for bit in range(signals[target])]
        return {(name if bit is None else (name, bit)):
                ("value", expression, line) if len(bits) == 1 else ("bit_value", expression, offset, line)
                for offset, (name, bit) in enumerate(bits)}

    def add(result, incoming, line):
        overlap = result.keys() & incoming.keys()
        if overlap:
            raise CircuitError(f'Signal "{sorted(overlap, key=str)[0]}" already has a logic assignment.', line)
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
            missing = ", ".join(map(str, sorted(yes.keys() ^ no.keys(), key=str)))
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
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*(?:\s*\[[^]]+\])?)\s*=\s*(.+)", statement)
            if not match:
                raise CircuitError("Combinational behavior assigns declared outputs/wires using expressions or complete if/else branches.", line)
            add(result, assignments(match[1], match[2], line), line)
        if nested:
            raise CircuitError("Close the combinational block with }.", rows[-1][1])
        return result

    return block()


class _CombinationalExpressions(Expressions):
    def name(self, prefix):
        # Clock expression helpers have their own counter and ooc_expr prefix.
        return super().name("ooc_comb")


def _module(name, pins, body, header_line):
    body = normalize_integer_constants(body)
    body = expand_switches(expand_digital_loops(normalize_switch_labels(body)))
    body = normalize_port_blocks(body)
    body = expand_argument_lists(body, pins)
    body = expand_clear_blocks(body)
    body = normalize_wire_declarations(lower_digital_arrays(body, pins), pins)
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

    declared = set(pins) | wires.keys()
    banks = pin_bank_names(declared)
    behavior, depth = [], 0
    for statement, line in remaining:
        match = STATEMENT.fullmatch(statement)
        constant = re.fullmatch(r"const\s+(.+)", statement)
        if depth == 0 and constant:
            if "=" not in statement:
                raise CircuitError("Give each constant an explicit value.", line)
            _state(constant[1], declared | states.keys(), constants, line)
        elif depth == 0 and not statement.endswith("{") and (
                not match or (match[1].strip() not in declared and
                              not ("[" in match[1] and match[1].partition("[")[0].strip() in declared | banks))):
            _state(statement, declared | constants.keys(), states, line)
        else:
            behavior.append((statement, line))
        depth += int(statement.endswith("{")) - int(statement == "}")

    for pin in pins:
        if pin not in directions:
            raise CircuitError(f'Declare "{pin}" as input or output.', header_line)
    targets = {pin for pin, direction in directions.items() if direction == "output"} | wires.keys()
    signals = {**port_widths, **wires}
    signals.update((state, width if vector else None) for state, (width, _, vector) in {**states, **constants}.items())
    trees = _combinational(behavior, targets, signals)
    for target in targets:
        if target not in trees and any((target, bit) not in trees for bit in range(signals[target] or 1)):
            raise CircuitError(f'Signal "{target}" needs a logic assignment for every bit.', header_line)
    for header, line, _ in clocks:
        match = re.match(r"clock\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", header)
        if match and signals.get(match[1]) not in (None, 1):
            raise CircuitError("A clock input must be one bit wide.", line)
    expressions = _CombinationalExpressions(signals)
    selections = {}

    def reads_in(expression, line):
        reads = {match[0] for match in TOKEN.finditer(expression) if match[0] in signals
                 and not (match[0] == "sevenseg" and expression[match.end():].lstrip().startswith("("))}
        for selection in SELECTOR.finditer(expression):
            reads.update(name for name, _ in selected_bits(selection[0], signals, line))
        return reads

    def lower(tree):
        line = tree[-1]
        if tree[0] == "bit_value":
            key = tree[1], line
            if key not in selections:
                code, width, reads = lower(("value", tree[1], line))
                code, _ = expressions.temporary(code, width)
                selections[key] = code, width, reads
            code, width, reads = selections[key]
            return (f"{code}[{tree[2]}]" if tree[2] < width else "1'b0"), 1, reads
        if tree[0] == "value":
            expression = tree[1]
            if "~" in expression:
                raise CircuitError("Use ! for logical NOT.", line)
            # Keep existing single-bit HDL stable, including its spelling.
            try:
                code, reads = _logic(expression, signals, line)
                width, reads = 1, set(reads)
            except CircuitError:
                code, width = expressions.parse(expression, line)
                reads = reads_in(expression, line)
            return code, width, reads
        condition, yes, no = tree[1:4]
        code, _ = expressions.parse(condition, line)
        yes_code, yes_width, yes_reads = lower(yes)
        no_code, no_width, no_reads = lower(no)
        code, width = expressions.temporary(f"({code} ? {yes_code} : {no_code})", max(yes_width, no_width))
        reads = reads_in(condition, line)
        return code, width, reads | yes_reads | no_reads

    drivers = {}
    for target in dict.fromkeys(key[0] if isinstance(key, tuple) else key for key in trees):
        if target in trees:
            tree = trees[target]
            code, width, reads = lower(tree)
        else:
            bit_values = [lower(trees[target, bit]) for bit in range(signals[target])]
            if any(width != 1 for _, width, _ in bit_values):
                raise CircuitError("Assign a one-bit expression to an indexed output.", header_line)
            code, width = expressions.pack([value for value, _, _ in bit_values])
            reads = set().union(*(reads for _, _, reads in bit_values))
            tree = trees[target, 0]
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


# Compile each definition independently and preserve header port order for callers.
# Return metadata alongside HDL so simulation and editor tooling share the same validation.
def compile_chips(source: str, *, instances=None, path=None, read_source=None):
    """Return each validated chip's pins, body, and Verilog, with optional assembly instances."""
    return _compile_digital_source(source, instances, path, read_source)[0]


@dataclass(frozen=True)
class DigitalDesign:
    modules: tuple
    instances: tuple[MethodInstance, ...]
    named_wires: tuple[str, ...]


def compile_digital_design(source, *, path=None, read_source=None):
    """Compile HDL and retain the hierarchy and nets for the schematic."""
    instances = []
    modules, named_wires = _compile_digital_source(source, instances, path, read_source)
    return DigitalDesign(tuple(modules), tuple(instances), named_wires)


def _compile_digital_source(source, instances, path, read_source):
    from circuitlab.language.digital_structure import assemble_digital

    # Compile one assembly from all of its included chip definitions. Retain
    # the line offsets so diagnostics point back to the responsible source file.
    origins = []
    if includes_files(source):
        files, _ = read_includes(source, path, read_source)
        offset = 0
        for filename, text in files.items():
            origins.append((offset, filename))
            offset += text.count("\n") + 1
        source = "\n".join(files.values())

    def location(line):
        return next(((line - start, filename) for start, filename in reversed(origins)
                     if line and line > start), (line, None))

    first_instance = len(instances) if instances is not None else 0
    try:
        assembled = assemble_digital(source, instances=instances)
        modules = _compile_chips(source, assembled)
    except CircuitError as error:
        if not origins or error.path is not None:
            raise
        line, filename = location(error.line)
        raise CircuitError(error.message, line, error.code, path=filename) from None
    if instances is not None and origins:
        for index in range(first_instance, len(instances)):
            instance = instances[index]
            line, filename = location(instance.line)
            instances[index] = replace(instance, line=line, path=filename)
    return modules, tuple(assembled[1]) if assembled else ()


def _compile_chips(source, assembled):
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
        body = normalize_wire_declarations(lower_thumb_roms(digital_math_rows(body)), pins)
        verilog, directions = _module(name, pins, body, line)
        statements = tuple(text for text, _ in body)
        modules.append((name, directions, statements, verilog))
    if not modules:
        raise CircuitError("Enter a chip block to export Verilog.")
    return modules


def export_verilog(source: str, *, path=None, read_source=None) -> str:
    """Emit one Verilog module per chip."""
    chips = compile_chips(source, path=path, read_source=read_source)
    return "\n\n".join(verilog for _, _, _, verilog in chips) + "\n"
