"""Verify that schematic geometry preserves the compiled electrical nets."""
# Electrical net identity is the acceptance rule for geometry changes.
# Same-net contacts become junctions; crossings between distinct nets need visual gaps and
# must never become accidental electrical connections.

from collections import Counter, defaultdict
from itertools import chain, combinations

from circuitlab.layout.geometry import (
    PACKAGE_WIRE_CLEARANCE,
    along,
    package_body_corners,
    rotate_point,
    symbol_bounds,
    wire_axis,
)


def intersection(first, second):
    """Return intersection parameters for two nonparallel line segments."""
    a, b = first
    c, d = second
    x, y = b[0] - a[0], b[1] - a[1]
    u, v = d[0] - c[0], d[1] - c[1]
    determinant = x * v - y * u
    if abs(determinant) < 1e-10:
        return None
    dx, dy = c[0] - a[0], c[1] - a[1]
    return (dx * v - dy * u) / determinant, (dx * y - dy * x) / determinant


def segments_intersect(first, second):
    """Check whether two segments or points touch."""
    a, b = first
    c, d = second
    overlaps = all(
        max(min(a[i], b[i]), min(c[i], d[i]))
        <= min(max(a[i], b[i]), max(c[i], d[i])) + 1e-8
        for i in (0, 1)
    )
    if not overlaps or (wire_axis(first) is not None and wire_axis(second) is not None):
        return overlaps
    crossing = intersection(first, second)
    if crossing is not None:
        return all(-1e-8 <= pos <= 1 + 1e-8 for pos in crossing)
    if a == b:
        a, b, c = c, d, a
    return abs((c[0] - a[0]) * (b[1] - a[1]) - (c[1] - a[1]) * (b[0] - a[0])) < 1e-8


def segment_intersects_box(edge, a, b):
    """Check a straight segment against an axis-aligned rectangle and its border."""
    left, right = sorted((a[0], b[0]))
    bottom, top = sorted((a[1], b[1]))
    (x1, y1), (x2, y2) = edge
    if (max(x1, x2) < left - 1e-8 or min(x1, x2) > right + 1e-8
            or max(y1, y2) < bottom - 1e-8 or min(y1, y2) > top + 1e-8):
        return False
    if wire_axis(edge) is not None:
        return True
    borders = (((left, bottom), (right, bottom)), ((right, bottom), (right, top)),
               ((right, top), (left, top)), ((left, top), (left, bottom)))
    return (any(left < x < right and bottom < y < top for x, y in edge)
            or any(segments_intersect(edge, border) for border in borders))


def clear_package_wires(drawing, edges=None):
    """Require a grid square around package bodies while leaving pin leads open."""
    bounds = []
    # A lane exactly one square away can lie on the keepout border.
    padding = PACKAGE_WIRE_CLEARANCE - 1e-7
    for part, a, b, _ in drawing.symbols:
        if part.kind != "H":
            continue
        (left, bottom), (right, top) = package_body_corners(part, a, b, drawing.pin_grid)
        turns = drawing.rotations.get(part.name, 0)
        bounds.append((rotate_point((left - padding, bottom - padding), a, b, turns),
                       rotate_point((right + padding, top + padding), a, b, turns)))
    if not bounds:
        return True
    if edges is None:
        edges = chain.from_iterable(drawing.wires.values())
    return not any(segment_intersects_box(edge, a, b) for edge in edges for a, b in bounds)


def clear_of_bodies(edges, layout):
    """Keep segments outside symbol bodies and the clearance around packages."""
    if not clear_package_wires(layout, edges):
        return False
    for symbol in layout.symbols:
        part = symbol[0]
        if part.kind == "H":
            continue
        a, b = symbol_bounds(symbol, layout.rotations.get(part.name, 0), body_only=True)
        if any(segment_intersects_box(edge, a, b) for edge in edges):
            return False
    return True


