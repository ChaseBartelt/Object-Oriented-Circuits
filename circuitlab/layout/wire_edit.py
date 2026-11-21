"""Edit a schematic's wire geometry while retaining its electrical nets."""
# Convert visible runs into a graph split at tees and overlaps before editing them.
# Cleanup can remove dead stubs and collinear bends while keeping fixed contacts connected.

from collections import Counter, defaultdict
from dataclasses import replace
from itertools import chain
from math import dist

from circuitlab.layout.checks import clear_of_bodies, intersection, segments_intersect
from circuitlab.layout.geometry import along, wire_axis


def clean_point(point):
    return tuple(round(x, 8) for x in point)


def route_anchors(
    layout, net, pins=None, *, moving_ground=None, moving_connector=None, moving_boundary=None,
):
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
        clean_point(point) for name, point in layout.boundary.items()
        if name == net and name != moving_boundary and point != moving_ground
    }


# Split runs at all real contacts before graph traversal so a tee in the middle of a long
# segment is recognized as a connection rather than an unrelated crossing.
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
            cuts[a, b].update(point for point in endpoints if segments_intersect((point, point), (a, b)))
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


def graph_edges(graph):
    """Yield each undirected wire edge once, retaining graph iteration order."""
    return ((a, b) for a, neighbors in graph.items() for b in neighbors if a < b)


def physical_wire_islands(drawing, net, extra=()):
    """Group contacts by visible wires, without joining matching net labels."""
    anchors = route_anchors(drawing, net) | set(map(clean_point, extra))
    graph = wire_graph(drawing.wires.get(net, ()), anchors)
    islands = {}
    for point in sorted(graph.keys() | anchors):
        if point in islands:
            continue
        island = len(islands)
        pending = [point]
        while pending:
            current = pending.pop()
            if current not in islands:
                islands[current] = island
                pending.extend(graph.get(current, ()))
    return graph, islands


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


def count_wire_bends(layout):
    """Count corners between consecutive strokes, leaving branch tees separate."""
    bends = 0
    for edges in layout.wires.values():
        neighbors = defaultdict(set)
        for a, b in edges:
            if a != b:
                neighbors[a].add(b)
                neighbors[b].add(a)
        for at, ends in neighbors.items():
            if len(ends) == 2 and not segments_intersect((at, at), tuple(ends)):
                bends += 1
    return bends


def merge_strokes(edges):
    """Yield the visible union of collinear wire strokes without filling crossing gaps."""
    lines = defaultdict(list)
    diagonals = set()
    for a, b in edges:
        if a == b:
            continue
        axis = wire_axis((a, b))
        if axis is None:
            diagonals.add(tuple(sorted((a, b))))
        else:
            lines[axis, a[1 - axis]].append(tuple(sorted((a[axis], b[axis]))))
    for (axis, fixed), spans in lines.items():
        def segment(low, high):
            return ((low, fixed), (high, fixed)) if axis == 0 else ((fixed, low), (fixed, high))

        start, end = None, None
        for low, high in sorted(spans):
            if start is not None and low > end + 1e-8:
                yield segment(start, end)
                start = None
            if start is None:
                start, end = low, high
            else:
                end = max(end, high)
        yield segment(start, end)
    yield from sorted(diagonals)


def _remove_cycles(graph):
    """Keep a connected forest of the existing strokes in each physical island."""
    parent = {point: point for point in graph}

    def root(point):
        while parent[point] != point:
            parent[point] = parent[parent[point]]
            point = parent[point]
        return point

    for a, b in sorted(graph_edges(graph), key=lambda edge: (dist(*edge), edge)):
        first, second = root(a), root(b)
        if first == second:
            graph[a].remove(b)
            graph[b].remove(a)
        else:
            parent[first] = second


# Preserve anchored contacts while pruning dead ends and collapsing straight-through bends.
def compact_wire(edges, anchors, *, prune_cycles=False):
    """Remove dead stubs and merge collinear bends away from terminals."""
    graph = wire_graph(edges, anchors)
    if prune_cycles:
        _remove_cycles(graph)
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
        sorted(graph_edges(graph)),
        sorted(p for p, neighbors in graph.items() if len(neighbors) > 2),
    )


def route_trial(layout, **changes):
    """Copy the editable routes before trying a change to a drawing."""
    return replace(
        layout,
        wires={net: list(edges) for net, edges in layout.wires.items()},
        junctions={net: list(points) for net, points in layout.junctions.items()},
        labels=dict(layout.labels),
        crossings={},
        **changes,
    )


