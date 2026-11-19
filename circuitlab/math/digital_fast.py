"""Conservative, exact event skipping for simple unsigned clocked logic.

An interval is skipped only when every selected register update is a constant
increment and every branch is proved unchanged throughout it. Unsupported
expressions simply return a one-cycle interval for the normal interpreter.
"""
# Acceleration is optional: skip cycles only when register increments and branch boundaries
# are mathematically proven to remain equivalent to ordinary stepping. An unsupported or
# ambiguous expression falls back to the exact interpreter instead of estimating behavior.

from dataclasses import dataclass
import operator


# Use the same unsigned operators as the ordinary digital interpreter.
# Evaluation below applies width masks; the operator functions themselves
# use Python integers, which have no fixed hardware width.
OPS = {"+": operator.add, "-": operator.sub, "&": operator.and_,
       "|": operator.or_, "^": operator.xor, "==": operator.eq,
       "!=": operator.ne, "<": operator.lt, "<=": operator.le,
       ">": operator.gt, ">=": operator.ge, "<<": operator.lshift, ">>": operator.rshift,
       "&&": lambda a, b: bool(a) and bool(b),
       "||": lambda a, b: bool(a) or bool(b)}
# These operators produce one-bit expressions; shifts retain the left
# operand's width, while other binary operators use the wider operand.
BOOLEAN = {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}


# The caller catches this to disable acceleration while keeping normal
# simulation available. It is a planning limit, not a circuit syntax error.
class PlanLimit(Exception):
    """A valid design is too deeply connected for the optional fast planner."""


# Binding replaces source names with state slots and expands driven wires.
# The frozen tree can be a dictionary key, letting one span reuse work
# for shared subexpressions while its state snapshot stays fixed.
@dataclass(frozen=True)
class _Expr:
    op: str
    # args contains child expressions or operator-specific data, such as a
    # literal integer, signal slot, or bit offset. width controls truncation.
    args: tuple
    width: int


