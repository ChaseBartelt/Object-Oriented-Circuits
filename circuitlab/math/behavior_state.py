"""Accepted history for integrators, transport delays and inertial transitions.

Newton evaluation is read-only. A successful electrical solve proposes every
state update from the same previous snapshot, validates them all, then commits
one new snapshot. Retries and rejected candidates cannot consume time twice.
"""

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass
import math

from circuitlab.model.graph import CircuitError


MAX_DELAY_SAMPLES = 200_000
_KEEP = object()
_DYNAMIC = {"v", "bit", "time", "state"}


@dataclass(frozen=True)
class _Memory:
    operation: str
    value: float
    time: float
    # Transport samples include one predecessor of the retained interval.
    samples: tuple[tuple[float, float], ...] = ()
    initial_time: float = 0.0
    pending: tuple[int, float] | None = None


def _children(tree):
    return (child for child in tree[1:] if isinstance(child, tuple) and child
            and isinstance(child[0], str))


def _constant(tree):
    return tree[0] not in _DYNAMIC and all(_constant(child) for child in _children(tree))


class BehaviorState:
    """State context shared by all hidden B equations in one numerical circuit."""

    def __init__(self, step=None, equations=()):
        self.step = step
        self.time = 0.0
        self.nodes = {}
        self.origins = {}
        self.memory = {}
        for equation in equations:
            tree = getattr(equation, "tree", equation)
            origin = getattr(equation, "line", None), getattr(equation, "path", None)
            self._register(tree, origin)

    def _error(self, key, message, code="MathState"):
        line, path = self.origins.get(key, (None, None))
        raise CircuitError(message, line, code, path=path)

    def _register(self, tree, origin):
        if tree[0] == "state":
            if len(tree) < 4 or tree[1] not in {"integrate", "delay", "transition"}:
                raise CircuitError("Unsupported stateful mathematical operation.", origin[0], "MathState", path=origin[1])
            operation, key, *arguments = tree[1:]
            expected = {"integrate": {1, 2}, "delay": {2}, "transition": {3}}[operation]
            if len(arguments) not in expected:
                raise CircuitError(f"Invalid {operation} argument count.", origin[0], "MathState", path=origin[1])
            if key in self.nodes and self.nodes[key] != tree:
                self._error(key, "Stateful equation keys must identify one operation and expression.")
            self.nodes[key], self.origins[key] = tree, origin
            # History dimensions must not change as Newton changes node guesses.
            for argument in arguments[1:]:
                if not _constant(argument):
                    self._error(key, "Stateful initial values and delays must be constant model expressions.")
        for child in _children(tree):
            self._register(child, origin)

    def clone(self, *, step=_KEEP, equations=None):
        """Copy accepted history without sharing mutable buffers with a retry."""
        updated = BehaviorState(self.step if step is _KEEP else step,
                                self.nodes.values() if equations is None else equations)
        if equations is None:
            updated.origins = dict(self.origins)
        updated.time = self.time
        for key, node in updated.nodes.items():
            if key not in self.memory or key not in self.nodes or node[1] != self.nodes[key][1]:
                continue
            if node[1] == "delay":
                # Equivalent static spellings must not discard a valid buffer.
                # A genuinely longer/shorter delay changes which history was
                # retained, so it still needs an explicit reset.
                before = self._run(self.nodes[key][4], {}, self.time)[0]
                after = updated._run(node[4], {}, self.time)[0]
                tolerance = 8 * max(math.ulp(before), math.ulp(after))
                if not math.isclose(before, after, rel_tol=1e-12, abs_tol=tolerance):
                    updated._error(key, "The transport delay changed. Reset the simulation to rebuild its history.", "TopologyChanged")
            updated.memory[key] = deepcopy(self.memory[key])
        return updated

    def _run(self, tree, voltages, time):
        # Local import keeps the evaluator and its state context independent at
        # module initialization while sharing exactly the same numeric grammar.
        from circuitlab.math.behavioral import evaluate
        return evaluate(tree, voltages, time, state=self)

    def _arguments(self, node, voltages, time):
        key = node[2]
        values = [self._run(argument, voltages, time) for argument in node[3:]]
        if not all(math.isfinite(value) and all(math.isfinite(v) for v in gradient.values())
                   for value, gradient in values):
            self._error(key, "Stateful math produced a non-finite value.", "MathDomain")
        for value, gradient in values[1:]:
            if gradient:
                self._error(key, "Stateful initial values and delays must be constant model expressions.")
        if node[1] in {"delay", "transition"} and any(value < 0 for value, _ in values[1:]):
            self._error(key, "Delay times must be finite and nonnegative.")
        return values

    def _proposal(self, node, voltages, time, *, retain=False):
        operation, key = node[1:3]
        arguments = self._arguments(node, voltages, time)
        signal, gradient = arguments[0]
        record = self.memory.get(key)
        if time < self.time - 8 * math.ulp(max(abs(time), abs(self.time), 1e-300)):
            self._error(key, "Stateful math cannot advance backwards in time.")

        if operation == "integrate":
            initial = arguments[1][0] if len(arguments) == 2 else 0.0
            previous = initial if record is None else record.value
            interval = max(0.0, time - self.time) if self.step is not None else 0.0
            value = previous + interval * signal
            return value, {net: interval * slope for net, slope in gradient.items()}, _Memory(operation, value, time)

        if operation == "transition":
            level = int(signal >= .5)
            if self.step is None or record is None:
                return level, {}, _Memory(operation, level, time)
            pending = record.pending
            current = int(record.value)
            if level == current:
                pending = None
            else:
                latency = arguments[1 if level else 2][0]
                if pending is None or pending[0] != level:
                    pending = level, time + latency
                tolerance = 8 * math.ulp(max(abs(time), abs(pending[1]), 1e-300))
                if time + tolerance >= pending[1]:
                    current, pending = level, None
            return current, {}, _Memory(operation, current, time, pending=pending)

        duration = arguments[1][0]
        if self.step is None or record is None:
            points = ((self.time, signal),) if time == self.time else ((self.time, signal), (time, signal))
            return signal, gradient, _Memory(operation, signal, time, points, self.time)
        target = time - duration
        points = record.samples
        first_time, first_value = points[0]
        if target < first_time and first_time > record.initial_time:
            self._error(key, "The requested delay exceeds its retained history. Reset the simulation.")
        if duration == 0:
            value, slopes = signal, gradient
        elif target <= first_time:
            value, slopes = first_value, {}
        elif target >= points[-1][0]:
            old_time, old_value = points[-1]
            fraction = min(1.0, max(0.0, (target - old_time) / (time - old_time))) if time > old_time else 0.0
            value = old_value + fraction * (signal - old_value)
            slopes = {net: fraction * slope for net, slope in gradient.items()}
        else:
            index = bisect_right(points, (target, math.inf))
            left_time, left_value = points[index - 1]
            right_time, right_value = points[index]
            fraction = (target - left_time) / (right_time - left_time)
            value, slopes = left_value + fraction * (right_value - left_value), {}
        if not retain:
            return value, slopes, None
        samples = [*points]
        if time == samples[-1][0]:
            samples[-1] = time, signal
        else:
            samples.append((time, signal))
        # Constant spans need only their first/last samples for interpolation.
        if len(samples) >= 3 and samples[-3][1] == samples[-2][1] == samples[-1][1]:
            samples.pop(-2)
        start = 0
        while len(samples) - start > 2 and samples[start + 1][0] < target:
            start += 1
        samples = tuple(samples[start:])
        if len(samples) > MAX_DELAY_SAMPLES:
            self._error(key, "Transport delay history is too large. Use a larger time step or a shorter delay.")
        return value, slopes, _Memory(operation, value, time, samples, record.initial_time)

    def evaluate(self, node, voltages, time):
        """Read a trial result without changing accepted values or pending events."""
        try:
            value, gradient, _ = self._proposal(node, voltages, time)
            if not math.isfinite(value) or not all(math.isfinite(v) for v in gradient.values()):
                raise ValueError("non-finite stateful result")
            return value, gradient
        except (TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
            self._error(node[2], f"Invalid stateful mathematical behavior: {error}.", "MathDomain")

    def commit(self, voltages, time):
        """Commit all operations atomically after the electrical endpoint is valid."""
        proposed = {}
        for key, node in self.nodes.items():
            try:
                value, gradient, record = self._proposal(node, voltages, time, retain=True)
                if not math.isfinite(value) or not all(math.isfinite(v) for v in gradient.values()):
                    raise ValueError("non-finite stateful result")
                proposed[key] = record
            except (TypeError, ValueError, OverflowError, ZeroDivisionError) as error:
                self._error(key, f"Invalid stateful mathematical behavior: {error}.", "MathDomain")
        self.memory, self.time = proposed, time