# Compare physical wire islands and terminal attachments against the compiled net sets.
# Both missing connections and unintended cross-net contacts invalidate the drawing.
def preserves_nets(circuit, drawing):
    """Check for missing connections and unintended contacts between nets."""
    if Counter(part for part, *_ in drawing.symbols) != Counter(circuit.components):
        return False
    pins, links = defaultdict(list), defaultdict(list)
    for net, point in chain(drawing.pins(), drawing.boundary.items()):
        pins[net].append((point, point))
    for point in drawing.grounds:
        net = drawing.ground_names.get(point, "0")
        if net not in circuit.grounds:
            return False
        links[net].append((point, point))
    names, nets = {}, {}
    for net, point, name, *_ in drawing.connectors:
        name = name.casefold()
        if names.setdefault(name, net) != net or nets.setdefault(net, name) != name:
            return False
        links[net].append((point, point))
    if (drawing.wires.keys() | links.keys() | drawing.junctions.keys()) - pins.keys():
        return False

    visible = []
    for net, terminals in pins.items():
        wires = drawing.wires.get(net, [])
        junctions = [(p, p) for p in drawing.junctions.get(net, [])]
        # A ground or matching connector can join physically separate islands.
        conductors = [*terminals, *wires]
        pending = conductors + junctions
        endpoints = links[net]
        reached = [*endpoints] if endpoints else [pending.pop()]
        for segment in reached:
            remaining = []
            for other in pending:
                (reached if segments_intersect(segment, other) else remaining).append(other)
            pending = remaining
        if pending or any(
            not any(segments_intersect(end, segment) for segment in conductors)
            for end in endpoints
        ):
            return False

        segments = [*terminals, *endpoints, *junctions]
        if wires:
            segments.extend(drawing.visible_segments(net))
        visible.append(segments)

    return all(
        not segments_intersect(first, second)
        for one_net, other_net in combinations(visible, 2)
        for first in one_net
        for second in other_net
    )


# A crossing inside one net is a junction; a crossing between different nets is a visual
# bridge/gap. The display distinction must not change the underlying wire graph.
def mark_crossings(drawing):
    """Mark junctions for the same net and visual gaps for distinct nets."""
    contacts = []

    def record(net, other, a, b, point, offset):
        if net == other:
            drawing.junctions.setdefault(net, []).append(point)
        else:
            drawing.crossings.setdefault((net, a, b), []).append(offset)
            contacts.append((net, other))

    horizontal, vertical, diagonal = [], [], []
    for net, segments in drawing.wires.items():
        for a, b in segments:
            axis = wire_axis((a, b))
            (diagonal if axis is None else (horizontal, vertical)[axis]).append((net, a, b))
    for net, a, b in vertical:
        x = a[0]
        low, high = sorted((a[1], b[1]))
        for other, c, d in horizontal:
            left, right = sorted((c[0], d[0]))
            y = c[1]
            if left < x < right and low < y < high:
                record(net, other, a, b, (x, y), y)

    seen = horizontal + vertical
    for net, a, b in diagonal:
        for other, c, d in seen:
            crossing = intersection((a, b), (c, d))
            if crossing is None:
                continue
            first, second = crossing
            if 0 < first < 1 and 0 < second < 1:
                record(net, other, a, b, along(a, b, first), first)
        seen.append((net, a, b))

    # Prefer a clear span of the trunk for its label.
    for net, (center, y) in drawing.labels.items():
        if not drawing.wires.get(net) or ("net", net) in drawing.label_offsets:
            continue
        a, b = drawing.wires[net][0]
        left, right = sorted((a[0], b[0]))
        stops = sorted({left, right} | {
            c[0] for _, c, d in vertical
            if left < c[0] < right and c[1] <= y + 0.75 and d[1] >= y
        })
        candidates = [
            (start + end) / 2
            for start, end in zip(stops, stops[1:])
            if end - start >= len(net) * 0.32 + 0.35
        ]
        if candidates:
            drawing.labels[net] = (min(candidates, key=lambda x: abs(x - center)), y)
        elif len(stops) > 2:
            start, end = max(zip(stops, stops[1:]), key=lambda span: span[1] - span[0])
            drawing.labels[net] = ((start + end) / 2, y)

    return contacts
