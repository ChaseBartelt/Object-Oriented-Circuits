"""Conservative, exact event skipping for simple unsigned clocked logic.

An interval is skipped only when every selected register update is a constant
increment and every branch is proved unchanged throughout it. Unsupported
expressions simply return a one-cycle interval for the normal interpreter.
"""

from dataclasses import dataclass
import operator


OPS = {"+": operator.add, "-": operator.sub, "&": operator.and_,
       "|": operator.or_, "^": operator.xor, "==": operator.eq,
       "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge,
       "&&": lambda a, b: bool(a) and bool(b),
       "||": lambda a, b: bool(a) or bool(b)}
BOOLEAN = {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}


class PlanLimit(Exception):
    """A valid design is too deeply connected for the optional fast planner."""


@dataclass(frozen=True)
class _Expr:
    op: str
    args: tuple
    width: int


def _boundary(value, slope, width, limit):
    """First positive integer time an unsigned affine value would wrap."""
    if slope > 0:
        return min(limit, (((1 << width) - 1 - value) // slope) + 1)
    if slope < 0:
        return min(limit, value // -slope + 1)
    return limit


def _comparison(op, value, slope, limit):
    initial = bool(OPS[op](value, 0))
    if slope:
        root = -value // slope
        for step in sorted({1, root, root + 1}):
            if 0 < step < limit and bool(OPS[op](value + slope * step, 0)) != initial:
                return int(initial), step
    return int(initial), limit


class FastPlan:
    def __init__(self, blocks, drivers, widths, slots, clocks, visible):
        self.clock_slots = {slots[name] for name in clocks}
        cache = {}

        def bind(node, names, depth=0):
            if depth > 128:
                raise PlanLimit
            op = node[0]
            if op == "literal":
                return _Expr(op, (node[1],), node[2])
            if op in {"name", "bit"}:
                name = names[node[1]]
                if name not in cache:
                    if name in drivers:
                        expression, context = drivers[name]
                        bound = bind(expression, context, depth + 1)
                        cache[name] = (_Expr("cast", (bound,), widths[name])
                                       if bound.width != widths[name] else bound)
                    else:
                        cache[name] = _Expr("signal", (slots[name],), widths[name])
                expr = cache[name]
                return _Expr("bit", (expr, node[2]), 1) if op == "bit" else expr
            left = bind(node[1], names, depth + 1)
            if op == "!":
                return _Expr(op, (left,), 1)
            if op == "?:":
                yes, no = bind(node[2], names, depth + 1), bind(node[3], names, depth + 1)
                return _Expr(op, (left, yes, no), max(yes.width, no.width))
            right = bind(node[2], names, depth + 1)
            return _Expr(op, (left, right), 1 if op in BOOLEAN else max(left.width, right.width))

        def statements(rows, names):
            result = []
            for row in rows:
                if row[0] == "write":
                    name = names[row[1]]
                    result.append(("write", slots[name], bind(row[2], names), widths[name]))
                else:
                    result.append(("if", bind(row[1], names),
                                   statements(row[2], names), statements(row[3], names)))
            return result

        self.blocks = [statements(rows, names) for _, rows, names, _ in blocks]
        identity = {name: name for name in widths}
        self.traces = {name: bind(("name", name), identity)
                       for name in visible if widths[name] == 1 and name not in clocks}

    def span(self, values, limit, traces=()):
        """Return a proven interval and increments, or None to use one step."""
        writes, guards = {}, []
        # Rising-edge expressions see a high clock. Display traces see it low.
        def evaluate(expr, deltas, horizon, high=True, memo=None):
            if memo is not None and expr in memo:
                return memo[expr]
            op, args, width = expr.op, expr.args, expr.width
            mask = (1 << width) - 1
            if op == "literal":
                result = args[0], 0, horizon
            elif op == "signal":
                slot = args[0]
                value = int(high) if slot in self.clock_slots else values[slot]
                slope = deltas.get(slot, 0)
                result = value, slope, _boundary(value, slope, width, horizon)
            else:
                left, slope, bound = evaluate(args[0], deltas, horizon, high, memo)
                if op == "cast":
                    value = left & mask
                    result = value, slope, _boundary(value, slope, width, bound)
                elif op == "?:":
                    if slope:
                        _, bound = _comparison("!=", left, slope, bound)
                    value, branch_slope, branch_bound = evaluate(
                        args[1] if left else args[2], deltas, horizon, high, memo)
                    result = value & mask, branch_slope, min(bound, branch_bound)
                elif op == "bit":
                    block = 1 << args[1]
                    value = (left // block) & 1
                    if slope > 0:
                        bound = min(bound, (block - 1 - left % block) // slope + 1)
                    elif slope < 0:
                        bound = min(bound, (left % block) // -slope + 1)
                    result = value, 0, bound
                elif op == "!":
                    value, bound = _comparison("==", left, slope, bound)
                    result = value, 0, bound
                else:
                    right, other_slope, other_bound = evaluate(args[1], deltas, horizon, high, memo)
                    bound = min(bound, other_bound)
                    value = int(OPS[op](left, right)) & mask
                    if op in {"+", "-"}:
                        slope = slope + other_slope if op == "+" else slope - other_slope
                        result = value, slope, _boundary(value, slope, width, bound)
                    elif op in {"==", "!=", "<", "<=", ">", ">="}:
                        value, bound = _comparison(op, left - right, slope - other_slope, bound)
                        result = value, 0, bound
                    elif slope or other_slope:
                        result = value, 0, 1
                    else:
                        result = value, 0, bound
            if memo is not None:
                memo[expr] = result
            return result

        def select(rows):
            for row in rows:
                if row[0] == "write":
                    writes[row[1]] = row[2], row[3]
                else:
                    value = evaluate(row[1], {}, limit + 1)[0]
                    guards.append(row[1])
                    select(row[2] if value else row[3])

        for rows in self.blocks:
            select(rows)
        deltas = {}
        for slot, (expr, width) in writes.items():
            if expr.op in {"+", "-"} and expr.args[0].op == "signal" and expr.args[0].args[0] == slot:
                delta = evaluate(expr.args[1], {}, limit + 1)[0]
                deltas[slot] = delta if expr.op == "+" else -delta
            elif expr.op == "+" and expr.args[1].op == "signal" and expr.args[1].args[0] == slot:
                deltas[slot] = evaluate(expr.args[0], {}, limit + 1)[0]
            elif evaluate(expr, {}, limit + 1)[0] & ((1 << width) - 1) == values[slot]:
                deltas[slot] = 0
            else:
                return None

        span, memo = limit, {}
        for slot, (expr, width) in writes.items():
            value, slope, bound = evaluate(expr, deltas, limit + 1, memo=memo)
            mask = (1 << width) - 1
            if slope != deltas[slot] or value & mask != (values[slot] + slope) & mask:
                return None
            # Assignment truncation can wrap before an expression's width does.
            span = min(span, bound, _boundary(value & mask, slope, width, limit + 1))
        for guard in guards:
            _, slope, bound = evaluate(guard, deltas, limit + 1, memo=memo)
            # A vector used as a condition is true until it reaches zero.
            if slope:
                _, bound = _comparison("!=", evaluate(guard, deltas, limit + 1)[0], slope, bound)
            span = min(span, bound)
        for name in traces:
            expr = self.traces.get(name)
            if expr is not None:
                _, slope, bound = evaluate(expr, deltas, limit + 1, high=False)
                span = min(span, 1 if slope else bound)
        return max(1, span), {slot: (delta, (1 << writes[slot][1]) - 1)
                              for slot, delta in deltas.items() if delta}
