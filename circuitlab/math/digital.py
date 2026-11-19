"""Two-state, cycle-accurate simulation of OOC's generated digital HDL.

This is an ideal logic model, not an analog solver, timing model or FPGA reader.
Only compiler output is accepted; the private parser deliberately rejects other
Verilog constructs. Board conditioning comes from the same wrapper as FPGA builds.
"""
# This interpreter consumes the restricted HDL emitted by OOC, rather than arbitrary Verilog.
# Combinational values settle between clock transitions; edge-triggered assignments sample
# the previous state before their updates are committed together.

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import re
from time import perf_counter

from circuitlab.exporters.clocked import PRECEDENCE, frequency
from circuitlab.exporters.verilog import compile_chips
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.math.digital_fast import BOOLEAN, OPS as BINARY
from circuitlab.model.graph import CircuitError


# Recognize the generated subset, including sized binary/decimal literals.
# Multi-character operators precede their single-character prefixes.
# Unknown-state literals (x/z) and general Verilog syntax are not supported.
LEX = re.compile(r"\s+|//[^\n]*|[0-9]+'[bd][0-9]+|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|"
                 r"==|!=|<=|>=|<<|>>|&&|\|\||[!+\-&^|<>()\[\]{}?:;,.=@]")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# Use the application's common error type, with a recognizable simulator
# prefix for failures originating in generated HDL or runtime validation.
def _error(message):
    return CircuitError("Digital simulation: " + message)


# This is a parsed module, before hierarchy names are resolved or runtime
# functions are built. Each container belongs to this module instance.
@dataclass
class _Module:
    name: str
    # Port name -> input/output direction; widths also includes internal names.
    ports: dict = field(default_factory=dict)
    widths: dict = field(default_factory=dict)
    # Register/constant name -> initializer expression, not a live state value.
    registers: dict = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    # Continuously driven output/wire name -> expression tree.
    drivers: dict = field(default_factory=dict)
    # Clock blocks hold (clock_name, ordered_statements). Instances hold
    # (module_type, instance_name, {child_port: parent_signal}).
    blocks: list = field(default_factory=list)
    instances: list = field(default_factory=list)
    # Track declared internal wires separately so missing drivers can be rejected.
    wires: set = field(default_factory=set)


