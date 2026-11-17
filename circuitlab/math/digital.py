"""Two-state, cycle-accurate simulation of OOC's generated digital HDL.

This is an ideal logic model, not an analog solver, timing model or FPGA reader.
Only compiler output is accepted; the private parser deliberately rejects other
Verilog constructs. Board conditioning comes from the same wrapper as FPGA builds.
"""

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import re
from time import perf_counter

from circuitlab.exporters.clocked import PRECEDENCE, frequency
from circuitlab.exporters.verilog import compile_chips
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.math.digital_fast import BOOLEAN, OPS as BINARY
from circuitlab.model.graph import CircuitError


LEX = re.compile(r"\s+|//[^\n]*|[0-9]+'[bd][0-9]+|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|"
                 r"==|!=|<=|>=|&&|\|\||[!+\-&^|<>()\[\]{}?:;,.=@]")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _error(message):
    return CircuitError("Digital simulation: " + message)


@dataclass
class _Module:
    name: str
    ports: dict = field(default_factory=dict)
    widths: dict = field(default_factory=dict)
    registers: dict = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    drivers: dict = field(default_factory=dict)
    blocks: list = field(default_factory=list)
    instances: list = field(default_factory=list)
    wires: set = field(default_factory=set)


class _Parser:
    """Parse only the declarations and statements emitted by OOC."""

    def __init__(self, text):
        if len(text) > 1_000_000:
            raise _error("generated design is too large for the interactive simulator.")
        self.tokens, offset = [], 0
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

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise _error(f"expected {expected or 'a token'} in generated HDL, got {token!r}.")
        self.index += 1
        return token

    def identifier(self):
        token = self.take()
        if not IDENTIFIER.fullmatch(token):
            raise _error("expected a generated signal name.")
        return token

    def integer(self):
        token = self.take()
        if not token.isdigit() or len(token) > 5:
            raise _error("invalid generated bit index or width.")
        return int(token)

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

    def expression(self, minimum=1, depth=0):
        if depth > 128:
            raise _error("expression nesting exceeds the simulator limit.")
        token = self.take()
        if token == "!":
            left = ("!", self.expression(9, depth + 1))
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
        while PRECEDENCE.get(self.peek(), 0) >= minimum:
            op = self.take()
            left = (op, left, self.expression(PRECEDENCE[op] + 1, depth + 1))
        if minimum == 1 and self.peek() == "?":
            self.take("?")
            yes = self.expression(1, depth + 1)
            self.take(":")
            left = ("?:", left, yes, self.expression(1, depth + 1))
        return left

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

    def parse(self):
        modules = {}
        while self.peek() is not None:
            self.take("module")
            module = _Module(self.identifier())
            if module.name in modules or len(modules) >= 2:
                raise _error("only one chip and its optional board wrapper are supported.")
            self.take("(")
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
                elif kind == "assign":
                    name = self.identifier()
                    if name in module.drivers or (module.ports.get(name) != "output" and name not in module.wires):
                        raise _error("invalid generated output driver.")
                    self.take("=")
                    module.drivers[name] = self.expression()
                    self.take(";")
                elif kind == "always":
                    for token in ("@", "(", "posedge"):
                        self.take(token)
                    clock = self.identifier()
                    self.take(")")
                    self.take("begin")
                    module.blocks.append((clock, self.statements()))
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
            if module.wires - module.drivers.keys():
                raise _error("generated wire has no driver.")
            modules[module.name] = module
        if not modules:
            raise _error("no generated module.")
        return modules


