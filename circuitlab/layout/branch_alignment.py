"""Align passive stacks between existing wire trunks."""

from collections import Counter, defaultdict
from math import dist, floor

from circuitlab.layout.checks import mark_crossings, segments_intersect
from circuitlab.layout.editing import boxes_overlap, move_symbols
from circuitlab.layout.geometry import routed_pins, symbol_bounds, wire_axis
from circuitlab.layout.lead_spacing import TERMINAL_SPANS
from circuitlab.layout.route_quality import crossing_count, improves_placement, wire_length
from circuitlab.layout.wire_edit import (
    clean_point, graph_edges, replace_route, route_anchors, route_trial, terminal_run, wire_graph,
)


PASSIVES = {"R", "C", "L", "X", "Z"}


def _stack_pairs(drawing):
    """Find two vertical devices that already share a physical middle terminal."""
    lower_ends, upper_ends = defaultdict(list), defaultdict(list)
    for symbol in drawing.symbols:
        part = symbol[0]
        if part.kind not in PASSIVES or part.p == part.n:
            continue
        turns = drawing.rotations.get(part.name, 0)
        lower, upper = sorted(
            routed_pins(symbol, turns, drawing.pin_grid),
            key=lambda pin: pin[1][1],
        )
        if wire_axis((lower[1], upper[1])) == 1 and lower[1] != upper[1]:
            lower_ends[lower].append((part.name, upper))
            upper_ends[upper].append((part.name, lower))

    for middle, above in lower_ends.items():
        below = upper_ends.get(middle, ())
        if len(above) == len(below) == 1:
            yield middle, above[0], below[0]


def _run(drawing, net, start, pins):
    anchors = route_anchors(drawing, net, pins)
    graph = wire_graph(drawing.wires.get(net, ()), anchors)
    return graph, terminal_run(graph, start, anchors - {start})


def _replace_run(drawing, net, graph, before, after):
    removed = {frozenset(edge) for edge in zip(before, before[1:])}
    edges = [edge for edge in graph_edges(graph) if frozenset(edge) not in removed]
    label = drawing.labels.get(net)
    replace_route(drawing, net, [*edges, *zip(after, after[1:])])
    if label is not None and any(
        segments_intersect((label, label), edge) for edge in drawing.wires.get(net, ())
    ):
        drawing.labels[net] = label


def _shift_midpoint_lead(drawing, net, delta, graph, run):
    """Extend the existing horizontal lane to the moved vertical midpoint."""
    first_turn = next((
        i for i, (a, b) in enumerate(zip(run, run[1:])) if wire_axis((a, b)) != 1
    ), len(run))
    if first_turn >= len(run) - 1 or wire_axis(tuple(run[first_turn:first_turn + 2])) != 0:
        return
    fixed = run[first_turn + 1]
    shifted = [clean_point((x + delta, y)) for x, y in run[:first_turn + 1]]
    _replace_run(drawing, net, graph, run[:first_turn + 2], [*shifted, fixed])


def _horizontal_trunk(graph, run):
    """Limit a tee shift to the trunk next to the end of its old branch."""
    if len(run) < 2:
        return None
    joint = run[-1]
    along = [point[0] for point in graph[joint]
             if point[1] == joint[1] and point != run[-2]]
    if not along:
        return None
    return min(joint[0], *along), max(joint[0], *along)


def _bridge_stack_positions(drawing, upper_pin, lower_pin, pins, grid):
    """Find a common column between two horizontal tracks outside a vertical stack."""
    _, (x, top) = upper_pin
    _, (_, bottom) = lower_pin
    routes = [_run(drawing, net, at, pins) for net, at in (upper_pin, lower_pin)]
    (_, feed), (_, load) = routes
    if feed[-1][1] <= top or load[-1][1] >= bottom:
        return None
    spans = [_horizontal_trunk(graph, run) for graph, run in routes]
    if any(span is None for span in spans):
        return None
    left, right = max(span[0] for span in spans), min(span[1] for span in spans)
    if left > right:
        return None
    nearest = min(max(x, left), right)
    center = min(max(round((left + right) / (2 * grid)) * grid, left), right)
    return routes, tuple(dict.fromkeys((nearest, center)))


def _shift_bridge_stack(circuit, drawing, names, upper_pin, lower_pin, routes, column):
    dx = clean_point((column - upper_pin[1][0], 0))[0]
    trial = move_symbols(circuit, drawing, names, (dx, 0))
    if trial is None:
        return None
    trial = route_trial(trial)
    for (net, at), (graph, run) in zip((upper_pin, lower_pin), routes):
        if net in drawing.labels:
            trial.labels[net] = drawing.labels[net]
        _replace_run(trial, net, graph, run, ((column, at[1]), (column, run[-1][1])))
    mark_crossings(trial)
    return trial