class _Parser:
    """Parse only the declarations and statements emitted by OOC."""

    # Bound generated text and token counts before building a runtime. These
    # limits keep unusually large designs from overwhelming interactive use.
    def __init__(self, text):
        if len(text) > 1_000_000:
            raise _error("generated design is too large for the interactive simulator.")
        self.tokens, offset = [], 0
        # Match at the current offset, rather than searching ahead. An unsupported
        # character must fail here instead of disappearing between valid tokens.
        while offset < len(text):
            match = LEX.match(text, offset)
            if not match:
                raise _error("unsupported generated HDL token.")
            token = match[0]
            if not token.isspace() and not token.startswith("//"):
                self.tokens.append(token)
            offset = match.end()
        if len(self.tokens) > 100_000:
            raise _error("generated design exceeds the interactive token limit.")
        self.index = 0

    # None is the end-of-input sentinel; take() turns unexpected EOF into a
    # controlled parse error even inside a block missing its closing keyword.
    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    # Consume exactly one token, optionally requiring a particular spelling.
    # All grammar routines use this to reject malformed generated constructs.
    def take(self, expected=None):
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise _error(f"expected {expected or 'a token'} in generated HDL, got {token!r}.")
        self.index += 1
        return token

    # Keywords are handled by their surrounding grammar; signal/module names
    # must still have the identifier shape emitted by the compiler.
    def identifier(self):
        token = self.take()
        if not IDENTIFIER.fullmatch(token):
            raise _error("expected a generated signal name.")
        return token

    # Plain decimal tokens here are bit indexes and width endpoints, not
    # expression literals. Limit their length before converting to int.
    def integer(self):
        token = self.take()
        if not token.isdigit() or len(token) > 5:
            raise _error("invalid generated bit index or width.")
        return int(token)

    # A missing range means one bit. Explicit vectors must be [high:0], so
    # their width is high+1 and indexing always counts upward from bit zero.
    def width(self):
        if self.peek() != "[":
            return 1
        self.take("[")
        high = self.integer()
        self.take(":")
        low = self.integer()
        self.take("]")
        if low != 0 or not 0 <= high < 4096:
            raise _error("only unsigned vectors of 1–4096 bits are supported.")
        return high + 1

    # Build tuple nodes such as ("literal", value, width), ("name", name),
    # (operator, left, right), and ("?:", condition, yes, no). No source text
    # is evaluated as Python; _expression() binds these nodes later.
    #
    # minimum is the precedence threshold for this recursive parse. Raising
    # it for a right operand makes equal-precedence binary operators group
    # left-to-right, while allowing tighter operators to remain inside it.
    def expression(self, minimum=1, depth=0):
        if depth > 128:
            raise _error("expression nesting exceeds the simulator limit.")
        token = self.take()
        # Logical NOT binds more tightly than every binary operator. Parentheses
        # and concatenation explicitly restart parsing at the lowest precedence.
        if token == "!":
            left = ("!", self.expression(max(PRECEDENCE.values()) + 1, depth + 1))
        elif token == "(":
            left = self.expression(1, depth + 1)
            self.take(")")
        elif token == "{":
            values = [self.expression(1, depth + 1)]
            while self.peek() == ",":
                self.take(",")
                values.append(self.expression(1, depth + 1))
            self.take("}")
            left = ("concat", tuple(values))
        # A sized literal carries its own arithmetic width (for example 8'd255).
        # Reject bad digits and values that do not fit, rather than truncating
        # an invalid literal into a different value.
        elif literal := re.fullmatch(r"([0-9]+)'([bd])([0-9]+)", token):
            if len(literal[1]) > 4 or len(literal[3]) > 4096:
                raise _error("generated literal exceeds the simulator limit.")
            width = int(literal[1])
            if not 1 <= width <= 4096 or (literal[2] == "b" and set(literal[3]) - {"0", "1"}):
                raise _error("invalid generated unsigned literal.")
            value = int(literal[3], 2 if literal[2] == "b" else 10)
            if value.bit_length() > width:
                raise _error("generated literal does not fit its width.")
            left = ("literal", value, width)
        # Keep a name selection symbolic until module names and widths are known.
        # The binding pass checks that bit/slice indexes are within that signal.
        elif IDENTIFIER.fullmatch(token):
            left = ("name", token)
            if self.peek() == "[":
                self.take("[")
                high = self.integer()
                if self.peek() == ":":
                    self.take(":")
                    left = ("slice", token, high, self.integer())
                else:
                    left = ("bit", token, high)
                self.take("]")
        else:
            raise _error("unsupported generated expression.")
        # Consume a chain of binary operators in the same precedence order used
        # by the exporter, so simulation agrees with the emitted expression.
        while PRECEDENCE.get(self.peek(), 0) >= minimum:
            op = self.take()
            left = (op, left, self.expression(PRECEDENCE[op] + 1, depth + 1))
        # The ternary is handled after binary operators because it binds more
        # loosely. Recursive alternatives also allow nested conditional expressions.
        if minimum == 1 and self.peek() == "?":
            self.take("?")
            yes = self.expression(1, depth + 1)
            self.take(":")
            left = ("?:", left, yes, self.expression(1, depth + 1))
        return left

    # Clocked statements are ordered trees, not immediately executed actions.
    # A full write is ("write", target, RHS); a generated slice write adds
    # inclusive high/low indexes. Only nonblocking <= writes are accepted.
    def statements(self, depth=0):
        if depth > 64:
            raise _error("statement nesting exceeds the simulator limit.")
        result = []
        while self.peek() != "end":
            if self.peek() == "if":
                result.append(self.conditional(depth))
            else:
                target = self.identifier()
                selection = ()
                if self.peek() == "[":
                    self.take("[")
                    high = self.integer()
                    self.take(":")
                    selection = high, self.integer()
                    self.take("]")
                self.take("<=")
                result.append(("write", target, self.expression(), *selection))
                self.take(";")
        self.take("end")
        return result

    # Normalize else-if into an if nested in the false branch. Later passes
    # therefore handle every condition as ("if", condition, yes_rows, no_rows).
    def conditional(self, depth):
        self.take("if")
        self.take("(")
        condition = self.expression()
        self.take(")")
        self.take("begin")
        yes, no = self.statements(depth + 1), []
        if self.peek() == "else":
            self.take("else")
            if self.peek() == "if":
                if depth >= 64:
                    raise _error("else-if nesting exceeds the simulator limit.")
                no = [self.conditional(depth + 1)]
            else:
                self.take("begin")
                no = self.statements(depth + 1)
        return "if", condition, yes, no

    # Collect at most the source core and an optional board wrapper. References
    # between them are resolved later, after both modules have been parsed.
    def parse(self):
        modules = {}
        while self.peek() is not None:
            self.take("module")
            module = _Module(self.identifier())
            if module.name in modules or len(modules) >= 2:
                raise _error("only one chip and its optional board wrapper are supported.")
            self.take("(")
            # The emitted ANSI-style header declares each input/output as an unsigned
            # wire. Other port directions and declaration forms are outside this parser.
            while True:
                direction = self.take()
                if direction not in {"input", "output"}:
                    raise _error("only input/output ports are supported.")
                self.take("wire")
                width = self.width()
                name = self.identifier()
                if name in module.widths:
                    raise _error("duplicate generated port.")
                module.ports[name], module.widths[name] = direction, width
                if self.peek() != ",":
                    break
                self.take(",")
            self.take(")")
            self.take(";")
            while self.peek() != "endmodule":
                kind = self.take()
                if kind in {"reg", "localparam", "wire"}:
                    width, name = self.width(), self.identifier()
                    if name in module.widths:
                        raise _error("duplicate generated declaration.")
                    module.widths[name] = width
                    # An internal wire may receive its driver here or in a later assign.
                    # Registers and localparams instead store initializer expressions.
                    if kind == "wire":
                        module.wires.add(name)
                        if self.peek() == ";":
                            self.take(";")
                            continue
                    self.take("=")
                    value = self.expression()
                    self.take(";")
                    target = module.drivers if kind == "wire" else (
                        module.registers if kind == "reg" else module.constants)
                    target[name] = value
                # An assign can drive an output or declared wire exactly once. It cannot
                # be used to overwrite state declared as a register or constant.
                elif kind == "assign":
                    name = self.identifier()
                    if name in module.drivers or (module.ports.get(name) != "output" and name not in module.wires):
                        raise _error("invalid generated output driver.")
                    self.take("=")
                    module.drivers[name] = self.expression()
                    self.take(";")
                # Only a rising-edge block is emitted for state updates; there are no
                # delays, falling-edge blocks, or general sensitivity lists in this model.
                elif kind == "always":
                    for token in ("@", "(", "posedge"):
                        self.take(token)
                    clock = self.identifier()
                    self.take(")")
                    self.take("begin")
                    module.blocks.append((clock, self.statements()))
                # Otherwise treat kind as a module type followed by a named instance.
                # Connections must be named .port(signal) bindings; flatten() later
                # checks that the child exists and every child port is connected.
                else:
                    instance = self.identifier()
                    self.take("(")
                    connections = {}
                    while True:
                        self.take(".")
                        pin = self.identifier()
                        self.take("(")
                        if pin in connections:
                            raise _error("duplicate generated instance connection.")
                        connections[pin] = self.identifier()
                        self.take(")")
                        if self.peek() != ",":
                            break
                        self.take(",")
                    self.take(")")
                    self.take(";")
                    module.instances.append((kind, instance, connections))
            self.take("endmodule")
            # A two-state wire needs an explicit driver. Do not invent a floating
            # or unknown value for a declaration that the generator left undriven.
            if module.wires - module.drivers.keys():
                raise _error("generated wire has no driver.")
            modules[module.name] = module
        if not modules:
            raise _error("no generated module.")
        return modules


