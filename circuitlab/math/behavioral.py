"""Evaluate mathematical behavior and expand it privately for the solvers."""

import math
import operator
from dataclasses import replace

import numpy as np

from circuitlab.model.graph import CircuitError, Component


COMPARE = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
           "<=": operator.le, "==": operator.eq, "!=": operator.ne}


def add_grad(first, second, a=1, b=1):
    result = {key: a * value for key, value in first.items()}
    for key, value in second.items():
        result[key] = result.get(key, 0) + b * value
    return {key: value for key, value in result.items() if value != 0}


def evaluate(tree, voltages, time=0, *, state=None):
    """Return value and sparse voltage gradient; calls/attributes cannot execute."""
    kind = tree[0]
    if kind == "c":
        return tree[1], {}
    if kind == "time":
        return time, {}
    if kind == "state":
        if state is None:
            raise ValueError("stateful math requires a time-domain state context")
        return state.evaluate(tree, voltages, time)
    if kind in {"v", "bit"}:
        value = voltages[tree[1]] - voltages[tree[2]]
        if kind == "bit":
            return int(value >= .5), {}
        return value, add_grad({tree[1]: 1}, {tree[2]: 1}, 1, -1)
    def run(node):
        result = evaluate(node, voltages, time, state=state)
        if isinstance(result[0], int) and result[0].bit_length() > 4096:
            raise ValueError("digital math is limited to 4096 bits")
        return result
    if kind == "unary":
        value, gradient = run(tree[2])
        if tree[1] == "+":
            return value, gradient
        if tree[1] == "-":
            return -value, {n: -v for n, v in gradient.items()}
        return (int(not value) if tree[1] == "!" else ~int(value)), {}
    if kind == "bool":
        for child in tree[2:]:
            value, _ = run(child)
            if (tree[1] == "and" and not value) or (tree[1] == "or" and value):
                return int(bool(value)), {}
        return int(tree[1] == "and"), {}
    if kind == "compare":
        first, _ = run(tree[2])
        for operation, child in zip(tree[1], tree[3:]):
            second, _ = run(child)
            if not COMPARE[operation](first, second):
                return 0, {}
            first = second
        return 1, {}
    if kind == "binary":
        op = tree[1]
        a, da = run(tree[2])
        b, db = run(tree[3])
        if op in {"+", "-"}:
            return (a + b, add_grad(da, db)) if op == "+" else (a - b, add_grad(da, db, 1, -1))
        if op == "*":
            return a * b, add_grad(da, db, b, a)
        if op == "/":
            return a / b, add_grad(da, db, 1 / b, -a / b ** 2)
        if op == "**":
            if abs(b) > 64:
                raise ValueError("power exponent must be between -64 and 64")
            value = a ** b
            first = b * a ** (b - 1) if b and da else 0
            second = value * math.log(a) if db else 0
            return value, add_grad(da, db, first, second)
        if op in {"<<", ">>"} and not 0 <= b <= 4096:
            raise ValueError("shift count must be between 0 and 4096")
        operations = {"%": operator.mod, "//": operator.floordiv, "&": operator.and_,
                      "|": operator.or_, "^": operator.xor, "<<": operator.lshift, ">>": operator.rshift}
        value = operations[op](int(a), int(b))
        if value.bit_length() > 4096:
            raise ValueError("digital math is limited to 4096 bits")
        return value, {}
    function = tree[1]
    if function == "ifelse":
        condition, _ = run(tree[2])
        return run(tree[3] if condition else tree[4])
    arguments = [run(child) for child in tree[2:]]
    if function == "pow":
        return evaluate(("binary", "**", *tree[2:]), voltages, time, state=state)
    if function == "square":
        phase, duty = arguments
        if not 0 <= duty[0] <= 1:
            raise ValueError("square duty must be between 0 and 1")
        return int(phase[0] % 1 < duty[0]), {}
    if function in {"min", "max"}:
        return (min if function == "min" else max)(arguments, key=lambda item: item[0])
    if function == "clamp":
        value, low, high = arguments
        if low[0] > high[0]:
            raise ValueError("clamp minimum exceeds maximum")
        return low if value[0] < low[0] else high if value[0] > high[0] else value
    value, gradient = arguments[0]
    if function == "abs":
        answer, slope = abs(value), 1 if value > 0 else -1 if value < 0 else 0
    elif function in {"floor", "ceil"}:
        answer, slope = getattr(math, function)(value), 0
    else:
        answer = getattr(math, function)(value)
        slope = {"sin": lambda: math.cos(value), "cos": lambda: -math.sin(value),
                 "tan": lambda: 1 / math.cos(value) ** 2,
                 "exp": lambda: answer, "log": lambda: 1 / value,
                 "log10": lambda: 1 / (value * math.log(10)),
                 "sqrt": lambda: .5 / answer if answer else 0}[function]()
    return answer, {n: slope * v for n, v in gradient.items()}


