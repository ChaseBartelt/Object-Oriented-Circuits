"""Write native LTspice symbols, wires, and flags from a schematic layout."""

from collections import defaultdict
from itertools import combinations

from .graph import CircuitError
from .layout_checks import segments_intersect
from .spice import opamp_subcircuit, spice_circuit

# Pin locations relative to the anchors in LTspice's built-in symbol library.
SYMBOLS = {
    "R": ("res", 16, 16, 96),
    "C": ("cap", 16, 0, 64),
    "L": ("ind", 16, 16, 96),
    "V": ("voltage", 0, 16, 96),
    "I": ("current", 0, 0, 80),
}


def export_schematic(circuit, layout):
    """Serialize the rendered circuit with the same electrical nets as SPICE."""
    spice = spice_circuit(circuit)
    for scale in (48, 64, 96, 128):
        try:
            return _schematic(layout, spice, scale), False
        except GridConflict:
            continue
    # Preserve symbol placement if the native pins and the existing tracks
    # cannot coexist on the grid. Flags on the actual pins carry every net.
    try:
        return _schematic(layout, spice, 64, pin_flags=True), True
    except GridConflict:
        raise CircuitError(
            "The rendered symbols are too close for the LTspice grid. "
            "Select Labeled connectors and export again."
        ) from None


class GridConflict(Exception):
    """The drawing needs more grid space to keep connections distinct."""


