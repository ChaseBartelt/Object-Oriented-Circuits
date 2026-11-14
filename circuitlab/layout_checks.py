"""Verify that schematic geometry preserves the compiled electrical nets."""

from collections import Counter, defaultdict
from itertools import combinations


def segments_intersect(first, second):
    """Check whether two orthogonal segments or points touch."""
    a, b = first
    c, d = second
    return (
        max(min(a[0], b[0]), min(c[0], d[0]))
        <= min(max(a[0], b[0]), max(c[0], d[0])) + 1e-8
        and max(min(a[1], b[1]), min(c[1], d[1]))
        <= min(max(a[1], b[1]), max(c[1], d[1])) + 1e-8
    )


def preserves_nets(circuit, drawing):
    """Check for missing connections and unintended contacts between nets."""
    if Counter(part for part, *_ in drawing.symbols) != Counter(circuit.components):
        return False
    pins, links = defaultdict(list), defaultdict(list)
    for net, point in drawing.pins():
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
        if any(a[0] != b[0] and a[1] != b[1] for a, b in wires):
            return False
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