# Compile the validated expression structure into integer operations, not source-code eval.
def _expression(node, names, widths, slots, depth=0):
    """Bind a validated expression to integer values; never execute source text."""
    # Return (callable, result_width, read_names). The callable reads the live
    # slot array; read_names supplies dependency ordering and UI metadata.
    # This depth check also catches long, flat-looking expression chains whose
    # tuple trees became deep during parsing.
    if depth > 128:
        raise _error("expression depth exceeds the interactive simulator limit.")
    op = node[0]
    if op == "literal":
        # A literal has no signal dependencies and was already checked to fit.
        return lambda values: node[1], node[2], set()
    if op == "concat":
        pieces = [_expression(part, names, widths, slots, depth + 1) for part in node[1]]
        # Pack the first expression into the most significant bits. Mask each
        # piece to its own width before appending the next one.
        def packed(values):
            result = 0
            for expression, width, _ in pieces:
                result = (result << width) | (expression(values) & ((1 << width) - 1))
            return result
        return packed, sum(width for _, width, _ in pieces), set().union(*(reads for _, _, reads in pieces))
    if op in {"name", "bit", "slice"}:
        if node[1] not in names:
            raise _error(f"unknown generated signal {node[1]}.")
        # Translate a module-local identifier through its hierarchy binding, then
        # capture the numeric slot index instead of looking up strings each cycle.
        name = names[node[1]]
        index, width = slots[name], widths[name]
        if op == "bit":
            if node[2] >= width:
                raise _error("generated bit index is outside its signal.")
            return lambda values: (values[index] >> node[2]) & 1, 1, {name}
        if op == "slice":
            high, low = node[2:]
            if not 0 <= low <= high < width:
                raise _error("generated slice is outside its signal.")
            # A slice is shifted down to bit zero and clipped to high-low+1 bits;
            # its result width differs from the width of the original signal.
            mask = (1 << (high - low + 1)) - 1
            return lambda values: (values[index] >> low) & mask, high - low + 1, {name}
        return lambda values: values[index], width, {name}
    left, left_width, reads = _expression(node[1], names, widths, slots, depth + 1)
    # Logical operators produce integer 0/1, even when their input is a vector.
    if op == "!":
        return lambda values: int(not left(values)), 1, reads
    # Evaluate only the chosen branch at runtime, but record reads from both
    # branches and the condition. The dependency graph must remain valid
    # when a different branch is selected on a later cycle.
    if op == "?:":
        yes, yes_width, yes_reads = _expression(node[2], names, widths, slots, depth + 1)
        no, no_width, no_reads = _expression(node[3], names, widths, slots, depth + 1)
        return (lambda values: yes(values) if left(values) else no(values),
                max(yes_width, no_width), reads | yes_reads | no_reads)
    right, right_width, right_reads = _expression(node[2], names, widths, slots, depth + 1)
    # Width is determined at every operator, before a destination assignment.
    # For example, an 8-bit (255+1) wraps to zero even if later assigned into
    # a 16-bit register. Shifts retain the left width; comparisons yield 1 bit.
    width = 1 if op in BOOLEAN else left_width if op in {"<<", ">>"} else max(left_width, right_width)
    mask, function = (1 << width) - 1, BINARY[op]
    # A shift by the operand width or more is zero in this unsigned model.
    # Check that case before shifting to avoid huge Python intermediates.
    if op in {"<<", ">>"}:
        def shifted(values):
            count = right(values)
            return 0 if count >= width else function(left(values), count) & mask
        return shifted, width, reads | right_reads
    # Mask after each binary operation. This gives subtraction modular
    # unsigned behavior and converts comparison/logical bool results to bits.
    return lambda values: int(function(left(values), right(values))) & mask, width, reads | right_reads


