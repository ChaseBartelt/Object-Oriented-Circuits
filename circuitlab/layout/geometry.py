"""Drawing data, terminal geometry and small placement primitives."""
# Coordinates use schematic units with positive Y upward; the viewer later maps them to pixels.
# Terminal geometry is centralized here so routing, rotation, persistence, and export agree
# on the physical contact positions for the same component.

from collections import defaultdict, deque
from dataclasses import dataclass, field
import math

BJT_PIN_OFFSET = 0.85
POT_PIN_OFFSET = 0.85
PACKAGE_WIDTH = 6.8


def wire_axis(edge):
    """Return the coordinate parallel to the segment, or None for a diagonal."""
    a, b = edge
    if a[1] == b[1]:
        return 0
    if a[0] == b[0]:
        return 1
    return None


# Rotate about the symbol center in quarter turns, retaining exact reusable coordinates.
def rotate_point(point, a, b, turns):
    """Rotate clockwise around the center of a symbol, in quarter turns."""
    if not turns:
        return point
    x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    dx, dy = point[0] - x, point[1] - y
    dx, dy = ((dx, dy), (dy, -dx), (-dx, -dy), (-dy, dx))[turns % 4]
    return round(x + dx, 8), round(y + dy, 8)


def pin_offset(value, grid=0):
    """Retain legacy spacing unless a layout explicitly uses grid-sized pins."""
    if not grid or not value:
        return value
    return math.copysign(max(1, math.floor(abs(value) / grid + 0.5)) * grid, value)


# The physical package order runs down one side and back up the other, so header order
# continues to match package pin numbering.
def package_pins(part, grid=0):
    """Put the first half down the left side and the rest up the right side."""
    if part.base:
        for (label, net), (left, offset) in zip(
            part.package_pins, ((True, 0), (False, BJT_PIN_OFFSET), (False, -BJT_PIN_OFFSET))
        ):
            yield label, net, left, pin_offset(offset, grid)
        return
    left_count = (len(part.package_pins) + 1) // 2
    middle = (left_count - 1) / 2
    for index, (label, net) in enumerate(part.package_pins):
        left = index < left_count
        row = index if left else len(part.package_pins) - 1 - index
        yield label, net, left, (middle - row) * max(1, 2 * grid)


# Use the device-specific contact positions rather than treating every glyph as a simple
# two-terminal line. Routing and saved-layout checks consume this same geometry.
def symbol_pins(part, a, b, top, *, grid=0):
    """Yield each net and its coordinates for one placed symbol."""
    if part.kind == "H":
        for _, net, left, offset in package_pins(part, grid):
            yield net, ((a if left else b)[0], a[1] + offset)
    elif part.kind == "O":
        offset = pin_offset(0.35, grid)
        plus, minus = (offset, -offset) if top == "+" else (-offset, offset)
        yield part.in_plus, (a[0], a[1] + plus)
        yield part.in_minus, (a[0], a[1] + minus)
        yield part.p, b
        if part.vplus:
            power_offset = pin_offset(1.25, grid)
            center = (a[0] + b[0]) / 2
            yield part.vplus, (center, a[1] + power_offset)
            yield part.vminus, (center, a[1] - power_offset)
    elif part.kind == "Q":
        offset = pin_offset(BJT_PIN_OFFSET, grid)
        yield part.p, (b[0], b[1] + offset)
        yield part.n, (b[0], b[1] - offset)
        yield part.base, a
    elif part.kind == "S":
        offset = pin_offset(0.6, grid)
        yield part.in_plus, (a[0], a[1] + offset)
        yield part.in_minus, (a[0], a[1] - offset)
        yield part.p, (b[0], b[1] + offset)
        yield part.n, (b[0], b[1] - offset)
    elif part.kind == "P":
        offset = pin_offset(POT_PIN_OFFSET, grid)
        yield part.p, (a[0], a[1] + offset)
        yield part.wiper, b
        yield part.n, (a[0], a[1] - offset)
    else:
        yield part.p, a
        yield part.n, b


@dataclass
class Layout:
    """Drawing coordinates with positive y up, keyed by electrical net."""

    positions: dict = field(default_factory=dict)
    symbols: list = field(default_factory=list)
    wires: dict = field(default_factory=dict)
    junctions: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    grounds: list = field(default_factory=list)
    ground_names: dict = field(default_factory=dict)
    connectors: list = field(default_factory=list)
    crossings: dict = field(default_factory=dict)
    boundary: dict = field(default_factory=dict)
    rotations: dict = field(default_factory=dict)
    ground_rotations: dict = field(default_factory=dict)
    label_offsets: dict = field(default_factory=dict)
    pin_grid: float = 0

    @property
    def uses_packages(self):
        return any(part.kind == "H" for part, *_ in self.symbols)

    def pins(self):
        """Yield the physical pin coordinates and the net attached to each."""
        for symbol in self.symbols:
            part, a, b, _ = symbol
            turns = self.rotations.get(part.name, 0)
            for net, pin in symbol_pins(*symbol, grid=self.pin_grid):
                yield net, rotate_point(pin, a, b, turns)

    def visible_segments(self, net):
        """Cut visual gaps at crossings while retaining the original net ID."""
        for a, b in self.wires[net]:
            cuts = self.crossings.get((net, a, b))
            if not cuts:
                yield a, b
                continue
            if wire_axis((a, b)) is None:
                length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
                start = 0
                for cut in sorted(cuts):
                    low, high = max(0, cut - 0.12 / length), min(1, cut + 0.12 / length)
                    if low > start:
                        yield along(a, b, start), along(a, b, low)
                    start = max(start, high)
                if start < 1:
                    yield along(a, b, start), b
                continue
            x = a[0]
            start, end = sorted((a[1], b[1]))
            for crossing in sorted(cuts):
                low, high = crossing - 0.12, crossing + 0.12
                if low > start:
                    yield (x, start), (x, low)
                start = max(start, high)
            if end > start:
                yield (x, start), (x, end)


