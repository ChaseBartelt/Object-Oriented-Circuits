"""Compile combinational .ckt chip blocks to Verilog-2005 modules."""

import re
from graphlib import CycleError, TopologicalSorter

from .graph import CircuitError
from .syntax import CHIP, source_rows

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STATEMENT = re.compile(r"([^=]+)\s*=\s*(.*)")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[^\s]")
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


def _logic(expression, pins, line):
    """Check the allowed tokens and keep Verilog's expression precedence."""
    output, references = [], {}
    expect_operand = True
    depth = 0
    for token in TOKEN.findall(expression):
        if expect_operand:
            if token in {"!", "("}:
                depth += token == "("
                output.append(token)
                continue
            if token in {"0", "1"}:
                output.append(f"1'b{token}")
            elif token in pins:
                references[token] = None
                output.append(token)
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


def _module(name, pins, body, header_line):
    directions, drivers = {}, {}
    for statement, line in body:
        match = STATEMENT.fullmatch(statement)
        if not match:
            raise CircuitError("Use pin = input, pin = output, or pin = logic.", line)
        left, right = (field.strip() for field in match.groups())
        targets = _pin_list(left, line)
        for pin in targets:
            if pin not in pins:
                raise CircuitError(f'Pin "{pin}" is missing from the chip header.', line)
        direction = right.lower()
        if direction in DIRECTIONS:
            for pin in targets:
                if pin in directions:
                    raise CircuitError(f'Pin "{pin}" already has a direction.', line)
                directions[pin] = direction
            continue
        if len(targets) != 1:
            raise CircuitError("Assign a logic expression to one output at a time.", line)
        pin = targets[0]
        if pin in drivers:
            raise CircuitError(f'Output "{pin}" already has a logic assignment.', line)
        value, inputs = _logic(right, pins, line)
        drivers[pin] = value, inputs, line

    for pin, (_, _, line) in drivers.items():
        if directions.get(pin) == "input":
            raise CircuitError(f'Input "{pin}" cannot have a logic assignment.', line)
    for pin in pins:
        if pin not in directions:
            raise CircuitError(f'Declare "{pin}" as input or output.', header_line)
        if directions[pin] == "output" and pin not in drivers:
            raise CircuitError(f'Output "{pin}" needs a logic assignment.', header_line)
    dependencies = {
        pin: [source for source in inputs if source in drivers]
        for pin, (_, inputs, _) in drivers.items()
    }
    try:
        TopologicalSorter(dependencies).prepare()
    except CycleError as error:
        cycle = error.args[1]
        raise CircuitError(
            f"Combinational loop through {' -> '.join(cycle)}.", drivers[cycle[0]][2]
        ) from None

    ports = ",\n".join(f"    {directions[pin]} wire {pin}" for pin in pins)
    assignments = [f"    assign {pin} = {value};" for pin, (value, _, _) in drivers.items()]
    verilog = "\n".join([f"module {name} (", ports, ");", *assignments, "endmodule"])
    return verilog, tuple((pin, directions[pin]) for pin in pins)


def compile_chips(source: str):
    """Return each validated chip's pins, body, and Verilog."""
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
        body = []
        for statement, body_line, _ in rows:
            if statement == "}":
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