class DigitalSimulation:
    """Single-clock OOC simulation. Public values are settled at clock-low.

    reset() clears inputs, restores declared state and sets cycles to zero.
    step() performs one complete clock cycle; all edge blocks see old state.
    With no clock, step() advances a sample index without inventing a frequency.
    """

    # Build in stages: compile OOC to HDL, parse it, flatten its scopes, bind
    # expressions to slots, order combinational drivers, and compile edge blocks.
    # The resulting functions and arrays are reused by step() and advance().
    def __init__(self, source, profile="ooc"):
        if len(source) > 1_000_000:
            raise _error("source exceeds the interactive size limit.")
        # Simulation follows the compiler's lowered design, including generated
        # counters and phase increments, instead of interpreting OOC a second way.
        chips = compile_chips(source)
        if len(chips) != 1:
            raise _error("select one chip; multi-chip simulation is not yet supported.")
        self.profile = profile
        # Use the same board wrapper as an FPGA build, so switch synchronizer
        # registers and their cycle delays are real simulated state. The ordinary
        # OOC profile uses the board-neutral core HDL directly.
        if profile == "de10-lite":
            from circuitlab.tooling.fpga import compile_de10_lite
            hdl, _, _ = compile_de10_lite(source)
        elif profile == "ooc":
            hdl = chips[0][3]
        else:
            raise _error("choose the ooc or de10-lite profile.")
        modules = _Parser(hdl).parse()
        # The compiler emits the top module first: the core by itself, or the
        # board wrapper before its renamed user core.
        self.name = next(iter(modules))
        top = modules[self.name]
        # These tables use flattened names. A driver retains (expression, local
        # name mapping); a block retains its clock, rows, mapping, and legal targets.
        # visible controls public probes, independently of private runtime slots.
        widths, initials, drivers, blocks, visible, raw_dependencies = {}, {}, {}, [], set(), {}
        kinds, clock_names, descriptions = {}, {}, {}
        declared_wires = set()
        self.source_bindings = {}
        # Recover explicitly declared source wires so they remain observable.
        # Compiler-created temporary wires still participate in evaluation, but
        # do not need to appear as separate user signals in the viewer.
        for row in chips[0][2]:
            if declaration := re.fullmatch(r"(.+?)\s*=\s*wire", row, re.I):
                declared_wires.update(re.sub(r"\[.*", "", name).strip()
                                      for name in expand_pin_banks(declaration[1], 1).split(","))

        # Give internal names an instance prefix while aliasing child ports to
        # the parent's existing nets. This runtime accepts only a top module and
        # one child level, which covers the generated board wrapper/core pair.
        def flatten(module, prefix="", aliases=None, ancestors=()):
            if module.name in ancestors or len(ancestors) > 1:
                raise _error("recursive or deeper generated hierarchy is unsupported.")
            # This mapping is kept with every expression and statement from the
            # module, so identical local spellings in different scopes stay distinct.
            names = {name: (aliases or {}).get(name, prefix + name) for name in module.widths}
            if not module.instances:
                # The source core is the leaf module; the board profile may
                # wrap it and connect its input pins to synchronizer outputs.
                # Expose where source-core signals actually live after wrapper aliases.
                # For example, source SW0 may read ooc_sw0_sync, while a local register
                # may be named ooc_logic.held. Diagrams use this instead of guessing names.
                self.source_bindings = dict(names)
            for name, width in module.widths.items():
                full = names[name]
                # A child port reuses a parent slot rather than allocating a second value.
                # Validate its existing width before accepting the alias.
                if aliases and name in module.ports:
                    if full not in widths or widths[full] != width:
                        raise _error("invalid generated port width or connection.")
                    continue
                if full in widths:
                    raise _error("duplicate generated hierarchical signal.")
                widths[full] = width
                # Publish ports, state, constants, and explicitly declared source wires.
                # All other generated names remain in widths for execution, even when hidden.
                if name in module.ports or name in module.registers or name in module.constants or name in declared_wires:
                    visible.add(full)
                    kinds[full] = module.ports.get(name) or (
                        "register" if name in module.registers else "constant" if name in module.constants else "wire")
            # Initialization is deliberately literal-only. Save masked reset values
            # now; no initializer can depend on another signal's evaluation order.
            for name, initial in {**module.registers, **module.constants}.items():
                if initial[0] != "literal":
                    raise _error("only literal generated initializers are supported.")
                initials[names[name]] = initial[1] & ((1 << widths[names[name]]) - 1)
            # Save drivers before binding them, allowing forward references to other
            # declared signals. The later graph pass resolves evaluation order.
            for name, expression in module.drivers.items():
                full = names[name]
                if full in drivers:
                    raise _error("multiple generated combinational drivers.")
                drivers[full] = expression, names
            # Carry the set of this module's registers with each clock block. Binding
            # must not turn an output, wire, or unrelated scope's net into writable state.
            for clock, statements in module.blocks:
                if module.ports.get(clock) != "input":
                    raise _error("only top-level input clocks are supported.")
                blocks.append((names[clock], statements, names, {names[n] for n in module.registers}))
            instances = set()
            for child, instance, connections in module.instances:
                if child not in modules or instance in instances:
                    raise _error("unknown or duplicate generated instance.")
                instances.add(instance)
                # Require exactly one connection for every child port, all using parent
                # names. Recursive flattening composes these aliases with the parent map.
                if set(connections) != set(modules[child].ports) or any(n not in names for n in connections.values()):
                    raise _error("incomplete generated instance connections.")
                flatten(modules[child], prefix + instance + ".",
                        {pin: names[net] for pin, net in connections.items()}, (*ancestors, module.name))

        flatten(top)
        if len(widths) > 4096:
            raise _error("design exceeds 4096 simulated signals.")
        # Many blocks may share a clock, but their canonical clock must be the
        # same top-level input. Derived clocks and multiple independent clocks
        # would require a different event scheduler.
        self.clocks = tuple(dict.fromkeys(clock for clock, *_ in blocks))
        if len(self.clocks) > 1 or any(top.ports.get(clock) != "input" for clock in self.clocks):
            raise _error("only one input clock is supported in this simulator.")
        # Only non-clock top inputs are manually editable. The simulator owns
        # clock transitions; child-port aliases are not separate external inputs.
        self.inputs = tuple(name for name, direction in top.ports.items() if direction == "input" and name not in self.clocks)
        self.outputs = tuple(name for name, direction in top.ports.items() if direction == "output")
        self.signals = {name: width for name, width in widths.items() if name in visible}
        # Use one integer list for all live signals, including hidden wires.
        # All slot arrays follow the same insertion order. Undeclared initial
        # values start at zero; combinational outputs are recomputed on reset.
        self._slots = {name: index for index, name in enumerate(widths)}
        self._initial = [initials.get(name, 0) for name in widths]
        self._drivers, edges, dependencies = {}, [], {}
        # Compile each driven net into (destination_slot, callable, target_mask).
        # The target mask models assignment truncation separately from masks
        # already applied inside the expression's intermediate operations.
        for name, (expression, names) in drivers.items():
            function, _, reads = _expression(expression, names, widths, self._slots)
            self._drivers[name] = self._slots[name], function, (1 << widths[name]) - 1
            dependencies[name] = reads
        # Keep only driven-net reads as graph predecessors; registers and inputs
        # are already available leaves. A topological order then settles every
        # driver in one pass, regardless of its source declaration order.
        try:
            order = TopologicalSorter({name: reads & drivers.keys() for name, reads in dependencies.items()}).static_order()
            self._settlers = [self._drivers[name] for name in order]
        # This ideal model rejects combinational loops instead of trying to
        # iterate them to convergence or assign an analog oscillation behavior.
        except CycleError:
            raise _error("combinational feedback is unsupported.") from None

        # Bind clocked rows into executable full writes, masked partial writes,
        # and branches. Return their target set for clock-driver ownership checks.
        # controls collects condition reads; guards preserves the signed branch
        # path for human-readable descriptions, rather than runtime evaluation.
        def compile_statements(statements, names, registers, clock, controls=frozenset(), guards=()):
            result, targets = [], set()
            for statement in statements:
                if statement[0] == "write":
                    target = names.get(statement[1])
                    if target not in registers:
                        raise _error("clocked assignment does not target a register.")
                    expression, _, reads = _expression(statement[2], names, widths, self._slots)
                    # State depends on both its RHS and the conditions deciding whether that
                    # write occurs. Record the clock too, so metadata reflects the edge source.
                    dependencies.setdefault(target, set()).update(reads | controls | {clock})
                    clock_names[target] = clock
                    descriptions.setdefault(target, []).append((statement[2], names, guards))
                    # A partial write stores an already positioned destination mask plus
                    # its low-bit offset. Runtime merging preserves untouched register bits.
                    if len(statement) == 5:
                        high, low = statement[3:]
                        if not 0 <= low <= high < widths[target]:
                            raise _error("clocked slice is outside its register.")
                        result.append(("partial", self._slots[target], expression,
                                       ((1 << (high - low + 1)) - 1) << low, low))
                    else:
                        result.append(("write", self._slots[target], expression, (1 << widths[target]) - 1))
                    targets.add(target)
                else:
                    condition, _, reads = _expression(statement[1], names, widths, self._slots)
                    # Compile both paths, adding this condition to their control dependencies.
                    # A false-path guard is retained explicitly for the viewer's description.
                    yes, yes_targets = compile_statements(statement[2], names, registers, clock,
                                                         controls | reads, (*guards, (statement[1], True)))
                    no, no_targets = compile_statements(statement[3], names, registers, clock,
                                                       controls | reads, (*guards, (statement[1], False)))
                    result.append(("if", condition, yes, no))
                    targets.update(yes_targets | no_targets)
            return result, targets

        # A register may be written repeatedly inside one block (last selected
        # write wins). Separate blocks may not own the same register, even if
        # their conditions or written bit ranges appear disjoint.
        owners = set()
        for clock, statements, names, registers in blocks:
            compiled, targets = compile_statements(statements, names, registers, clock)
            if owners & targets:
                raise _error("a register has multiple clocked drivers.")
            owners.update(targets)
            edges.append(compiled)
        self._edges = edges
        # Acceleration is optional. Failure to build a bounded fast plan leaves
        # the ordinary edge interpreter intact; it does not invalidate the design.
        from circuitlab.math.digital_fast import FastPlan, PlanLimit
        try:
            self._fast_plan = FastPlan(blocks, drivers, widths, self._slots, self.clocks, visible)
        except PlanLimit:
            self._fast_plan = None  # Large connection chains still use exact stepping.

        # Collapse hidden intermediate wires until the first visible signals are
        # reached. Stop there, retaining meaningful register-to-register links
        # instead of flattening every dependency all the way back to input ports.
        def visible_reads(name, seen):
            if name in visible:
                return {name}
            # Bound metadata traversal too. Visible register feedback is legal and
            # returns above; an unbounded chain of hidden dependencies is not useful
            # as an interactive diagram dependency.
            if name in seen or len(seen) > 128:
                raise _error("generated expression dependencies are cyclic or too deep.")
            result = set()
            for dependency in dependencies.get(name, ()):
                result.update(visible_reads(dependency, seen | {name}))
            return result

        for name in self.signals:
            reads = set()
            for dependency in dependencies.get(name, ()):
                reads.update(visible_reads(dependency, set()))
            raw_dependencies[name] = tuple(sorted(reads))
        # These public tables describe probes and diagrams; runtime execution
        # continues to use the private slot functions built above.
        self.dependencies = raw_dependencies
        self.signal_kinds = kinds
        self.signal_clocks = clock_names

        # Descriptions have a strict display budget; shortening metadata never
        # changes the expressions executed by the simulator.
        def clip(text, limit=500):
            return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"

        # Render a bounded explanation from the same parsed expression tree.
        # Both a text budget and a recursion bound limit deeply expanded tooltips.
        def describe(node, names, limit=480, depth=0):
            if limit < 1 or depth > 128:
                return "…"
            op = node[0]
            if op == "literal":
                return clip(str(node[1]), limit)
            if op == "concat":
                return clip("{" + ", ".join(describe(part, names, max(8, limit // len(node[1])), depth + 1)
                                             for part in node[1]) + "}", limit)
            if op in {"name", "bit", "slice"}:
                name = names[node[1]]
                # Inline compiler temporary expressions in explanations, but preserve
                # names that users can actually inspect as visible signals.
                if name not in visible and name in drivers:
                    expression, context = drivers[name]
                    text = describe(expression, context, limit, depth + 1)
                else:
                    text = name
                selection = f"[{node[2]}]" if op == "bit" else f"[{node[2]}:{node[3]}]" if op == "slice" else ""
                return clip(text + selection, limit)
            left = describe(node[1], names, limit - 3, depth + 1)
            if op == "!":
                return clip("!" + left, limit)
            if op == "?:":
                # Reserve space for the condition and punctuation, then share the
                # remaining tooltip budget between the two alternatives.
                branch_limit = max(0, (limit - len(left) - 8) // 2)
                yes = describe(node[2], names, branch_limit, depth + 1)
                no = describe(node[3], names, branch_limit, depth + 1)
                return clip(f"({left} ? {yes} : {no})", limit)
            right = describe(node[2], names, limit - len(left) - len(op) - 4, depth + 1)
            return clip(f"({left} {op} {right})", limit)

        # Build role-specific text: source input, continuous expression, constant,
        # or initialized register with its guarded edge assignments in source order.
        self.signal_expressions = {}
        for name, kind in kinds.items():
            if kind == "input":
                text = f"Input {name}"
            elif kind in {"output", "wire"}:
                node, names = drivers[name]
                text = f"{name} = {describe(node, names)}"
            elif kind == "constant":
                text = f"{name} = {initials[name]} (constant)"
            else:
                text = f"Initial: {initials[name]}"
                if name in clock_names:
                    text += f"\nAt each rising edge of {clock_names[name]} (assignments in source order):"
                    # These lines describe pending writes, not sequential mutations. Multiple
                    # RHS expressions still read old state, as the edge executor does below.
                    for node, names, guards in descriptions[name]:
                        conditions = [describe(guard, names, 200) if positive else
                                      f"!({describe(guard, names, 200)})" for guard, positive in guards]
                        prefix = "if (" + " && ".join(conditions) + ") " if conditions else ""
                        text += f"\n{prefix}{name} = {describe(node, names)}"
                        if len(text) > 500:
                            break
                else:
                    text += " (held; no clocked assignments)"
            self.signal_expressions[name] = clip(text)
        # Clock frequency is metadata for physical time interpretation; step()
        # advances one logical cycle without sleeping for a real clock period.
        # Use the first declared rate, or no rate for a clockless source.
        rates = []
        for row in chips[0][2]:
            match = re.fullmatch(r"clock\s*\([^,()]+,\s*([^()]+)\)\s*\{", row)
            if match:
                rates.append(frequency(match[1], 1))
        self.frequency = float(rates[0]) if rates else None
        # The supported board clock is fixed at 50 MHz. A clockless board design
        # still has no invented timebase.
        if profile == "de10-lite":
            self.frequency = 50_000_000.0 if self.clocks else None
        self.reset()

    # Recompute combinational drivers once in dependency order after an input, clock,
    # or register change, separately from committing pending register writes.
    def _settle(self):
        # The dependency order was fixed during construction. Each callable sees
        # any upstream driven nets already updated in this pass; no iterative
        # fixed-point search is needed because combinational cycles were rejected.
        for slot, expression, mask in self._settlers:
            self._values[slot] = expression(self._values) & mask

    # Publish visible values as a fresh dictionary, leaving hidden slots
    # private. Later publishes replace this dictionary, so a saved dictionary
    # from a sample retains its integer readings from that moment.
    def _publish(self):
        self.values = {name: self._values[self._slots[name]] for name in self.signals}

    # Copy the reset template before mutating state. Inputs return to zero,
    # literal register/constant initializers are restored, and driven outputs
    # are recalculated before the first public snapshot.
    def reset(self):
        self._values = self._initial.copy()
        self.cycles = 0
        self._settle()
        self._publish()

    # Validate before changing any slot. Input changes settle immediately
    # without consuming a cycle; registers update only through clock edges.
    def set_input(self, name, value):
        if name not in self.inputs or not isinstance(value, int):
            raise _error("set a non-clock input to an unsigned integer.")
        width = self.signals[name]
        if not 0 <= value < (1 << width):
            raise _error(f'Input "{name}" requires an unsigned {width}-bit value (0..{(1 << width) - 1}).')
        self._values[self._slots[name]] = value
        self._settle()
        self._publish()

    # Advance one complete edge cycle. Every clocked right-hand side must observe the same
    # pre-edge state, even when several assignments update connected registers.
    def step(self):
        # pending holds next-state values while _values stays at the pre-commit
        # state. Thus r<=r+1; r<=r+1 computes old_r+1 twice and stores it once,
        # rather than incrementing twice as sequential Python assignments would.
        def execute(statements):
            for statement in statements:
                if statement[0] == "write":
                    pending[statement[1]] = statement[2](self._values) & statement[3]
                elif statement[0] == "partial":
                    _, target, expression, mask, low = statement
                    # Merge into earlier pending destination bits, but evaluate the new RHS
                    # from old _values. This preserves earlier nonoverlapping slice writes;
                    # a later write replaces overlapping bits in source order.
                    previous = pending.get(target, self._values[target])
                    pending[target] = (previous & ~mask) | ((expression(self._values) << low) & mask)
                else:
                    # Branch choices also observe the shared pre-commit state, including the
                    # high clock level, rather than any values already staged in pending.
                    execute(statement[2] if statement[1](self._values) else statement[3])

        # Raise the clock and settle its combinational dependents before reading
        # edge expressions. This makes an RHS that explicitly reads clk see 1.
        if self.clocks:
            slot = self._slots[self.clocks[0]]
            self._values[slot] = 1
            self._settle()
            pending = {}
            for statements in self._edges:
                execute(statements)
            # Every block has finished sampling before any register is committed.
            # For a two-stage synchronizer, stage two therefore sees the old stage
            # one value, even when the stages are written by different blocks.
            for target, value in pending.items():
                self._values[target] = value
            self._values[slot] = 0
        # After commit, publish at clock-low with combinational outputs settled.
        # For a clockless design, this still advances the sample index by one
        # without creating an edge or assigning a physical clock frequency.
        self._settle()
        self.cycles += 1
        self._publish()
        return self.values

    # Use the fast planner only for intervals it can prove exact; otherwise execute normal
    # cycles, respecting the optional wall-time budget used by interactive controls.
    def advance(self, cycles, *, traces=(), sample=None, deadline=None, sample_every=1):
        """Advance exact state, stopping at a wall-time budget if supplied.

        Simple counters/accumulators skip only mathematically proven intervals.
        Other logic runs the ordinary per-cycle interpreter. Single-bit traces
        bound skipped intervals at their transitions; vector traces are sampled
        at most ``sample_every`` cycles apart. Samples before/after each interval
        preserve the edge positions of those observed single-bit signals.
        """
        # cycles is a relative request. sample receives (absolute_cycle, values).
        # sample_every limits the gap between samples when a callback is supplied;
        # without a callback, that argument does not restrict bulk advancement.
        if not isinstance(cycles, int) or cycles < 0:
            raise _error("advance requires a nonnegative integer cycle count.")
        if not isinstance(sample_every, int) or sample_every < 1:
            raise _error("sample interval must be a positive integer.")
        start = self.cycles
        target = start + cycles
        while self.cycles < target:
            # deadline is an absolute perf_counter() time, checked between intervals.
            # The current interval is not interrupted partway through, so callers
            # use the returned completed-cycle count when a time budget stops a batch.
            if deadline is not None and perf_counter() >= deadline:
                break
            limit = target - self.cycles
            if sample is not None:
                limit = min(limit, sample_every)
            # The fast planner returns (safe_count, {slot: (delta, mask)}) only for
            # updates proved exact over that interval. Missing/one-cycle plans use
            # the full interpreter, preserving all branch and partial-write behavior.
            plan = self._fast_plan.span(self._values, limit, traces) if limit > 1 and self._fast_plan is not None else None
            if plan is None or plan[0] == 1:
                self.step()
                if sample is not None:
                    sample(self.cycles, self.values)
                continue
            count, increments = plan

            # A proven interval can update changing registers arithmetically. Leave
            # held state alone, then recompute all combinational values and publish
            # the same settled endpoint that repeated ordinary steps would reach.
            def move(amount):
                for slot, (delta, mask) in increments.items():
                    self._values[slot] = (self._values[slot] + delta * amount) & mask
                self.cycles += amount
                self._settle()
                self._publish()

            # Sample on both sides of the interval's final boundary. The planner
            # stops at requested one-bit transitions, so these samples preserve the
            # last old value and first new value at their exact cycle indexes.
            if sample is not None:
                move(count - 1)
                sample(self.cycles, self.values)
                move(1)
                sample(self.cycles, self.values)
            else:
                move(count)
        # Report actual progress, which can be smaller than the request when the
        # deadline is reached. A request for zero cycles returns zero immediately.
        return self.cycles - start