def _expression(node, names, widths, slots, depth=0):
    """Bind a validated expression to integer values; never execute source text."""
    if depth > 128:
        raise _error("expression depth exceeds the interactive simulator limit.")
    op = node[0]
    if op == "literal":
        return lambda values: node[1], node[2], set()
    if op == "concat":
        pieces = [_expression(part, names, widths, slots, depth + 1) for part in node[1]]
        def packed(values):
            result = 0
            for expression, width, _ in pieces:
                result = (result << width) | (expression(values) & ((1 << width) - 1))
            return result
        return packed, sum(width for _, width, _ in pieces), set().union(*(reads for _, _, reads in pieces))
    if op in {"name", "bit", "slice"}:
        if node[1] not in names:
            raise _error(f"unknown generated signal {node[1]}.")
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
            mask = (1 << (high - low + 1)) - 1
            return lambda values: (values[index] >> low) & mask, high - low + 1, {name}
        return lambda values: values[index], width, {name}
    left, left_width, reads = _expression(node[1], names, widths, slots, depth + 1)
    if op == "!":
        return lambda values: int(not left(values)), 1, reads
    if op == "?:":
        yes, yes_width, yes_reads = _expression(node[2], names, widths, slots, depth + 1)
        no, no_width, no_reads = _expression(node[3], names, widths, slots, depth + 1)
        return (lambda values: yes(values) if left(values) else no(values),
                max(yes_width, no_width), reads | yes_reads | no_reads)
    right, right_width, right_reads = _expression(node[2], names, widths, slots, depth + 1)
    width = 1 if op in BOOLEAN else max(left_width, right_width)
    mask, function = (1 << width) - 1, BINARY[op]
    return lambda values: int(function(left(values), right(values))) & mask, width, reads | right_reads


