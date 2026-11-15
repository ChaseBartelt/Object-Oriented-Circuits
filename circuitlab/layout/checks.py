"""Verify that schematic geometry preserves the compiled electrical nets."""

from collections import Counter, defaultdict
from itertools import combinations

from circuitlab.layout.geometry import along, wire_axis


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
    overlaps = (
        max(min(a[0], b[0]), min(c[0], d[0]))
        <= min(max(a[0], b[0]), max(c[0], d[0])) + 1e-8
        and max(min(a[1], b[1]), min(c[1], d[1]))
        <= min(max(a[1], b[1]), max(c[1], d[1])) + 1e-8
    )
    if not overlaps or (wire_axis(first) is not None and wire_axis(second) is not None):
        return overlaps
    crossing = intersection(first, second)
    if crossing is not None:
        return all(-1e-8 <= pos <= 1 + 1e-8 for pos in crossing)
    if a == b:
        a, b, c = c, d, a
    return abs((c[0] - a[0]) * (b[1] - a[1]) - (c[1] - a[1]) * (b[0] - a[0])) < 1e-8


def preserves_nets(circuit, drawing):
    """Check for missing connections and unintended contacts between nets."""
    if Counter(part for part, *_ in drawing.symbols) != Counter(circuit.components):
        return False
    pins, links = defaultdict(list), defaultdict(list)
    for net, point in drawing.pins():
        pins[net].append((point, point))
    for net, point in drawing.boundary.items():
        pins[net].append((point, point))
    for point in drawing.grounds:
        net = drawing.ground_names.get(point, "0")
        if net not in circuit.grounds:
            return False
        links[net].append((point, point))
    names, nets = {}, {}
    for net, point, name, *_ in drawing.connectors:
        if (
            names.setdefault(name.casefold(), net) != net
            or nets.setdefault(net, name.casefold()) != name.casefold()
        ):
            return False
        links[net].append((point, point))
    if (drawing.wires.keys() | links.keys() | drawing.junctions.keys()) - pins.keys():
        return False

    visible = []
    for net, terminals in pins.items():
        wires = drawing.wires.get(net, [])
        junctions = [(p, p) for p in drawing.junctions.get(net, [])]
        # A ground or matching connector can join physically separate islands.
        pending = [*terminals, *wires, *junctions]
        endpoints = links[net]
        reached = [*endpoints] if endpoints else [pending.pop()]
        for segment in reached:
            touching = [
                other for other in pending if segments_intersect(segment, other)
            ]
            if touching:
                pending = [other for other in pending if other not in touching]
                reached.extend(touching)
        if pending or any(
            not any(
                segments_intersect(end, segment) for segment in (*terminals, *wires)
            )
            for end in endpoints
        ):
            return False

        visible.extend(
            (net, segment) for segment in (*terminals, *endpoints, *junctions)
        )
        if wires:
            visible.extend((net, segment) for segment in drawing.visible_segments(net))

    return all(
        net == other or not segments_intersect(first, second)
        for (net, first), (other, second) in combinations(visible, 2)
    )


def mark_crossings(drawing):
    """Mark junctions for the same net and visual gaps for distinct nets."""
    horizontal, vertical, diagonal = [], [], []
    for net, segments in drawing.wires.items():
        for a, b in segments:
            axis = wire_axis((a, b))
            (diagonal if axis is None else (horizontal, vertical)[axis]).append((net, a, b))
    contacts = []
    for net, a, b in vertical:
        x = a[0]
        low, high = sorted((a[1], b[1]))
        for other, c, d in horizontal:
            left, right = sorted((c[0], d[0]))
            y = c[1]
            if left < x < right and low < y < high:
                if net == other:
                    drawing.junctions.setdefault(net, []).append((x, y))
                else:
                    drawing.crossings.setdefault((net, a, b), []).append(y)
                    contacts.append((net, other))

    straight = horizontal + vertical
    for index, (net, a, b) in enumerate(diagonal):
        for other, c, d in straight + diagonal[:index]:
            crossing = intersection((a, b), (c, d))
            if crossing is None:
                continue
            first, second = crossing
            if 0 < first < 1 and 0 < second < 1:
                if net == other:
                    drawing.junctions.setdefault(net, []).append(along(a, b, first))
                else:
                    drawing.crossings.setdefault((net, a, b), []).append(first)
                    contacts.append((net, other))

    # Prefer a clear span of the trunk for its label.
    for net, (center, y) in drawing.labels.items():
        if not drawing.wires.get(net) or ("net", net) in drawing.label_offsets:
            continue
        a, b = drawing.wires[net][0]
        left, right = sorted((a[0], b[0]))
        stops = sorted(
            {
                left,
                right,
                *(
                    c[0]
                    for _, c, d in vertical
                    if left < c[0] < right and c[1] <= y + 0.75 and d[1] >= y
                ),
            }
        )
        candidates = [
            (start + end) / 2
            for start, end in zip(stops, stops[1:])
            if end - start >= len(net) * 0.32 + 0.35
        ]
        if candidates:
            drawing.labels[net] = (min(candidates, key=lambda x: abs(x - center)), y)
        elif len(stops) > 2:
            start, end = max(
                zip(stops, stops[1:]), key=lambda interval: interval[1] - interval[0]
            )
            drawing.labels[net] = ((start + end) / 2, y)

    return contacts
