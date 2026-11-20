"""Compile optional mathematical behavior without creating schematic parts."""

import ast
import math
import re
from dataclasses import replace

from circuitlab.math.numbers import NUMBER, SCALES, real, scalar
from circuitlab.model.graph import CircuitError, MathEquation
from circuitlab.language.syntax import MATH_BRACE, MATH_INLINE, parse_wires
from circuitlab.language.properties import PROPERTY_DECLARATION


FUNCTION_ARITY = {"abs": (1, 1), "sqrt": (1, 1), "sin": (1, 1), "cos": (1, 1),
                  "tan": (1, 1), "exp": (1, 1), "log": (1, 1), "log10": (1, 1),
                  "floor": (1, 1), "ceil": (1, 1), "pow": (2, 2),
                  "min": (2, 16), "max": (2, 16), "clamp": (3, 3), "ifelse": (3, 3),
                  "square": (2, 2)}
STATE_ARITY = {"integrate": (1, 2), "delay": (2, 3), "transition": (3, 3)}
LOCAL_ASSIGNMENT = re.compile(r"(?:let\s+)?([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)\s*=(?!=)\s*(.+)")
OPERATORS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**",
             ast.Mod: "%", ast.FloorDiv: "//", ast.BitAnd: "&", ast.BitOr: "|",
             ast.BitXor: "^", ast.LShift: "<<", ast.RShift: ">>"}
COMPARISONS = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!="}
UNIT_LITERAL = re.compile(rf"(?<![A-Za-z0-9_.])({NUMBER})\s*([pnuµμmkMG]?(?:ohm|Hz|V|A|s)|[pnuµμmkMG])(?![A-Za-z0-9_])")


def compile_equation(text, digital, binding, line, name, *, properties=None, variables=None, state_prefix=None):
    """Parse a bounded expression grammar; never evaluate source as Python."""
    assignment = re.fullmatch(r"\s*(V\s*\([^()]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.+)", text)
    if not assignment:
        raise CircuitError("Use V(output) = expression, or output = expression in digital math.", line)
    lhs, expression = assignment.groups()
    voltage = re.fullmatch(r"V\s*\(([^()]*)\)", lhs, re.I)
    if voltage:
        pins = parse_wires(voltage[1])
        if len(pins) not in {1, 2}:
            raise CircuitError("Use V(node) or V(node, reference).", line)
        output, reference = (*pins, "0")[:2]
    elif digital:
        output, reference = lhs, "0"
    else:
        raise CircuitError("Analog math assigns V(output) or V(output, reference).", line)
    output, reference = binding.wire(output), binding.wire(reference)
    if output == reference:
        raise CircuitError("A math output needs two distinct voltage nodes.", line)
    tree, inputs = compile_expression(expression, digital, binding, line,
                                      properties=properties, variables=variables,
                                      state_prefix=state_prefix or name)
    return MathEquation(name, output, reference, tree, inputs, digital,
                        line, binding.path, binding.instance)