class DigitalSimulation:
    """Single-clock OOC simulation. Public values are settled at clock-low.

    reset() clears inputs, restores declared state and sets cycles to zero.
    step() performs one complete clock cycle; all edge blocks see old state.
    With no clock, step() advances a sample index without inventing a frequency.
    """

    def __init__(self, source, profile="ooc"):
        if len(source) > 1_000_000:
            raise _error("source exceeds the interactive size limit.")
        chips = compile_chips(source)
        if len(chips) != 1:
            raise _error("select one chip; multi-chip simulation is not yet supported.")
        self.profile = profile
        if profile == "de10-lite":
            from circuitlab.tooling.fpga import compile_de10_lite
            hdl, _, _ = compile_de10_lite(source)
        elif profile == "ooc":
            hdl = chips[0][3]
        else:
            raise _error("choose the ooc or de10-lite profile.")
        modules = _Parser(hdl).parse()
        self.name = next(iter(modules))
        top = modules[self.name]
        widths, initials, drivers, blocks, visible, raw_dependencies = {}, {}, {}, [], set(), {}
        kinds, clock_names, descriptions = {}, {}, {}
        declared_wires = set()
        self.source_bindings = {}
        for row in chips[0][2]:
            if declaration := re.fullmatch(r"(.+?)\s*=\s*wire", row, re.I):
                declared_wires.update(re.sub(r"\[.*", "", name).strip()
                                      for name in expand_pin_banks(declaration[1], 1).split(","))

        def flatten(module, prefix="", aliases=None, ancestors=()):
            if module.name in ancestors or len(ancestors) > 1:
                raise _error("recursive or deeper generated hierarchy is unsupported.")
            names = {name: (aliases or {}).get(name, prefix + name) for name in module.widths}
            if not module.instances:
                # The source core is the leaf module; the board profile may
                # wrap it and connect its input pins to synchronizer outputs.
                self.source_bindings = dict(names)
            for name, width in module.widths.items():
                full = names[name]
                if aliases and name in module.ports:
                    if full not in widths or widths[full] != width:
                        raise _error("invalid generated port width or connection.")
                    continue
                if full in widths:
                    raise _error("duplicate generated hierarchical signal.")
                widths[full] = width
                if name in module.ports or name in module.registers or name in module.constants or name in declared_wires:
                    visible.add(full)
                    kinds[full] = module.ports.get(name) or (
                        "register" if name in module.registers else "constant" if name in module.constants else "wire")
            for name, initial in {**module.registers, **module.constants}.items():
                if initial[0] != "literal":
                    raise _error("only literal generated initializers are supported.")
                initials[names[name]] = initial[1] & ((1 << widths[names[name]]) - 1)
            for name, expression in module.drivers.items():
                full = names[name]
                if full in drivers:
                    raise _error("multiple generated combinational drivers.")
                drivers[full] = expression, names
            for clock, statements in module.blocks:
                if module.ports.get(clock) != "input":
                    raise _error("only top-level input clocks are supported.")
                blocks.append((names[clock], statements, names, {names[n] for n in module.registers}))
            instances = set()
            for child, instance, connections in module.instances:
                if child not in modules or instance in instances:
                    raise _error("unknown or duplicate generated instance.")
                instances.add(instance)
                if set(connections) != set(modules[child].ports) or any(n not in names for n in connections.values()):
                    raise _error("incomplete generated instance connections.")
                flatten(modules[child], prefix + instance + ".",
                        {pin: names[net] for pin, net in connections.items()}, (*ancestors, module.name))

        flatten(top)
        if len(widths) > 4096:
            raise _error("design exceeds 4096 simulated signals.")
        self.clocks = tuple(dict.fromkeys(clock for clock, *_ in blocks))
        if len(self.clocks) > 1 or any(top.ports.get(clock) != "input" for clock in self.clocks):
            raise _error("only one input clock is supported in this simulator.")
        self.inputs = tuple(name for name, direction in top.ports.items() if direction == "input" and name not in self.clocks)
        self.outputs = tuple(name for name, direction in top.ports.items() if direction == "output")
        self.signals = {name: width for name, width in widths.items() if name in visible}
        self._slots = {name: index for index, name in enumerate(widths)}
        self._initial = [initials.get(name, 0) for name in widths]
        self._drivers, edges, dependencies = {}, [], {}
        for name, (expression, names) in drivers.items():
            function, _, reads = _expression(expression, names, widths, self._slots)
            self._drivers[name] = self._slots[name], function, (1 << widths[name]) - 1
            dependencies[name] = reads
        try:
            order = TopologicalSorter({name: reads & drivers.keys() for name, reads in dependencies.items()}).static_order()
            self._settlers = [self._drivers[name] for name in order]
        except CycleError:
            raise _error("combinational feedback is unsupported.") from None

        def compile_statements(statements, names, registers, clock, controls=frozenset(), guards=()):
            result, targets = [], set()
            for statement in statements:
                if statement[0] == "write":
                    target = names.get(statement[1])
                    if target not in registers:
                        raise _error("clocked assignment does not target a register.")
                    expression, _, reads = _expression(statement[2], names, widths, self._slots)
                    dependencies.setdefault(target, set()).update(reads | controls | {clock})
                    clock_names[target] = clock
                    descriptions.setdefault(target, []).append((statement[2], names, guards))
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
                    yes, yes_targets = compile_statements(statement[2], names, registers, clock,
                                                         controls | reads, (*guards, (statement[1], True)))
                    no, no_targets = compile_statements(statement[3], names, registers, clock,
                                                       controls | reads, (*guards, (statement[1], False)))
                    result.append(("if", condition, yes, no))
                    targets.update(yes_targets | no_targets)
            return result, targets

        owners = set()
        for clock, statements, names, registers in blocks:
            compiled, targets = compile_statements(statements, names, registers, clock)
            if owners & targets:
                raise _error("a register has multiple clocked drivers.")
            owners.update(targets)
            edges.append(compiled)
        self._edges = edges
        from circuitlab.math.digital_fast import FastPlan, PlanLimit
        try:
            self._fast_plan = FastPlan(blocks, drivers, widths, self._slots, self.clocks, visible)
        except PlanLimit:
            self._fast_plan = None  # Large connection chains still use exact stepping.

        def visible_reads(name, seen):
            if name in visible:
                return {name}
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
        self.dependencies = raw_dependencies
        self.signal_kinds = kinds
        self.signal_clocks = clock_names

        def clip(text, limit=500):
            return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"

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
                branch_limit = max(0, (limit - len(left) - 8) // 2)
                yes = describe(node[2], names, branch_limit, depth + 1)
                no = describe(node[3], names, branch_limit, depth + 1)
                return clip(f"({left} ? {yes} : {no})", limit)
            right = describe(node[2], names, limit - len(left) - len(op) - 4, depth + 1)
            return clip(f"({left} {op} {right})", limit)

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
        rates = []
        for row in chips[0][2]:
            match = re.fullmatch(r"clock\s*\([^,()]+,\s*([^()]+)\)\s*\{", row)
            if match:
                rates.append(frequency(match[1], 1))
        self.frequency = float(rates[0]) if rates else None
        if profile == "de10-lite":
            self.frequency = 50_000_000.0 if self.clocks else None
        self.reset()

    def _settle(self):
        for slot, expression, mask in self._settlers:
            self._values[slot] = expression(self._values) & mask

    def _publish(self):
        self.values = {name: self._values[self._slots[name]] for name in self.signals}

    def reset(self):
        self._values = self._initial.copy()
        self.cycles = 0
        self._settle()
        self._publish()

    def set_input(self, name, value):
        if name not in self.inputs or not isinstance(value, int):
            raise _error("set a non-clock input to an unsigned integer.")
        width = self.signals[name]
        if not 0 <= value < (1 << width):
            raise _error(f'Input "{name}" requires an unsigned {width}-bit value (0..{(1 << width) - 1}).')
        self._values[self._slots[name]] = value
        self._settle()
        self._publish()

    def step(self):
        def execute(statements):
            for statement in statements:
                if statement[0] == "write":
                    pending[statement[1]] = statement[2](self._values) & statement[3]
                elif statement[0] == "partial":
                    _, target, expression, mask, low = statement
                    previous = pending.get(target, self._values[target])
                    pending[target] = (previous & ~mask) | ((expression(self._values) << low) & mask)
                else:
                    execute(statement[2] if statement[1](self._values) else statement[3])

        if self.clocks:
            slot = self._slots[self.clocks[0]]
            self._values[slot] = 1
            self._settle()
            pending = {}
            for statements in self._edges:
                execute(statements)
            for target, value in pending.items():
                self._values[target] = value
            self._values[slot] = 0
        self._settle()
        self.cycles += 1
        self._publish()
        return self.values

    def advance(self, cycles, *, traces=(), sample=None, deadline=None, sample_every=1):
        """Advance exact state, stopping at a wall-time budget if supplied.

        Simple counters/accumulators skip only mathematically proven intervals.
        Other logic runs the ordinary per-cycle interpreter. Single-bit traces
        bound skipped intervals at their transitions; vector traces are sampled
        at most ``sample_every`` cycles apart. Samples before/after each interval
        preserve the edge positions of those observed single-bit signals.
        """
        if not isinstance(cycles, int) or cycles < 0:
            raise _error("advance requires a nonnegative integer cycle count.")
        if not isinstance(sample_every, int) or sample_every < 1:
            raise _error("sample interval must be a positive integer.")
        start = self.cycles
        target = start + cycles
        while self.cycles < target:
            if deadline is not None and perf_counter() >= deadline:
                break
            limit = target - self.cycles
            if sample is not None:
                limit = min(limit, sample_every)
            plan = self._fast_plan.span(self._values, limit, traces) if limit > 1 and self._fast_plan is not None else None
            if plan is None or plan[0] == 1:
                self.step()
                if sample is not None:
                    sample(self.cycles, self.values)
                continue
            count, increments = plan

            def move(amount):
                for slot, (delta, mask) in increments.items():
                    self._values[slot] = (self._values[slot] + delta * amount) & mask
                self.cycles += amount
                self._settle()
                self._publish()

            if sample is not None:
                move(count - 1)
                sample(self.cycles, self.values)
                move(1)
                sample(self.cycles, self.values)
            else:
                move(count)
        return self.cycles - start