def _unstacked_pairs(circuit, drawing, pins):
    by_net = defaultdict(list)
    supply_nets = circuit.grounds | {net for part in circuit.components if part.is_source for net in part.terminal_nets}
    for symbol in drawing.symbols:
        part = symbol[0]
        if part.kind not in PASSIVES or part.p == part.n:
            continue
        turns = drawing.rotations.get(part.name, 0)
        first, last = routed_pins(symbol, turns, drawing.pin_grid)
        for middle, outer in ((first, last), (last, first)):
            if middle[0] not in supply_nets and outer[0] not in circuit.grounds:
                by_net[middle[0]].append((symbol, middle[1], outer))
    for net, pair in by_net.items():
        if len(pair) != 2 or pair[0][1] == pair[1][1] or pair[0][2][0] == pair[1][2][0]:
            continue
        feeds = [(symbol, pin, _run(drawing, *pin, pins)) for symbol, _, pin in pair]
        feeds.sort(key=lambda branch: branch[2][1][-1][1], reverse=True)
        spans = [_horizontal_trunk(*route) for _, _, route in feeds]
        if any(span is None for span in spans):
            continue
        start, end = max(span[0] for span in spans), min(span[1] for span in spans)
        if start <= end:
            yield net, pair, feeds, start, end


def _middle_trunk(drawing, net, pair, pins):
    anchors = route_anchors(drawing, net, pins)
    graph = wire_graph(drawing.wires.get(net, ()), anchors)
    removed = set()
    for _, at, _ in pair:
        lead = terminal_run(graph, at, anchors - {at})
        removed.update(frozenset(edge) for edge in zip(lead, lead[1:]))
    return [edge for edge in graph_edges(graph) if frozenset(edge) not in removed]


def _stack_between_trunks(drawing, net, feeds, column, heights, middle_wires, midpoint, grid):
    upper, middle, lower = heights
    updated = {}
    for (symbol, pin, _), height in zip(feeds, (upper, lower)):
        part, _, _, top = symbol
        outer, joint = (column, height), (column, middle)
        a, b = (outer, joint) if part.p == pin[0] else (joint, outer)
        updated[part.name] = part, a, b, top
    for symbol in updated.values():
        body = symbol_bounds(symbol, 0, body_only=True)
        if any(boxes_overlap(body, symbol_bounds(other, drawing.rotations.get(other[0].name, 0), body_only=True))
               for other in drawing.symbols if other[0].name not in updated):
            return
    symbols = [updated.get(symbol[0].name, symbol) for symbol in drawing.symbols]
    base = route_trial(
        drawing, symbols=symbols, positions={s[0].name: s[1] for s in symbols},
        rotations={name: turn for name, turn in drawing.rotations.items() if name not in updated},
    )
    for (_, (outer_net, _), (graph, run)), height in zip(feeds, (upper, lower)):
        _replace_run(base, outer_net, graph, run, ((column, height), (column, run[-1][1])))

    if not midpoint:
        replace_route(base, net, middle_wires)
        mark_crossings(base)
        yield base
        return
    # Move away from the resistor bodies before turning onto the old midpoint
    # trunk. Retain the wire that leads to the rest of the circuit.
    for side in (-1, 1):
        offset = side * (max(grid, .5))
        x = clean_point((column + offset, 0))[0]
        touches = [a[1] for a, b in middle_wires if a[1] == b[1] and a[0] <= x <= b[0]]
        if not touches:
            continue
        y = min(touches, key=lambda value: abs(value - middle))
        trial = route_trial(base)
        points = ((column, middle), (x, middle), (x, y))
        replace_route(trial, net, [*middle_wires, *zip(points, points[1:])], prune_cycles=True)
        mark_crossings(trial)
        yield trial