def replace_route(layout, net, edges, *, prune_cycles=False):
    edges, junctions = compact_wire(edges, route_anchors(layout, net), prune_cycles=prune_cycles)
    for routes, items in ((layout.wires, edges), (layout.junctions, junctions)):
        if items:
            routes[net] = items
        else:
            routes.pop(net, None)
    if not edges or ("net", net) not in layout.label_offsets:
        layout.labels.pop(net, None)


def collinear_run(graph, edge, axis):
    """Follow one continuous run through any intermediate branch tees."""
    start, end = edge
    lane = start[1 - axis]
    pending, reached = [start], set()
    while pending:
        at = pending.pop()
        if at not in reached:
            reached.add(at)
            pending.extend(next_at for next_at in graph.get(at, ()) if next_at[1 - axis] == lane)
    return reached if end in reached else set()


def _back_turns(graph, run, anchors, edge, axis):
    """Identify an unanchored end that immediately reverses along the run."""
    first, last = edge
    for at in edge:
        exits = graph[at] - run
        if at in anchors or len(exits) != 1:
            continue
        bend, = exits
        if bend in anchors or len(graph[bend]) != 2 or wire_axis((at, bend)) != 1 - axis:
            continue
        fixed, = graph[bend] - {at}
        if wire_axis((bend, fixed)) == axis and first[axis] <= fixed[axis] <= last[axis]:
            yield at, bend, fixed


def wire_slide_candidates(original, net, edge, distance, *, snap=0, pointer=None):
    """Try snapped positions before the raw pointer position."""
    edge = tuple(sorted(map(clean_point, edge)))
    a, b = edge
    axis = wire_axis(edge)
    if a == b or axis is None:
        yield original, None
        return
    anchors = route_anchors(original, net)
    graph = wire_graph(original.wires[net], anchors | set(edge))
    on_run = collinear_run(graph, edge, axis)
    if not on_run:
        yield original, None
        return
    edge = min(on_run), max(on_run)
    back_turns = tuple(_back_turns(graph, on_run, anchors, edge, axis))
    attached = []
    snap_offsets = Counter()
    for p, neighbors in graph.items():
        if p in on_run and (p in anchors or any(q not in on_run for q in neighbors)):
            attached.append(p)
            if snap:
                for q in neighbors:
                    if wire_axis((p, q)) == 1 - axis and (
                        q in anchors or any(wire_axis((q, r)) == axis for r in graph[q])
                    ):
                        snap_offsets[q[1 - axis] - a[1 - axis]] += 1
    fixed_edges = [(p, q) for p, q in graph_edges(graph) if p not in on_run or q not in on_run]

    # Follow the nearest row first; remove more jogs only to break a tie.
    pointer = distance if pointer is None else pointer
    offsets = sorted(
        (offset for offset in snap_offsets if abs(offset - pointer) <= snap),
        key=lambda offset: (abs(offset - pointer), -snap_offsets[offset], offset),
    )
    for offset in offsets:
        for drawing in _shifted_runs(original, net, edge, axis, offset, fixed_edges, attached, back_turns):
            if drawing is not original:
                yield drawing, offset
    for drawing in _shifted_runs(original, net, edge, axis, distance, fixed_edges, attached, back_turns):
        yield drawing, None


def _shifted_runs(original, net, edge, axis, distance, fixed_edges, attached, back_turns):
    if not distance:
        yield original
        return

    lane = round(edge[0][1 - axis] + distance, 8)

    def shifted(point):
        coords = list(point)
        coords[1 - axis] = lane
        return clean_point(coords)

    shifted_edge = tuple(map(shifted, edge))

    def reconnect(edges, terminals):
        drawing = route_trial(original)
        replace_route(drawing, net, [*edges, shifted_edge, *((p, shifted(p)) for p in terminals)])
        # A dangling piece may be pruned entirely when it was attached at one end.
        return drawing if any(
            segments_intersect((p, q), shifted_edge)
            and p[1 - axis] == q[1 - axis] == shifted_edge[0][1 - axis]
            for p, q in drawing.wires.get(net, ())
        ) else original

    if back_turns:
        removed, tails = set(), set()
        terminals = set(attached)
        for at, bend, fixed in back_turns:
            if clear_of_bodies([(fixed, shifted(fixed))], original):
                removed.update((frozenset((at, bend)), frozenset((bend, fixed))))
                tails.add(fixed)
                terminals.remove(at)
        if removed:
            trial = reconnect([edge for edge in fixed_edges if frozenset(edge) not in removed], terminals | tails)
            if trial is not original:
                yield trial
    yield reconnect(fixed_edges, attached)
