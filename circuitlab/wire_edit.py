"""Edit a schematic's wire geometry while retaining its electrical nets."""

from collections import Counter, defaultdict
from dataclasses import replace
from itertools import chain

from .layout import along, wire_axis
from .layout_checks import intersection, segments_intersect


def clean_point(point):
    return tuple(round(x, 8) for x in point)


def route_anchors(layout, net, pins=None, *, moving_ground=None, moving_connector=None):
    if pins is None:
        pins = layout.pins()
    return {
        clean_point(pin) for name, pin in pins if name == net
    } | {
        clean_point(point) for i, (name, point, *_) in enumerate(layout.connectors)
        if name == net and i != moving_connector
    } | {
        clean_point(point) for point in layout.grounds
        if point != moving_ground and layout.ground_names.get(point, "0") == net
    } | {
        clean_point(point) for name, point in layout.boundary.items() if name == net
    }


def wire_graph(edges, anchors):
    """Split overlaps and tees into a graph of distinct straight runs."""
    horizontal, vertical, diagonal = [], [], []
    for a, b in edges:
        a, b = sorted((clean_point(a), clean_point(b)))
        if a != b:
            axis = wire_axis((a, b))
            (diagonal if axis is None else (horizontal, vertical)[axis]).append((a, b))
    cuts = {edge: set(edge) for edge in horizontal + vertical + diagonal}
    for a, b in horizontal:
        for c, d in vertical:
            x, y = c[0], a[1]
            if a[0] <= x <= b[0] and c[1] <= y <= d[1]:
                cuts[a, b].add((x, y))
                cuts[c, d].add((x, y))
    for index, edge in enumerate(diagonal):
        for other in chain(horizontal, vertical, diagonal[:index]):
            crossing = intersection(edge, other)
            if crossing is None or not all(0 <= value <= 1 for value in crossing):
                continue
            point = clean_point(along(*edge, crossing[0]))
            cuts[edge].add(point)
            cuts[other].add(point)
    endpoints = anchors | {p for edge in cuts for p in edge}
    lines = defaultdict(list)
    for a, b in cuts:
        axis = wire_axis((a, b))
        if axis is None:
            # Diagonal leads are terminal runs. Split at any known attachment.
            for point in endpoints:
                if segments_intersect((point, point), (a, b)):
                    cuts[a, b].add(point)
        else:
            lines[axis, a[1 - axis]].append((a, b))
    for (axis, _), group in lines.items():
        points = anchors.union(*(cuts[edge] for edge in group))
        for edge in group:
            a, b = edge
            cuts[edge].update(
                p for p in points
                if p[1 - axis] == a[1 - axis] and a[axis] <= p[axis] <= b[axis]
            )
    graph = defaultdict(set)
    for points in cuts.values():
        ordered = sorted(points)
        for a, b in zip(ordered, ordered[1:]):
            graph[a].add(b)
            graph[b].add(a)
    return graph


def terminal_run(graph, start, fixed):
    """Follow a terminal's lead until a junction or another fixed attachment."""
    run = [start]
    previous = None
    while start not in fixed:
        neighbors = graph.get(start, set()) - {previous}
        if len(neighbors) != 1:
            break
        end = next(iter(neighbors))
        run.append(end)
        previous, start = start, end
    return run


def compact_wire(edges, anchors):
    """Remove dead stubs and merge collinear bends away from terminals."""
    graph = wire_graph(edges, anchors)
    pending = [p for p, neighbors in graph.items() if len(neighbors) == 1 and p not in anchors]
    while pending:
        point = pending.pop()
        neighbors = graph.pop(point, ())
        for other in neighbors:
            graph[other].remove(point)
            if len(graph[other]) == 1 and other not in anchors:
                pending.append(other)
    for point in list(graph):
        if point in anchors or len(graph[point]) != 2:
            continue
        a, b = graph[point]
        if segments_intersect((point, point), (a, b)):
            graph[a].remove(point)
            graph[b].remove(point)
            graph[a].add(b)
            graph[b].add(a)
            del graph[point]
    return (
        sorted((a, b) for a, neighbors in graph.items() for b in neighbors if a < b),
        sorted(p for p, neighbors in graph.items() if len(neighbors) > 2),
    )


def replace_route(layout, net, edges):
    anchors = route_anchors(layout, net)
    edges, junctions = compact_wire(edges, anchors)
    if edges:
        layout.wires[net] = edges
    else:
        layout.wires.pop(net, None)
    if junctions:
        layout.junctions[net] = junctions
    else:
        layout.junctions.pop(net, None)
    layout.labels.pop(net, None)


def wire_slide_candidates(original, net, edge, distance, *, snap=0):
    """Try snapped positions before the raw pointer position."""
    edge = tuple(sorted(map(clean_point, edge)))
    a, b = edge
    axis = wire_axis(edge)
    if a == b or axis is None or not distance:
        yield original, None
        return
    anchors = route_anchors(original, net)
    graph = wire_graph(original.wires[net], anchors | set(edge))
    if not any(
        segments_intersect((p, p), edge)
        and any(segments_intersect((q, q), edge) for q in neighbors)
        for p, neighbors in graph.items()
    ):
        yield original, None
        return
    attached = []
    edges = []
    snap_offsets = Counter()
    for p, neighbors in graph.items():
        on_run = segments_intersect((p, p), edge)
        if on_run and (p in anchors or any(not segments_intersect((q, q), edge) for q in neighbors)):
            attached.append(p)
            if snap:
                for q in neighbors:
                    if wire_axis((p, q)) == 1 - axis and (
                        q in anchors or any(wire_axis((q, r)) == axis for r in graph[q])
                    ):
                        snap_offsets[q[1 - axis] - a[1 - axis]] += 1
        edges.extend(
            (p, q) for q in neighbors if p < q
            and (not on_run or not segments_intersect((q, q), edge))
        )

    # Prefer the alignment that removes the most jogs within reach.
    offsets = sorted(
        (offset for offset in snap_offsets if abs(offset - distance) <= snap),
        key=lambda offset: (-snap_offsets[offset], abs(offset - distance), offset),
    )
    for offset in offsets:
        drawing = _shift_run(original, net, edge, axis, offset, edges, attached)
        if drawing is not original:
            yield drawing, offset
    yield _shift_run(original, net, edge, axis, distance, edges, attached), None


def _shift_run(original, net, edge, axis, distance, edges, attached):
    def shifted(point):
        coords = list(point)
        coords[1 - axis] += distance
        return clean_point(coords)

    shifted_edge = tuple(map(shifted, edge))
    drawing = replace(
        original, wires=dict(original.wires),
        junctions={name: list(points) for name, points in original.junctions.items()},
        labels=dict(original.labels), crossings={},
    )
    replace_route(drawing, net, [
        *edges, shifted_edge, *((p, shifted(p)) for p in attached),
    ])
    # A dangling piece may be pruned entirely when it was attached at one end.
    if not any(
        segments_intersect((p, q), shifted_edge)
        and p[1 - axis] == q[1 - axis] == shifted_edge[0][1 - axis]
        for p, q in drawing.wires.get(net, ())
    ):
        return original
    return drawing