def _new_stack_candidates(circuit, drawing, pins, budget, grid):
    """Place two adjacent passives end-to-end across parallel tracks."""
    def proximity(plan):
        _, pair, feeds, _, _ = plan
        y = {symbol[0].name: at[1] for symbol, at, _ in pair}
        upper, lower = feeds
        facing = (y[upper[0][0].name] < upper[1][1][1] and y[lower[0][0].name] > lower[1][1][1])
        return not facing, dist(pair[0][1], pair[1][1])

    for net, pair, feeds, start, end in sorted(_unstacked_pairs(circuit, drawing, pins), key=proximity):
        top, bottom = (route[1][-1][1] for _, _, route in feeds)
        spans = [TERMINAL_SPANS[symbol[0].kind] for symbol, _, _ in feeds]
        if top - bottom < sum(spans):
            spans = [span - .25 if symbol[0].kind == "R" else span
                     for span, (symbol, _, _) in zip(spans, feeds)]
        if top - bottom < sum(spans):
            continue
        upper = top - floor((top - bottom - sum(spans)) / (2 * grid)) * grid
        heights = upper, upper - spans[0], upper - sum(spans)
        middle_wires = _middle_trunk(drawing, net, pair, pins)
        stationary = list(pins)
        for _, at, _ in pair:
            stationary.remove((net, at))
        midpoint = route_anchors(drawing, net, stationary)
        if midpoint and not middle_wires:
            continue
        toward = [end, start]
        if middle_wires:
            # Prefer the end near the surviving midpoint route, where adding
            # a new tap can retire the most old branch wire.
            other = min(midpoint, key=lambda pos: (abs(pos[1] - heights[1]), pos), default=(toward[0], 0))
            toward.sort(key=lambda column: abs(column - other[0]))
        near, far = toward
        margin = min(abs(near - far) / 2, 3 * grid)
        x = near + (margin if near < far else -margin)
        columns = dict.fromkeys((x, round((start + end) / (2 * grid)) * grid, near, far))
        for column in columns:
            if budget <= 0:
                return
            budget -= 1
            names = frozenset(symbol[0].name for symbol, _, _ in feeds)
            for trial in _stack_between_trunks(drawing, net, feeds, column, heights, middle_wires, midpoint, grid):
                yield trial, names


def _bends(drawing):
    count = 0
    for edges in drawing.wires.values():
        graph = wire_graph(edges, set())
        for at, neighbors in graph.items():
            if len(neighbors) == 2 and not segments_intersect((at, at), tuple(neighbors)):
                count += 1
    return count


def stacked_alignment_candidates(circuit, drawing, *, budget=8, grid=0.25):
    """Yield valid alignments that trade lateral feed wire for fewer bends."""
    pairs = list(_stack_pairs(drawing))
    pins = list(drawing.pins())
    pin_counts = Counter(pins)
    length, bends = wire_length(drawing), None
    crossings = crossing_count(drawing)

    def improves_stack(trial):
        nonlocal bends
        if bends is None:
            bends = _bends(drawing)
        return (wire_length(trial) <= length + 1e-8 and _bends(trial) < bends
                and crossing_count(trial) <= crossings
                and improves_placement(circuit, drawing, trial, grid=grid, compact=True, straightens_input=True))

    for (middle_net, middle), (upper, upper_pin), (lower, lower_pin) in pairs:
        net, at = upper_pin
        ground, tail = lower_pin
        if net in circuit.grounds or pin_counts[upper_pin] != 1:
            continue
        if ground not in circuit.grounds:
            if net == ground or pin_counts[lower_pin] != 1:
                continue
            bridge = _bridge_stack_positions(drawing, upper_pin, lower_pin, pins, grid)
            if bridge is None:
                continue
            routes, columns = bridge
            names = frozenset((upper, lower))
            for column in columns:
                if budget <= 0:
                    return
                budget -= 1
                trial = _shift_bridge_stack(circuit, drawing, names, upper_pin, lower_pin, routes, column)
                if trial is not None and improves_stack(trial):
                    yield trial, names
                    break
            continue
        source_graph, feed = _run(drawing, net, at, pins)
        trunk = feed[-1]
        dx = clean_point((trunk[0] - at[0], 0))[0]
        if (len(feed) < 3 or not dx or trunk[1] <= at[1]
                or not any(other[0] == trunk[0] and other != feed[-2] for other in source_graph[trunk])):
            continue
        _, return_run = _run(drawing, ground, tail, pins)
        endpoint = return_run[-1]
        if (endpoint not in drawing.grounds or drawing.ground_names.get(endpoint, "0") != ground
                or any(point[0] != tail[0] for point in return_run)):
            continue
        if budget <= 0:
            return
        budget -= 1
        names = frozenset((upper, lower))
        trial = move_symbols(circuit, drawing, names | {("ground", endpoint)}, (dx, 0))
        if trial is None:
            continue
        trial = route_trial(trial)
        for label_net in (net, middle_net):
            if label_net in drawing.labels:
                trial.labels[label_net] = drawing.labels[label_net]
        aligned = clean_point((trunk[0], at[1]))
        _replace_run(trial, net, source_graph, feed, (aligned, trunk))
        midpoint_graph, run = _run(drawing, middle_net, middle, pins)
        _shift_midpoint_lead(trial, middle_net, dx, midpoint_graph, run)
        mark_crossings(trial)
        if improves_stack(trial):
            yield trial, names

    for trial, names in _new_stack_candidates(circuit, drawing, pins, budget, grid):
        if improves_stack(trial):
            yield trial, names
