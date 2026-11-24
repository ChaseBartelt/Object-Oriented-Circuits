"""Verify that schematic geometry preserves the compiled electrical nets."""
# Electrical net identity is the acceptance rule for geometry changes.
# Same-net contacts become junctions; crossings between distinct nets need visual gaps and
# must never become accidental electrical connections.

from collections import Counter, defaultdict
from dataclasses import replace
from itertools import chain

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


def clear_package_wires(drawing, edges=None, *, underside=None):
    """Require a grid square around package bodies while leaving pin leads open."""
    if underside is True:
        return True
    if drawing.underside_wires and underside is None:
        from circuitlab.layout.board_layers import _partition, layer_wires
        if edges is None:
            edges = list(chain.from_iterable(layer_wires(drawing).values()))
        else:
            edges, _ = _partition(list(edges), list(chain.from_iterable(drawing.underside_wires.values())))
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


def clear_of_bodies(edges, layout, *, underside=None):
    """Keep segments outside symbol bodies and the clearance around packages."""
    if underside is True:
        return True
    edges = list(edges)
    if layout.underside_wires and underside is None:
        from circuitlab.layout.board_layers import _partition
        edges, _ = _partition(edges, list(chain.from_iterable(layout.underside_wires.values())))
    if not clear_package_wires(layout, edges, underside=False):
        return False
    for symbol in layout.symbols:
        part = symbol[0]
        if part.kind == "H":
            continue
        a, b = symbol_bounds(symbol, layout.rotations.get(part.name, 0), body_only=True)
        if any(segment_intersects_box(edge, a, b) for edge in edges):
            return False
    return True


def _distinct_visible_nets(visible, *, checked_indices=None):
    """Sweep across the drawing, testing only overlapping wire bounds."""
    bounds = []
    for net, segments in enumerate(visible):
        for edge in segments:
            (x1, y1), (x2, y2) = edge
            bounds.append((min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2),
                           net, wire_axis(edge) is None, edge))
    if checked_indices is not None:
        # Manual repairs usually edit one net. Skip the quadratic comparison
        # among untouched wires, while retaining every edited/foreign pair.
        for left, right, low, high, net, diagonal, edge in bounds:
            if net not in checked_indices:
                continue
            for other_left, other_right, other_low, other_high, other, other_diagonal, other_edge in bounds:
                if (other == net or other_right < left - 1e-8 or other_left > right + 1e-8
                        or other_high < low - 1e-8 or other_low > high + 1e-8):
                    continue
                if not (diagonal or other_diagonal) or segments_intersect(edge, other_edge):
                    return False
        return True
    active = []
    for left, right, low, high, net, diagonal, edge in sorted(bounds):
        active = [span for span in active if span[0] >= left - 1e-8]
        for _, other_low, other_high, other, other_diagonal, other_edge in active:
            if other == net or other_high < low - 1e-8 or high < other_low - 1e-8:
                continue
            if not (diagonal or other_diagonal) or segments_intersect(edge, other_edge):
                return False
        active.append((right, low, high, net, diagonal, edge))
    return True


# Compare physical wire islands and terminal attachments against the compiled net sets.
# Both missing connections and unintended cross-net contacts invalidate the drawing.
def preserves_nets(circuit, drawing, *, nets=None):
    """Check connectivity and shorts, optionally only for explicitly edited nets.

    Scoped checks still compare edited nets against every foreign net. This
    allows repairing one route while unrelated existing faults remain visible.
    """
    checked = None if nets is None else frozenset(nets)
    if Counter(part for part, *_ in drawing.symbols) != Counter(circuit.components):
        return False
    pins, links = defaultdict(list), defaultdict(list)
    for net, point in chain(drawing.pins(), drawing.boundary.items()):
        pins[net].append((point, point))
    for point in drawing.grounds:
        net = drawing.ground_names.get(point, "0")
        if net not in circuit.grounds and (checked is None or net in checked):
            return False
        links[net].append((point, point))
    names, net_names = {}, {}
    for net, point, name, *_ in drawing.connectors:
        name = name.casefold()
        other = names.setdefault(name, net)
        if ((other != net and (checked is None or net in checked or other in checked))
                or (net_names.setdefault(net, name) != name and (checked is None or net in checked))):
            return False
        links[net].append((point, point))
    unknown = (drawing.wires.keys() | links.keys() | drawing.junctions.keys()) - pins.keys()
    if unknown and (checked is None or unknown & checked):
        return False
    if checked is not None and not checked <= pins.keys():
        return False

    visible = []
    checked_indices = set() if checked is not None else None
    all_nets = pins if checked is None else dict.fromkeys(chain(pins, drawing.wires, links, drawing.junctions))
    for net in all_nets:
        terminals = pins.get(net, [])
        wires = drawing.wires.get(net, [])
        junctions = [(p, p) for p in drawing.junctions.get(net, [])]
        # A ground or matching connector can join physically separate islands.
        conductors = [*terminals, *wires]
        endpoints = links[net]
        if checked is None or net in checked:
            pending = conductors + junctions
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
            if checked_indices is not None:
                checked_indices.add(len(visible))

        segments = [*terminals, *endpoints, *junctions]
        if wires:
            segments.extend(drawing.visible_segments(net))
        visible.append(segments)

    if drawing.underside_wires:
        return (_layered_connectivity(drawing, all_nets, pins, links, checked)
                and _distinct_layered_nets(drawing, all_nets, pins, links, checked))
    return _distinct_visible_nets(visible, checked_indices=checked_indices)