# An unsigned wrap ends the interval over which one affine formula is valid.
def _boundary(value, slope, width, limit):
    """First positive integer time an unsigned affine value would wrap."""
    # For x(t) = value + slope*t, valid unsigned values are 0..2**width-1.
    # Floor division counts complete safe steps; +1 finds the first unsafe
    # step. For example, an 8-bit value of 253 increasing by 1 wraps at t=3.
    if slope > 0:
        return min(limit, (((1 << width) - 1 - value) // slope) + 1)
    # A decreasing value leaves the unsigned range when it first becomes
    # negative. With value=2 and slope=-1, that boundary is also t=3.
    if slope < 0:
        return min(limit, value // -slope + 1)
    # A constant value has no wrap event within the requested horizon.
    return limit


# Compare an affine difference with zero and find its first truth change.
# The caller supplies left-right and left_slope-right_slope, so this
# handles both moving operands without searching every intervening cycle.
def _comparison(op, value, slope, limit):
    initial = bool(OPS[op](value, 0))
    if slope:
        # The continuous zero crossing lies between root and root+1. Testing both
        # also catches equality being true for just one integer step; testing t=1
        # handles a condition that changes immediately after the starting point.
        root = -value // slope
        for step in sorted({1, root, root + 1}):
            # Only positive steps strictly inside the horizon need a separate event.
            # Return the current truth as 0/1, along with the first changed step.
            if 0 < step < limit and bool(OPS[op](value + slope * step, 0)) != initial:
                return int(initial), step
    return int(initial), limit


# Construction binds the design once. span() then uses the current state
# to propose increments and prove how many cycles can share those rules.
# Neither phase advances registers; the simulation applies the result.
class FastPlan:
    def __init__(self, blocks, drivers, widths, slots, clocks, visible):
        # slots maps canonical signal names to indexes in the simulator state.
        # Clock slots are evaluated at an explicit phase rather than read from
        # the stored state, which normally rests with the clock low.
        self.clock_slots = {slots[name] for name in clocks}
        cache = {}

        # names maps identifiers in this instance to canonical signal names.
        # Inline combinational drivers so the proof can see through wires to
        # their actual register/input dependencies.
        def bind(node, names, depth=0):
            # Bound dependency expansion even when a legal design has a very long
            # chain of driven wires. Giving up here leaves exact stepping available.
            if depth > 128:
                raise PlanLimit
            op = node[0]
            if op == "literal":
                return _Expr(op, (node[1],), node[2])
            if op == "concat":
                # Concatenation width is the sum of its pieces, in source order.
                pieces = tuple(bind(part, names, depth + 1) for part in node[1])
                return _Expr(op, pieces, sum(piece.width for piece in pieces))
            if op in {"name", "bit", "slice"}:
                # Cache by canonical name so different instance-local spellings of the
                # same signal reuse one bound expression.
                name = names[node[1]]
                if name not in cache:
                    if name in drivers:
                        expression, context = drivers[name]
                        bound = bind(expression, context, depth + 1)
                        # A driven wire can be narrower or wider than its RHS. Preserve that
                        # assignment boundary explicitly instead of silently changing the
                        # arithmetic width of the driver itself.
                        cache[name] = (_Expr("cast", (bound,), widths[name])
                                       if bound.width != widths[name] else bound)
                    else:
                        # An undriven expression leaf reads a register, external input, or other
                        # stored signal through its state slot.
                        cache[name] = _Expr("signal", (slots[name],), widths[name])
                expr = cache[name]
                # Slice nodes store the inclusive high and low indexes. The bound form
                # needs only the low offset and the number of bits to retain.
                if op == "slice":
                    return _Expr("slice", (expr, node[3]), node[2] - node[3] + 1)
                return _Expr("bit", (expr, node[2]), 1) if op == "bit" else expr
            left = bind(node[1], names, depth + 1)
            if op == "!":
                return _Expr(op, (left,), 1)
            # Bind both alternatives once; evaluation follows only the currently
            # selected branch and stops when that choice might change.
            if op == "?:":
                yes, no = bind(node[2], names, depth + 1), bind(node[3], names, depth + 1)
                return _Expr(op, (left, yes, no), max(yes.width, no.width))
            right = bind(node[2], names, depth + 1)
            width = 1 if op in BOOLEAN else left.width if op in {"<<", ">>"} else max(left.width, right.width)
            return _Expr(op, (left, right), width)

        # Keep statement and branch order, but replace names with slots and
        # expressions with bound trees. This mirrors the normal execution order.
        def statements(rows, names):
            result = []
            for row in rows:
                if row[0] == "write":
                    # Only whole-register writes are planned. Overlapping bit/slice writes
                    # need the interpreter to merge pending updates in source order (NBA:
                    # nonblocking assignment, with RHS values read before the edge commits).
                    if len(row) != 3:
                        raise PlanLimit  # Partial NBA writes retain exact source ordering in the interpreter.
                    name = names[row[1]]
                    result.append(("write", slots[name], bind(row[2], names), widths[name]))
                else:
                    result.append(("if", bind(row[1], names),
                                   statements(row[2], names), statements(row[3], names)))
            return result

        # Each clock block retains its own identifier context during binding.
        self.blocks = [statements(rows, names) for _, rows, names, _ in blocks]
        identity = {name: name for name in widths}
        # Observed one-bit signals need transition boundaries so a skipped span
        # does not hide an edge. Vector sampling intervals are limited by the
        # caller; clocks are handled separately rather than traced as data here.
        self.traces = {name: bind(("name", name), identity)
                       for name in visible if widths[name] == 1 and name not in clocks}

    # Return a safe span only when every relevant branch and update remains predictable.
    # Uncertain dependencies fall back to one-cycle interpretation rather than guessing.
    def span(self, values, limit, traces=()):
        """Return a proven interval and increments, or None to use one step."""
        # writes maps a selected destination slot to (RHS expression, width).
        # guards records the conditions on the currently selected paths; those
        # paths must stay selected for all edges included in the bulk update.
        writes, guards = {}, []
        # Rising-edge expressions see a high clock. Display traces see it low.
        # Return (value_now, change_per_cycle, boundary). Before boundary, the
        # expression is represented exactly by value_now + change_per_cycle*t.
        # A zero slope can still have a finite boundary, as with a comparison.
        #
        # The proof covers integer t from 0 through boundary-1. A bulk update
        # may land on that boundary: its last RHS was read one cycle earlier.
        # The next plan must reconsider the state and any condition that changed.
        def evaluate(expr, deltas, horizon, high=True, memo=None):
            # A memo is valid only for one fixed set of deltas, horizon, and clock
            # phase. The final register/guard checks share one; trace checks do not.
            if memo is not None and expr in memo:
                return memo[expr]
            op, args, width = expr.op, expr.args, expr.width
            # Masking models finite-width unsigned results rather than unbounded
            # Python arithmetic; the planner stops before a predicted affine value
            # would cease to describe that masked result.
            mask = (1 << width) - 1
            # A literal is constant throughout the supplied horizon.
            if op == "literal":
                result = args[0], 0, horizon
            # Assemble the current bits, but do not infer an affine slope through
            # concatenation. Any moving piece restricts the result to one cycle;
            # constant pieces remain valid until their earliest own boundary.
            elif op == "concat":
                value, bound = 0, horizon
                for piece in args:
                    current, slope, span = evaluate(piece, deltas, horizon, high, memo)
                    value = (value << piece.width) | current
                    bound = min(bound, 1 if slope else span)
                result = value, 0, bound
            elif op == "signal":
                slot = args[0]
                # Clocked RHS/guard evaluation sees the rising-edge phase. Other leaves
                # start at the current stored value; a missing delta means they hold.
                value = int(high) if slot in self.clock_slots else values[slot]
                slope = deltas.get(slot, 0)
                result = value, slope, _boundary(value, slope, width, horizon)
            else:
                left, slope, bound = evaluate(args[0], deltas, horizon, high, memo)
                # Apply the destination wire width and recompute the wrap boundary.
                # Narrowing can cause an earlier wrap than the child expression predicts.
                if op == "cast":
                    value = left & mask
                    result = value, slope, _boundary(value, slope, width, bound)
                # A vector condition uses nonzero truth. Bound that truth change, then
                # combine it with the selected branch's own validity interval.
                elif op == "?:":
                    if slope:
                        _, bound = _comparison("!=", left, slope, bound)
                    value, branch_slope, branch_bound = evaluate(
                        args[1] if left else args[2], deltas, horizon, high, memo)
                    result = value & mask, branch_slope, min(bound, branch_bound)
                # Bits below the selected low offset may move without changing the
                # selected field. Treat the field as constant until left crosses the
                # next multiple of 2**offset, or an earlier child boundary is reached.
                elif op in {"bit", "slice"}:
                    block = 1 << args[1]
                    value = (left // block) & mask
                    # left % block is the position within the current block. Count steps
                    # to leave its upper edge; for a negative slope, use its lower edge.
                    if slope > 0:
                        bound = min(bound, (block - 1 - left % block) // slope + 1)
                    elif slope < 0:
                        bound = min(bound, (left % block) // -slope + 1)
                    result = value, 0, bound
                # Logical NOT is true exactly when the operand equals zero, so it uses
                # the same discrete crossing calculation as explicit comparisons.
                elif op == "!":
                    value, bound = _comparison("==", left, slope, bound)
                    result = value, 0, bound
                else:
                    right, other_slope, other_bound = evaluate(args[1], deltas, horizon, high, memo)
                    # A binary expression is valid only while both operand descriptions
                    # remain valid, even if their slopes happen to cancel.
                    bound = min(bound, other_bound)
                    # An unsigned shift by the full width or more produces zero. Handle it
                    # directly, avoiding an enormous intermediate Python integer for <<.
                    value = (0 if op in {"<<", ">>"} and right >= width
                             else int(OPS[op](left, right)) & mask)
                    # Sums and differences of affine operands are affine until a child
                    # changes regime or the result itself wraps at this expression width.
                    if op in {"+", "-"}:
                        slope = slope + other_slope if op == "+" else slope - other_slope
                        result = value, slope, _boundary(value, slope, width, bound)
                    # The comparison result is constant until the affine difference crosses
                    # its threshold. Operand wrap bounds above keep this unsigned analysis
                    # from incorrectly extending a comparison across a wrap.
                    elif op in {"==", "!=", "<", "<=", ">", ">="}:
                        value, bound = _comparison(op, left - right, slope - other_slope, bound)
                        result = value, 0, bound
                    else:
                        # Bitwise operators, shifts, and logical AND/OR have no general affine
                        # rule here. Moving operands force one-cycle handling; operands proven
                        # constant can keep their shared interval.
                        result = value, 0, 1 if slope or other_slope else bound
            if memo is not None:
                memo[expr] = result
            return result

        # Choose branches using only the current pre-edge state. Record pending
        # writes without changing that state; a later selected write to the same
        # slot replaces an earlier one, matching the interpreter's full writes.
        def select(rows):
            for row in rows:
                if row[0] == "write":
                    writes[row[1]] = row[2], row[3]
                else:
                    # Empty deltas are sufficient to choose the branch now. After candidate
                    # increments are known, every visited guard is checked again for change.
                    value = evaluate(row[1], {}, limit + 1)[0]
                    guards.append(row[1])
                    select(row[2] if value else row[3])

        for rows in self.blocks:
            select(rows)
        # First propose a fixed increment for each selected register. Recognize
        # r = r + amount, r = r - amount, amount + r, or a write that holds the
        # current value. These are candidates; their dependencies are proved next.
        deltas = {}
        for slot, (expr, width) in writes.items():
            if expr.op in {"+", "-"} and expr.args[0].op == "signal" and expr.args[0].args[0] == slot:
                # Use the amount at the current state. It may be a driven expression;
                # the second pass must still prove it stays suitable for this increment.
                delta = evaluate(expr.args[1], {}, limit + 1)[0]
                deltas[slot] = delta if expr.op == "+" else -delta
            elif expr.op == "+" and expr.args[1].op == "signal" and expr.args[1].args[0] == slot:
                deltas[slot] = evaluate(expr.args[0], {}, limit + 1)[0]
            # A masked RHS equal to the old value is a possible hold. A later check
            # rejects it if changing dependencies would give it a nonzero slope.
            elif evaluate(expr, {}, limit + 1)[0] & ((1 << width) - 1) == values[slot]:
                deltas[slot] = 0
            # A different kind of update, such as assigning a new constant, is left
            # to the normal interpreter instead of inventing an increment for it.
            else:
                return None

        # Now evaluate all selected writes with all proposed deltas together.
        # This detects cross-register dependencies that the initial snapshots
        # could not prove. One extra lookahead point is allowed (limit+1), while
        # the returned cycle count remains capped at the caller's limit.
        span, memo = limit, {}
        for slot, (expr, width) in writes.items():
            value, slope, bound = evaluate(expr, deltas, limit + 1, memo=memo)
            mask = (1 << width) - 1
            # The RHS must have both the expected rate and the correct first result:
            # RHS(t) == old_register + delta*(t+1), modulo the register width.
            # Checking only the slope could accept a sequence with the wrong offset.
            if slope != deltas[slot] or value & mask != (values[slot] + slope) & mask:
                return None
            # Assignment truncation can wrap before an expression's width does.
            # Bound both the RHS expression and its final assignment width. The RHS
            # already describes the next stored value, so this bound keeps every
            # applied update within the regime just proved.
            span = min(span, bound, _boundary(value & mask, slope, width, limit + 1))
        # Branch selection may change sooner than any register update wraps.
        # A guard built from a comparison already supplies its transition bound;
        # a changing numeric guard needs a separate nonzero-truth check below.
        for guard in guards:
            value, slope, bound = evaluate(guard, deltas, limit + 1, memo=memo)
            # A vector used as a condition is true until it reaches zero.
            if slope:
                _, bound = _comparison("!=", value, slope, bound)
            span = min(span, bound)
        # Protect requested one-bit trace transitions even when they do not
        # affect the clocked logic. Use the low-clock observation phase and a
        # fresh evaluation, since high-clock memo entries describe another phase.
        for name in traces:
            expr = self.traces.get(name)
            if expr is not None:
                _, slope, bound = evaluate(expr, deltas, limit + 1, high=False)
                # A moving one-bit value needs cycle-by-cycle observation. Otherwise
                # the first possible transition limits the skip; the caller samples
                # just before and at that boundary to retain its exact edge position.
                span = min(span, 1 if slope else bound)
        # Return only changing registers, each with its unsigned assignment mask.
        # The caller applies (old + delta*count) & mask and settles driven wires.
        # None or a span of 1 makes the caller execute an ordinary exact step.
        return max(1, span), {slot: (delta, (1 << writes[slot][1]) - 1)
                              for slot, delta in deltas.items() if delta}