def along(a, b, fraction):
    return tuple(x + (y - x) * fraction for x, y in zip(a, b))


def component_pins(parts, flipped=(), reversed_parts=()):
    """Describe each grid pin as (component index, net, left side, y offset)."""
    pins = [
        (i, e.p, e.kind != "O" and i not in reversed_parts, 0.0)
        for i, e in enumerate(parts)
        if e.kind not in {"Q", "H", "S", "P"}
    ]
    pins += [
        (i, e.in_plus, True, -0.35 if i in flipped else 0.35)
        if e.kind == "O"
        else (i, e.n, i in reversed_parts, 0.0)
        for i, e in enumerate(parts)
        if e.kind not in {"Q", "H", "S", "P"}
    ]
    pins += [
        (i, e.in_minus, True, 0.35 if i in flipped else -0.35)
        for i, e in enumerate(parts)
        if e.kind == "O"
    ]
    for i, part in enumerate(parts):
        if part.kind == "O" and part.vplus:
            # Side hints choose routing gutters; the physical power contacts
            # remain centered above and below the triangle.
            pins.extend(((i, part.vplus, True, 1.25), (i, part.vminus, True, -1.25)))
    for i, part in enumerate(parts):
        if part.kind == "H":
            pins.extend((i, net, left, offset) for _, net, left, offset in package_pins(part))
        elif part.kind in {"Q", "S", "P"}:
            a, b = (0.0, 0.0), (4.3, 0.0)
            pins.extend(
                (i, net, x == a[0], y) for net, (x, y) in symbol_pins(part, a, b, "+")
            )
    return pins


# Routing side hints are not always the physical contact: powered op-amp supply pins
# sit at the triangle center above and below the signal path.
def grid_terminal(part, a, b, left, offset):
    """Resolve a routing pin hint to its physical contact on a grid symbol."""
    if part.kind == "O" and part.vplus and abs(offset) == 1.25:
        return (a[0] + b[0]) / 2, a[1] + offset
    return min(a[0], b[0]) if left else max(a[0], b[0]), a[1] + offset


def placement_order(parts, pins, grounds=("0",), roots=None):
    """Visit nearby components without letting ground dominate placement."""
    nets = defaultdict(set)
    for i, net, *_ in pins:
        nets[net].add(i)
    adjacent = [set() for _ in parts]
    for net, owners in nets.items():
        if net not in grounds and len(owners) <= 6:
            for i in owners:
                adjacent[i].update(owners)
    sources = (
        [i for i, e in enumerate(parts) if e.kind in "VI"] if roots is None else roots
    )
    pending = set(range(len(parts))).difference(sources)
    queue = deque(sources)
    order = []
    while queue or pending:
        if not queue:
            i = min(pending)
            pending.remove(i)
            queue.append(i)
        i = queue.popleft()
        order.append(i)
        neighbors = sorted(adjacent[i] & pending)
        pending.difference_update(neighbors)
        queue.extend(neighbors)
    return order


def input_source_indices(parts):
    first_stage = min(
        (part.order for part in parts if not part.is_source and part.order), default=0
    )
    return [
        i
        for i, part in enumerate(parts)
        if part.is_source and (not first_stage or part.order <= first_stage)
    ]


def grid_symbol(part, x, y, grounds, *, top="+", reverse=False):
    """Place a symbol in a grid cell, keeping grounded sources vertical."""
    if part.is_source:
        start, end = (x + 2.15, y), (x + 2.15, y - 2.5)
        reverse = not source_pin_is_upper(part, True, grounds)
    else:
        width = PACKAGE_WIDTH if part.kind == "H" else 4.3
        start, end = (x, y), (x + width, y)
    if reverse:
        start, end = end, start
    return part, start, end, top


def add_wire(layout, net, *points):
    layout.wires.setdefault(net, []).extend(zip(points, points[1:]))


def label_node(layout, circuit, point, node):
    if not circuit.grouped or node in circuit.named_wires or node in ("0", "in"):
        layout.junctions.setdefault(node, []).append(point)
        layout.labels[node] = point


def ground_at(layout, point, name="0"):
    layout.grounds.append(point)
    if name != "0":
        layout.ground_names[point] = name
    layout.junctions.setdefault(name, []).append(point)


def place_symbol(layout, part, first_net, start, end):
    a, b = (start, end) if part.p == first_net else (end, start)
    layout.symbols.append((part, a, b, "+"))
    layout.positions[part.name] = a


# Drawing orientation follows which terminal is grounded, not the sign of the source value.
# A negative supply can therefore retain a readable vertical source and local ground.
def source_pin_is_upper(part, positive, grounds):
    """The grounded terminal goes below the active terminal, regardless of polarity."""
    return positive != (part.p in grounds and part.n not in grounds)