def _layered_connectivity(drawing, nets, pins, links, checked):
    """Connect faces at terminals, whole holes, and explicit layer splices."""
    from circuitlab.layout.board_layers import layer_wires
    from circuitlab.layout.wire_edit import clean_point, wire_graph

    faces = [layer_wires(drawing), layer_wires(drawing, underside=True)]
    for net in nets:
        if checked is not None and net not in checked:
            continue
        terminals = {clean_point(at) for at, _ in [*pins.get(net, ()), *links[net]]}
        vias = {clean_point(at) for edge in drawing.wires.get(net, ()) for at in edge
                if all(abs(value - round(value)) < 1e-7 for value in at)}
        # Explicit changes of face can be floating splices or half-hole
        # routing joints, so retain their exact intentional attachments.
        vias.update(clean_point(at) for edge in drawing.underside_wires.get(net, ()) for at in edge)
        graphs = [wire_graph(face.get(net, ()), terminals | vias) for face in faces]
        graph = defaultdict(set)
        for side, wires in enumerate(graphs):
            for at in terminals:
                wires.setdefault(at, set())
            for at, neighbors in wires.items():
                graph[side, at].update((side, other) for other in neighbors)
        for at in terminals | vias:
            if at in graphs[0] and at in graphs[1]:
                graph[0, at].add((1, at))
                graph[1, at].add((0, at))
        if not graph:
            continue
        pending = [(side, clean_point(at)) for side in (0, 1) for at, _ in links[net]] or [next(iter(graph))]
        reached = set()
        while pending:
            point = pending.pop()
            if point not in reached:
                reached.add(point)
                pending.extend(graph[point] - reached)
        if reached != graph.keys():
            return False
    return True


def _distinct_layered_nets(drawing, nets, pins, links, checked):
    """Opposite-face crossings are insulated; pins and shared holes are not."""
    from circuitlab.layout.board_layers import layer_wires

    faces = [layer_wires(drawing), layer_wires(drawing, underside=True)]
    for wires in faces:
        face = replace(drawing, wires=wires, crossings={}, junctions={}, labels={}, underside_wires={})
        mark_crossings(face)
        visible, indices = [], set() if checked is not None else None
        for net in nets:
            if indices is not None and net in checked:
                indices.add(len(visible))
            segments = [*pins.get(net, ()), *links[net], *((at, at) for at in face.junctions.get(net, ()))]
            if net in wires:
                segments.extend(face.visible_segments(net))
            visible.append(segments)
        if not _distinct_visible_nets(visible, checked_indices=indices):
            return False
    # A wire ending at a hole attaches through the board. A mere crossing
    # between the continuous spans of different faces remains insulated.
    for first, second in (faces, reversed(faces)):
        for net, wires in first.items():
            splices = {at for edge in drawing.underside_wires.get(net, ()) for at in edge}
            endpoints = {at for edge in chain(drawing.wires.get(net, ()), drawing.underside_wires.get(net, ()))
                         for at in edge if (at in splices or all(abs(value - round(value)) < 1e-7 for value in at))
                         and any(segments_intersect((at, at), run) for run in wires)}
            for other, edges in second.items():
                if other != net and (checked is None or net in checked or other in checked):
                    if any(segments_intersect((at, at), edge) for at in endpoints for edge in edges):
                        return False
    return True


# A crossing inside one net is a junction; a crossing between different nets is a visual
# bridge/gap. The display distinction must not change the underlying wire graph.
def mark_crossings(drawing):
    """Mark junctions for the same net and visual gaps for distinct nets."""
    if drawing.underside_wires:
        from circuitlab.layout.board_layers import layer_wires
        crossings, junctions, contacts = {}, defaultdict(list), []
        for underside in (False, True):
            face = replace(drawing, wires=layer_wires(drawing, underside=underside),
                           crossings={}, junctions={}, labels={}, underside_wires={})
            contacts.extend(mark_crossings(face))
            crossings.update(face.crossings)
            for net, points in face.junctions.items():
                junctions[net].extend(points)
        drawing.crossings = crossings
        drawing.junctions = dict(junctions)
        return contacts
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
