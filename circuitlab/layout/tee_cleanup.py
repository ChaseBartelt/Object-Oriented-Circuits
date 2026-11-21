"""Remove detours where a wire can meet an existing straight trunk sooner."""

from math import dist

from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.geometry import wire_axis
from circuitlab.layout.route_quality import crossing_count
from circuitlab.layout.wire_edit import collinear_run, graph_edges, replace_route, route_anchors, route_trial, wire_graph


def _branch_run(graph, anchors, start, neighbor):
    """Walk away from a tee until the next fixed contact or junction."""
    run = [start, neighbor]
    while run[-1] not in anchors:
        exits = graph[run[-1]] - {run[-2]}
        if len(exits) != 1:
            break
        point, = exits
        if point in run:
            break
        run.append(point)
    return run


def _branch_shortcuts(run, anchors, trunk_axis, span):
    tee = run[0]
    branch_axis = 1 - trunk_axis
    low, high = span
    length = 0
    for index, point in enumerate(run[1:], start=1):
        previous = run[index - 1]
        length += dist(previous, point)
        offset = point[branch_axis] - tee[branch_axis]
        if index < 2 or not offset or not low <= point[trunk_axis] <= high:
            continue
        if index + 1 < len(run):
            # Extend the incoming stroke straight to the trunk.
            following = run[index + 1]
            if wire_axis((point, following)) != branch_axis or (following[branch_axis] - point[branch_axis]) * offset <= 0:
                continue
        elif point not in anchors or wire_axis((previous, point)) != trunk_axis:
            # A short perpendicular leg into a fixed pin can also be removed.
            continue
        tap = list(point)
        tap[branch_axis] = tee[branch_axis]
        shortcut = point, tuple(tap)
        saving = length - dist(*shortcut)
        if saving > 1e-8:
            removed = {frozenset(edge) for edge in zip(run[:index], run[1:index + 1])}
            yield saving, shortcut, removed


def _shortcuts(graph, anchors):
    """Find branches that can meet the same trunk sooner."""
    spans = {}
    for tee, neighbors in graph.items():
        if len(neighbors) < 3:
            continue
        for trunk_axis in (0, 1):
            trunk = [at for at in neighbors if wire_axis((tee, at)) == trunk_axis]
            if not any(at[trunk_axis] < tee[trunk_axis] for at in trunk) or not any(
                at[trunk_axis] > tee[trunk_axis] for at in trunk
            ):
                continue
            if (trunk_axis, tee) not in spans:
                on_trunk = collinear_run(graph, (tee, trunk[0]), trunk_axis)
                span = (min(at[trunk_axis] for at in on_trunk), max(at[trunk_axis] for at in on_trunk))
                spans.update(((trunk_axis, at), span) for at in on_trunk)
            branch_axis = 1 - trunk_axis
            for neighbor in sorted(neighbors):
                if wire_axis((tee, neighbor)) != branch_axis:
                    continue
                run = _branch_run(graph, anchors, tee, neighbor)
                yield from _branch_shortcuts(run, anchors, trunk_axis, spans[trunk_axis, tee])


def reattach_tee_runs(circuit, drawing, *, budget=16):
    """Replace sideways detours with direct tee connections when visibly valid."""
    pins = list(drawing.pins())
    crossings = crossing_count(drawing)
    for net in list(drawing.wires):
        anchors = route_anchors(drawing, net, pins)
        graph = wire_graph(drawing.wires[net], anchors)
        proposals = sorted(_shortcuts(graph, anchors), key=lambda option: (-option[0], option[1]))
        for _, shortcut, removed in proposals:
            if budget <= 0:
                return drawing
            budget -= 1
            if not clear_of_bodies([shortcut], drawing):
                continue
            edges = [edge for edge in graph_edges(graph) if frozenset(edge) not in removed]
            trial = route_trial(drawing)
            replace_route(trial, net, [*edges, shortcut])
            label = drawing.labels.get(net)
            if label is not None and any(
                segments_intersect((label, label), edge) for edge in trial.wires.get(net, ())
            ):
                trial.labels[net] = label
            mark_crossings(trial)
            count = crossing_count(trial)
            if count <= crossings and preserves_nets(circuit, trial):
                drawing, crossings = trial, count
                break
    return drawing