def equation_value(equation, voltages, time=0, *, state=None):
    try:
        value, gradient = evaluate(equation.tree, voltages, time, state=state)
        if not math.isfinite(value) or not all(math.isfinite(v) for v in gradient.values()):
            raise ValueError("non-finite result")
        return value, gradient
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        raise CircuitError(f"Invalid mathematical behavior: {error}.", equation.line,
                           "MathDomain", path=equation.path) from None


def linear_form(tree):
    """Return (constant, node coefficients), or None for nonlinear/time behavior."""
    if tree[0] == "c":
        return tree[1], {}
    if tree[0] == "state":
        return None
    if tree[0] == "v":
        return 0, add_grad({tree[1]: 1}, {tree[2]: 1}, 1, -1)
    if tree[0] == "unary" and tree[1] in {"+", "-"}:
        child = linear_form(tree[2])
        if child is not None:
            factor = 1 if tree[1] == "+" else -1
            return child[0] * factor, {n: v * factor for n, v in child[1].items()}
    if tree[0] == "call" and tree[1] == "pow":
        return linear_form(("binary", "**", *tree[2:]))
    if tree[0] == "binary" and tree[1] == "**" and tree[3][0] == "c":
        if tree[3][1] == 0:
            return 1, {}
        if tree[3][1] == 1:
            return linear_form(tree[2])
    if tree[0] == "binary" and tree[1] in {"+", "-", "*", "/"}:
        first, second = linear_form(tree[2]), linear_form(tree[3])
        if first is None or second is None:
            return None
        a, da = first
        b, db = second
        if tree[1] in {"+", "-"}:
            factor = 1 if tree[1] == "+" else -1
            return a + factor * b, add_grad(da, db, 1, factor)
        if tree[1] == "*" and not (da and db):
            return a * b, add_grad(da, db, b, a)
        if tree[1] == "/" and not db and b:
            return a / b, {n: v / b for n, v in da.items()}
    # Expressions without voltage/time/bit dependencies can be folded safely.
    def variable(node):
        return node[0] in {"v", "bit", "time", "state"} or any(
            variable(value) for value in node[1:] if isinstance(value, tuple)
            and value and isinstance(value[0], str)
            and value[0] in {"c", "v", "bit", "time", "state", "unary", "binary", "bool", "compare", "call"})
    if not variable(tree):
        try:
            value, _ = evaluate(tree, {})
            return (value, {}) if math.isfinite(value) else None
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            pass
    return None


def expand_math(circuit):
    """Create private B sources only in a numerical/export copy of the graph."""
    if not circuit.behavior:
        return circuit
    hidden = [Component(eq.name, "B", eq.output, eq.reference, behavior=eq)
              for eq in circuit.behavior]
    return replace(circuit, components=[*circuit.components, *hidden], behavior=[])


def fold_math(network, original):
    """Keep hidden model equations out of public component-indexed results."""
    if not original.behavior:
        return network
    by_name = {name: i for i, name in enumerate(network.names)}
    indices = [by_name[part.name] for part in original.components]
    names = tuple(part.name for part in original.components)
    return replace(network, names=names, branch_voltage=network.branch_voltage[indices],
                   branch_current=network.branch_current[indices], branch_power=network.branch_power[indices],
                   terminal_current={name: value for name, value in network.terminal_current.items() if name in names})