def compile_expression(expression, digital, binding, line, *, properties=None, variables=None, state_prefix="@state"):
    """Lower an expression to bounded immutable IR, including scoped state calls."""
    properties = properties or {}
    variables = variables or {}
    if len(expression) > 4096:
        raise CircuitError("Use at most 4096 characters in a math expression.", line)
    replacements, inputs = {}, []
    reserved = set(re.findall(r"\b[A-Za-z_]\w*", expression))

    def voltage_read(match):
        pins = parse_wires(match[1])
        if len(pins) not in {1, 2}:
            raise CircuitError("Use V(node) or V(node, reference).", line)
        high, low = (*pins, "0")[:2]
        high, low = binding.wire(high), binding.wire(low)
        key = f"__voltage{len(replacements)}"
        while key in reserved or key in replacements:
            key += "_"
        replacements[key] = ("v", high, low)
        inputs.extend((high, low))
        return key

    expression = re.sub(r"\bV\s*\(([^()]*)\)", voltage_read, expression, flags=re.I)

    def unit_literal(match):
        number, unit = match.groups()
        value = float(number) * SCALES[unit] if unit in SCALES else real(
            scalar(number + unit, "ohm" if unit.endswith("ohm") else "Hz" if unit.endswith("Hz") else unit[-1]))
        if not math.isfinite(value):
            raise CircuitError("Every math literal must be finite.", line)
        return f"({value!r})"

    expression = UNIT_LITERAL.sub(unit_literal, expression)
    expression = re.sub(r"\btrue\b", "True", expression, flags=re.I)
    expression = re.sub(r"\bfalse\b", "False", expression, flags=re.I)
    expression = re.sub(r"!(?!=)", " not ", expression).replace("&&", " and ").replace("||", " or ")
    try:
        parsed = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise CircuitError("Invalid math expression.", line) from None
    if sum(1 for _ in ast.walk(parsed)) > 512:
        raise CircuitError("Use at most 512 expression nodes in mathematical behavior.", line)

    state_count = 0

    def variable(identifier):
        if callable(variables):
            return variables(identifier)
        return variables.get(identifier)

    def constant_argument(tree, label):
        if any(node[0] in {"v", "bit", "time", "state"} for node in ir_nodes(tree)):
            raise CircuitError(f"{label} must be a finite constant expression.", line)
        from circuitlab.math.behavioral import evaluate
        try:
            value, _ = evaluate(tree, {})
            if not math.isfinite(value):
                raise ValueError("non-finite value")
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            raise CircuitError(f"{label} must be a finite constant expression.", line) from None
        return value

    def lower(node, depth=0):
        nonlocal state_count
        if depth > 64:
            raise CircuitError("Math expressions may nest at most 64 levels.", line)
        child = lambda item: lower(item, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
            if not math.isfinite(node.value):
                raise CircuitError("Math constants must be finite.", line)
            return ("c", node.value)
        if isinstance(node, ast.Name):
            if node.id in replacements:
                return replacements[node.id]
            if node.id in properties:
                return ("c", properties[node.id])
            local = variable(node.id)
            if local is not None:
                return local
            if node.id in {"pi", "e"}:
                return ("c", getattr(math, node.id))
            if not digital and node.id in {"t", "time"}:
                return ("time",)
            if digital and not node.id.startswith("__"):
                net = binding.wire(node.id)
                inputs.extend((net, "0"))
                return ("bit", net, "0")
            raise CircuitError(f'Unknown math name "{node.id}". Read electrical nodes with V(name).', line)
        if isinstance(node, ast.Attribute):
            pieces = []
            current = node
            while isinstance(current, ast.Attribute):
                pieces.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                identifier = ".".join((current.id, *reversed(pieces)))
                if identifier in properties:
                    return ("c", properties[identifier])
                local = variable(identifier)
                if local is not None:
                    return local
            raise CircuitError("Dotted math names must refer to declared custom properties or internal variables.", line)
        if isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub, ast.Not, ast.Invert}:
            return ("unary", {ast.UAdd: "+", ast.USub: "-", ast.Not: "!", ast.Invert: "~"}[type(node.op)], child(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return ("binary", OPERATORS[type(node.op)], child(node.left), child(node.right))
        if isinstance(node, ast.BoolOp):
            return ("bool", "and" if isinstance(node.op, ast.And) else "or", *(child(v) for v in node.values))
        if isinstance(node, ast.Compare):
            return ("compare", tuple(COMPARISONS[type(op)] for op in node.ops),
                    child(node.left), *(child(v) for v in node.comparators))
        if isinstance(node, ast.IfExp):
            return ("call", "ifelse", child(node.test), child(node.body), child(node.orelse))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            function = node.func.id
            if function in STATE_ARITY:
                if digital:
                    raise CircuitError("Stateful equations belong in analog blocks, not digital logic.", line)
                low, high = STATE_ARITY[function]
                if not low <= len(node.args) <= high:
                    raise CircuitError(f"Unsupported state-function argument count: {function}.", line)
                arguments = [child(node.args[0])]
                if function == "integrate":
                    initial = (("c", 0) if len(node.args) == 1 or
                               (isinstance(node.args[1], ast.Name) and node.args[1].id in {"t", "time"})
                               else child(node.args[1]))
                    constant_argument(initial, "Integrator initial value")
                    arguments.append(initial)
                else:
                    for arg in node.args[1:]:
                        duration = child(arg)
                        if constant_argument(duration, "Delay") < 0:
                            raise CircuitError("Delay values must be nonnegative.", line)
                        arguments.append(duration)
                    if function == "delay" and len(arguments) == 3:
                        function = "transition"
                key = f"{state_prefix}:{state_count}"
                state_count += 1
                return ("state", function, key, *arguments)
            if function in FUNCTION_ARITY:
                low, high = FUNCTION_ARITY[function]
                if low <= len(node.args) <= high:
                    return ("call", function, *(child(v) for v in node.args))
            raise CircuitError(f'Unsupported math function or argument count: {function}.', line)
        raise CircuitError("Use numeric math operators, V(node), and supported math functions only.", line)

    try:
        tree = lower(parsed.body)
    except (KeyError, OverflowError, RecursionError):
        raise CircuitError("Unsupported or excessively large math expression.", line) from None
    _validate_ir(tree, line)
    inputs = tuple(dict.fromkeys(net for node in ir_nodes(tree) if node[0] in {"v", "bit"}
                                 for net in node[1:3]))
    return tree, inputs


IR_KINDS = {"c", "v", "bit", "time", "unary", "binary", "bool", "compare", "call", "state"}


def ir_nodes(tree):
    """Visit shared IR nodes once, retaining a bound on dependency traversal."""
    pending, seen = [tree], set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        pending.extend(value for value in node[1:] if isinstance(value, tuple) and value
                       and isinstance(value[0], str) and value[0] in IR_KINDS)


def _validate_ir(tree, line):
    sizes = {}
    def measure(node):
        if id(node) in sizes:
            return sizes[id(node)]
        children = [value for value in node[1:] if isinstance(value, tuple) and value
                    and isinstance(value[0], str) and value[0] in IR_KINDS]
        measured = [measure(child) for child in children]
        count = 1 + sum(size for size, _ in measured)
        depth = 1 + max((depth for _, depth in measured), default=0)
        if count > 4096 or depth > 64:
            raise CircuitError("Expanded mathematical expressions may use at most 4096 nodes and 64 levels.", line)
        sizes[id(node)] = count, depth
        return count, depth
    measure(tree)


def compile_scope_equations(rows, properties, *, issues=None, unresolved=()):
    """Resolve scoped algebraic variables before emitting voltage equations.

    Rows are (text, digital, Binding, line, override priority). Internal
    variables are immutable expression definitions; voltage equations stay
    simultaneous and never become schematic components.
    """
    local_rows, outputs = {}, []
    for index, row in enumerate(rows):
        text, digital, binding, line, priority = row
        local = LOCAL_ASSIGNMENT.fullmatch(text)
        if local and (not digital or text.startswith("let ")):
            name = local[1]
            from circuitlab.language.properties import validate_property_name
            try:
                validate_property_name(name)
            except CircuitError as error:
                raise CircuitError(error.message, line, path=binding.path) from None
            if name in properties:
                raise CircuitError(f'Internal variable "{name}" is already declared as a property.', line, path=binding.path)
            old = local_rows.get(name)
            if old is not None and old[0][4] == priority:
                raise CircuitError(f'Internal variable "{name}" is already declared.', line, path=binding.path)
            if old is None or old[0][4] < priority:
                local_rows[name] = row, local[2]
        else:
            outputs.append((index, row))
    cache, active, failures = {}, [], {}
    unresolved = set(unresolved)

    def resolve(identifier):
        if identifier in unresolved:
            raise CircuitError(f'Custom property "{identifier}" has an unresolved model value.')
        if identifier not in local_rows:
            return None
        if identifier in failures:
            raise failures[identifier]
        if identifier in cache:
            return cache[identifier]
        row, expression = local_rows[identifier]
        text, digital, binding, line, priority = row
        if identifier in active:
            raise CircuitError("Cyclic internal variable dependency: " + " -> ".join((*active, identifier)) + ".",
                               line, path=binding.path)
        if len(active) >= 64:
            raise CircuitError("Internal variable dependencies may nest at most 64 levels.", line, path=binding.path)
        active.append(identifier)
        prefix = f"@state:{binding.scope}:{binding.instance}:local:{identifier}"
        try:
            tree, _ = compile_expression(expression, digital, binding, line, properties=properties,
                                         variables=resolve, state_prefix=prefix)
        except CircuitError as error:
            issue = CircuitError(error.message, error.line or line, error.code, path=error.path or binding.path)
            failures[identifier] = issue
            raise issue from None
        finally:
            active.pop()
        cache[identifier] = tree
        return tree
    for identifier in local_rows:
        try:
            resolve(identifier)
        except CircuitError as error:
            if issues is None:
                raise
            issues.append(error)
    equations = []
    for index, (text, digital, binding, line, priority) in outputs:
        target = re.sub(r"\s+", "", text.partition("=")[0])
        prefix = f"@state:{binding.scope}:{binding.instance}:output:{target}"
        try:
            equation = compile_equation(text, digital, binding, line, f"@math{index}",
                                        properties=properties, variables=resolve, state_prefix=prefix)
        except CircuitError as error:
            issue = CircuitError(error.message, error.line or line, error.code, path=error.path or binding.path)
            if issues is None:
                raise issue from None
            issues.append(issue)
            continue
        equations.append(replace(equation, priority=priority))
    return equations


def remap_equation(equation, net, *, name=None, owner=None, state_key=None):
    """Translate every referenced terminal after aliasing/file linking."""
    def visit(tree):
        if tree[0] in {"v", "bit"}:
            return tree[0], net(tree[1]), net(tree[2])
        if tree[0] == "state":
            key = state_key(tree[2]) if state_key else tree[2]
            return "state", tree[1], key, *(visit(child) for child in tree[3:])
        return tuple(visit(value) if isinstance(value, tuple) and value and
                     isinstance(value[0], str) and value[0] in IR_KINDS
                     else value for value in tree)
    return replace(equation, name=equation.name if name is None else name,
                   output=net(equation.output), reference=net(equation.reference),
                   inputs=tuple(dict.fromkeys(map(net, equation.inputs))), tree=visit(equation.tree),
                   owner=equation.owner if owner is None else owner)


def digital_math_rows(rows):
    """Remove optional digital math wrappers before existing HDL compilation."""
    result, stack = [], []
    for row in rows:
        text, line = row[:2]
        if PROPERTY_DECLARATION.fullmatch(text):
            raise CircuitError("Analog properties use electrical simulation, not Verilog.", line)
        if re.fullmatch(r"properties\s*\{", text, re.I):
            raise CircuitError("Custom properties use electrical analog/digital math. Use const declarations in HDL chips.", line)
        match = MATH_BRACE.fullmatch(text)
        inline = MATH_INLINE.fullmatch(text)
        if match:
            if match[1].casefold() != "digital":
                raise CircuitError("Analog mathematics uses electrical simulation, not Verilog.", line)
            stack.append(True)
        elif inline:
            if inline[1].casefold() != "digital":
                raise CircuitError("Analog mathematics uses electrical simulation, not Verilog.", line)
            result.append((inline[2], *row[1:]))
        elif text == "}" and stack:
            hidden = stack.pop()
            if not hidden:
                result.append(row)
        else:
            if text.endswith("{"):
                stack.append(False)
            result.append(row)
    if stack:
        raise CircuitError("Close the digital math block with }.", rows[-1][1] if rows else None)
    return result
