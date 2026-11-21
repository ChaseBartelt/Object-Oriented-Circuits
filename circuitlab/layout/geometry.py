"""Drawing data, terminal geometry and small placement primitives."""
# Coordinates use schematic units with positive Y upward; the viewer later maps them to pixels.
# Terminal geometry is centralized here so routing, rotation, persistence, and export agree
# on the physical contact positions for the same component.

from collections import defaultdict, deque
from dataclasses import dataclass, field
import math

from circuitlab.model.graph import Component

BJT_PIN_OFFSET = 0.85
POT_PIN_OFFSET = 0.85
PACKAGE_WIDTH = 6.8
PACKAGE_WIRE_CLEARANCE = 0.25


@dataclass(frozen=True)
class ChipPackage(Component):
    """Schematic-only component with numbered pins followed by implicit ports."""

    physical_pin_count: int = 0


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


def opamp_body(a, b):
    """Keep the triangle centered as its input and output leads get shorter."""
    middle = (a[0] + b[0]) / 2
    return middle - .75, middle + .95


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
    count = part.physical_pin_count if isinstance(part, ChipPackage) else len(part.package_pins)
    left_count = max(1, (count + 1) // 2)
    middle = (left_count - 1) / 2
    for index, (label, net) in enumerate(part.package_pins):
        if index < count:
            left = index < left_count
            row = index if left else count - 1 - index
        else:
            # Implicit connections get their own rows below the physical pins.
            extra = index - count
            left = extra % 2 == 0
            row = left_count + extra // 2
        yield label, net, left, (middle - row) * max(1, 2 * grid)


def package_half_height(part, grid=0):
    """Measure the package body using only its physical pins for a chip."""
    if isinstance(part, ChipPackage):
        rows = max(1, (part.physical_pin_count + 1) // 2)
        return max(0.8, rows / 2 + 0.2, (rows - 1) / 2 * max(1, 2 * grid) + 0.2)
    return max(0.8, (len(part.package_pins) + 1) // 2 / 2 + 0.2,
               max((abs(offset) for _, _, _, offset in package_pins(part, grid)), default=0) + 0.2)


def package_body_corners(part, a, b, grid=0):
    """Return the outline corners, excluding the leads to the side pins."""
    half_height = package_half_height(part, grid)
    return (a[0] + .55, a[1] - half_height), (b[0] - .55, a[1] + half_height)


# Use the device-specific contact positions rather than treating every glyph as a simple
# two-terminal line. Routing and saved-layout checks consume this same geometry.
def symbol_pins(part, a, b, top, *, grid=0):
    """Yield each net and its coordinates for one placed symbol."""
    if part.kind == "H":
        for _, net, left, offset in package_pins(part, grid):
            yield net, ((a if left else b)[0], a[1] + offset)
    elif part.kind in {"O", "A"}:
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
        if part.kind == "A":
            gain_offset = pin_offset(1.75, grid)
            yield part.gain1, (a[0] + 1.5, a[1] + gain_offset)
            yield part.gain8, (a[0] + 3.0, a[1] + gain_offset)
            yield part.bypass, (a[0] + 3.0, a[1] - gain_offset)
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


def routed_pins(symbol, turns, grid=0):
    """Return physical contacts after applying the symbol's rotation."""
    _, a, b, _ = symbol
    return tuple((net, rotate_point(pin, a, b, turns)) for net, pin in symbol_pins(*symbol, grid=grid))


def symbol_bounds(symbol, turns, *, body_only=False):
    """Return opposite corners of the rotated symbol or its body."""
    part, a, b, _ = symbol
    if part.kind == "O" and body_only:
        left, right = opamp_body(a, b)
        return rotate_point((left, a[1] - .72), a, b, turns), rotate_point((right, a[1] + .72), a, b, turns)
    height = (
        package_half_height(part) if part.kind == "H"
        else 2.0 if part.kind == "A" else 1.4 if part.kind == "O" and part.vplus
        else 0.9 if part.kind in {"Q", "O", "S", "P"}
        else 0.6 if part.kind == "K" else 0.45
    )
    if body_only and (axis := wire_axis((a, b))) is not None:
        half_body = {"C": .18, "V": .48, "I": .48}.get(part.kind, .75)
        inset = {"H": .55, "A": .8, "Q": .8, "S": .8, "P": .8}.get(
            part.kind, max(0, abs(a[axis] - b[axis]) / 2 - half_body)
        )
        x_pad, y_pad = (-inset, height) if axis == 0 else (0.6 if part.kind == "K" else 0.45, -inset)
    else:
        x_pad, y_pad = 0.45, height
    lo = min(a[0], b[0]) - x_pad, min(a[1], b[1]) - y_pad
    hi = max(a[0], b[0]) + x_pad, max(a[1], b[1]) + y_pad
    return rotate_point(lo, a, b, turns), rotate_point(hi, a, b, turns)


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
            yield from routed_pins(symbol, self.rotations.get(symbol[0].name, 0), self.pin_grid)

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
        if e.kind not in {"Q", "H", "S", "P", "A"}
    ]
    pins += [
        (i, e.in_plus, True, -0.35 if i in flipped else 0.35)
        if e.kind == "O"
        else (i, e.n, i in reversed_parts, 0.0)
        for i, e in enumerate(parts)
        if e.kind not in {"Q", "H", "S", "P", "A"}
    ]
    pins += [
        (i, e.in_minus, True, 0.35 if i in flipped else -0.35)
        for i, e in enumerate(parts)
        if e.kind == "O"
    ]
    for i, part in enumerate(parts):
        if part.kind == "A":
            sign = -1 if i in flipped else 1
            pins.extend(((i, part.in_plus, True, sign * .35),
                         (i, part.in_minus, True, -sign * .35), (i, part.p, False, 0),
                         (i, part.vplus, True, 1.25), (i, part.vminus, True, -1.25),
                         (i, part.gain1, True, 1.75), (i, part.gain8, False, 1.75),
                         (i, part.bypass, False, -1.75)))
        elif part.kind == "O" and part.vplus:
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
    if part.kind in {"O", "A"} and part.vplus and abs(offset) == 1.25:
        return (a[0] + b[0]) / 2, a[1] + offset
    if part.kind == "A" and abs(offset) == 1.75:
        return a[0] + (1.5 if left else 3.0), a[1] + offset
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


def ground_lead(layout, terminal, name="0", *, turns=0, length=.5):
    """Draw a straight ground lead and point its symbol away from the terminal."""
    x, y = terminal
    end = rotate_point((x, y - length), terminal, terminal, turns)
    if end != terminal:
        add_wire(layout, name, terminal, end)
    ground_at(layout, end, name)
    if turns:
        layout.ground_rotations[end] = turns


def place_symbol(layout, part, first_net, start, end):
    a, b = (start, end) if part.p == first_net else (end, start)
    layout.symbols.append((part, a, b, "+"))
    layout.positions[part.name] = a


def inline_grounded_voltage(layout, source, net, ground, terminal, *, left, lead=.25):
    """Attach one isolated voltage source and its return to a side pin."""
    direction = -1 if left else 1
    x, y = terminal
    near = (x + direction * lead, y)
    far = (near[0] + direction * 2.5, y)
    place_symbol(layout, source, net, near, far)
    add_wire(layout, net, terminal, near)
    ground_lead(layout, far, ground, turns=1 if left else 3, length=.75)


# Drawing orientation follows which terminal is grounded, not the sign of the source value.
# A negative supply can therefore retain a readable vertical source and local ground.
def source_pin_is_upper(part, positive, grounds):
    """The grounded terminal goes below the active terminal, regardless of polarity."""
    return positive != (part.p in grounds and part.n not in grounds)