def _schematic(layout, spice, scale, *, pin_flags=False):
    """Snap the drawing, attach native symbol pins, and serialize the sheet."""

    def snap(value):
        return 16 * round(value * scale / 16)

    def point(position):
        return snap(position[0]), snap(-position[1])

    wires, points = defaultdict(set), defaultdict(set)
    flags = defaultdict(set)
    symbols = []

    def wire(net, a, b):
        if pin_flags:
            return
        points[net].update((a, b))
        if a != b:
            wires[net].add(tuple(sorted((a, b))))

    def pin(net, at):
        points[net].add(at)
        if pin_flags:
            flags[net].add(at)

    if not pin_flags:
        for net in layout.wires:
            for a, b in layout.visible_segments(net):
                wire(net, point(a), point(b))
        for net, p, _, _, _ in layout.connectors:
            flags[net].add(point(p))
        for p in layout.grounds:
            flags[layout.ground_names.get(p, "0")].add(point(p))

    for part, start, end, top in layout.symbols:
        model = spice.parts[part.name]
        a, b = point(start), point(end)
        if model.kind == "X":
            x, y = a
            if b[0] < x + 112:
                raise GridConflict
            origin = (x + 64, y - 64 if top == "-" else y + 64)
            rotation = "R0" if top == "-" else "M180"
            symbol = r"OpAmps\opamp2"
            for net, sign in ((part.in_plus, "+"), (part.in_minus, "-")):
                upper = sign == top
                offset = 0.35 if upper else -0.35
                at = point((start[0], start[1] + offset))
                lane = y - 16 if upper else y + 16
                wire(net, at, (x + 16, at[1]))
                wire(net, (x + 16, at[1]), (x + 16, lane))
                wire(net, (x + 16, lane), (x + 32, lane))
                pin(net, (x + 32, lane))
            wire(part.p, (x + 96, y), b)
            pin(part.p, (x + 96, y))
            for pin_y in (y - 32, y + 32):
                supply = (x + 64, pin_y)
                pin(part.n, supply)
                flags[part.n].add(supply)
            name = model.name
            value = model.value
        else:
            symbol, pin_x, p_y, n_y = SYMBOLS[model.kind]
            dx, dy = b[0] - a[0], b[1] - a[1]
            if (dx and dy) or abs(dx or dy) < n_y - p_y:
                raise GridConflict
            rotation, transform = _orientation(dx, dy)
            first = transform(pin_x, p_y)
            last = transform(pin_x, n_y)
            center = (snap((start[0] + end[0]) / 2), snap(-(start[1] + end[1]) / 2))
            length = n_y - p_y
            unit = ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
            along = (center[0] - a[0]) * unit[0] + (center[1] - a[1]) * unit[1]
            lead = min(max(0, along - 16 * round(length / 32)), abs(dx or dy) - length)
            p = (a[0] + lead * unit[0], a[1] + lead * unit[1])
            origin = p[0] - first[0], p[1] - first[1]
            n = origin[0] + last[0], origin[1] + last[1]
            wire(part.p, a, p)
            wire(part.n, b, n)
            pin(part.p, p)
            pin(part.n, n)
            name = model.name
            value = model.value
        lines = [
            f"SYMBOL {symbol} {origin[0]} {origin[1]} {rotation}",
            f"SYMATTR InstName {name}",
            f"SYMATTR Value {value}",
        ]
        if model.kind == "L":
            lines.append(f"SYMATTR SpiceLine {model.extra.strip()}")
        elif model.kind in "VI" and model.extra:
            lines.append(f"SYMATTR Value2 {model.extra.strip()}")
        symbols.extend(lines)

    # LTspice wires carry no net ID. Reject any contact between distinct
    # nets after snapping, including pins and flags that sit on a wire.
    occupied = [
        (net, segment)
        for net in points
        for segment in (*wires[net], *((p, p) for p in points[net] | flags[net]))
    ]
    if any(
        a != b and segments_intersect(segment, other)
        for (a, segment), (b, other) in combinations(occupied, 2)
    ):
        raise GridConflict

    lines = []
    for net, vertices in points.items():
        edges = set()
        vertices.update(flags[net])
        preferred = point(layout.labels[net]) if net in layout.labels else None
        if preferred is not None and any(
            segments_intersect((preferred, preferred), segment)
            for segment in wires[net]
        ):
            vertices.add(preferred)
        for segment in wires[net]:
            split = sorted(p for p in vertices if segments_intersect((p, p), segment))
            edges.update(zip(split, split[1:]))
        lines.extend(f"WIRE {b[0]} {b[1]} {a[0]} {a[1]}" for a, b in sorted(edges))
        # A drawn crossing gap or labeled connector can split a net into
        # islands. Give each island the same LTspice flag to reconnect it.
        for island in _islands(vertices, edges):
            if not island & flags[net]:
                flags[net].add(preferred if preferred in island else min(island))
        lines.extend(f"FLAG {x} {y} {spice.nodes[net]}" for x, y in sorted(flags[net]))

    all_points = [p for vertices in points.values() for p in vertices]
    left = min(x for x, _ in all_points)
    right = max(x for x, _ in all_points)
    bottom = max(y for _, y in all_points)
    lines.extend(symbols)
    if pin_flags:
        lines.append(f"TEXT {left} {bottom + 64} Left 2 ;Matching net labels connect.")
    commands = [spice.directive]
    commands.extend(f"{name} {node} 0 DC 0" for name, node in spice.references)
    if any(part.kind == "X" for part in spice.parts.values()):
        # opamp2's pin order is plus, minus, V+, V-, out. The unused V+
        # pin and the return pin both connect to the circuit's return net.
        commands.extend(opamp_subcircuit("plus minus unused return out"))
    lines.append(f"TEXT {left} {bottom + 96} Left 2 !" + r"\n".join(commands))
    return "\n".join(
        [
            "Version 4",
            f"SHEET 1 {max(880, right + 160)} {max(680, bottom + 240)}",
            *lines,
            "",
        ]
    )


def _orientation(dx, dy):
    if dx > 0:
        return "R270", lambda x, y: (y, -x)
    if dx < 0:
        return "R90", lambda x, y: (-y, x)
    if dy < 0:
        return "R180", lambda x, y: (-x, -y)
    return "R0", lambda x, y: (x, y)


def _islands(vertices, edges):
    """Find the groups of points connected by physical wire segments."""
    neighbors = defaultdict(set)
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    pending = set(vertices)
    for root in sorted(vertices):
        if root not in pending:
            continue
        pending.remove(root)
        stack = [root]
        for point in stack:
            next_points = neighbors[point] & pending
            pending.difference_update(next_points)
            stack.extend(next_points)
        yield set(stack)
