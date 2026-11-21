"""Simplify wires and optionally reposition parts, preserving electrical nets."""
# Wire simplification and component repositioning are separate, optional geometry operations.
# Each candidate must improve the visible route and preserve net identity; bounded searches
# keep an explicit Optimize wiring action from becoming an unbounded background process.

import math
from collections import Counter
from dataclasses import replace
from statistics import median_low

from circuitlab.layout.branch_alignment import stacked_alignment_candidates
from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets
from circuitlab.layout.editing import move_symbol
from circuitlab.layout.geometry import rotate_point, symbol_bounds, wire_axis
from circuitlab.layout.grid import align_layout
from circuitlab.layout.lead_spacing import shorten_symbol_leads
from circuitlab.layout.passive_alignment import align_passive_tees, rotate_detoured_passives, rotate_passive
from circuitlab.layout.route_quality import crossing_count, improves_placement, layout_span, wire_length, wiring_cost
from circuitlab.layout.tee_cleanup import reattach_tee_runs
from circuitlab.layout.wire_edit import graph_edges, replace_route, route_anchors, route_trial, wire_graph
from circuitlab.math.report import component_labels


def _detour(edges):
    points = [point for edge in edges for point in edge]
    if not points:
        return 0
    xs, ys = zip(*points)
    span = (max(xs) - min(xs)) + (max(ys) - min(ys))
    return sum(math.dist(a, b) for a, b in edges) - span + 0.4 * len(edges)


def _runs_between_attachments(graph, anchors):
    """List each run once, stopping at a junction or fixed contact."""
    stops = anchors | {point for point, neighbors in graph.items() if len(neighbors) != 2}
    seen = set()
    for start in sorted(stops):
        for neighbor in sorted(graph.get(start, ())):
            if frozenset((start, neighbor)) in seen:
                continue
            run = [start, neighbor]
            seen.add(frozenset(run))
            while run[-1] not in stops:
                choices = graph[run[-1]] - {run[-2]}
                if len(choices) != 1:
                    break
                end, = choices
                edge = frozenset((run[-1], end))
                if edge in seen:
                    break
                seen.add(edge)
                run.append(end)
            yield run


def _run_detour(run):
    return sum(math.dist(a, b) for a, b in zip(run, run[1:])) - math.dist(run[0], run[-1])


# Remove unnecessary bends only where terminals and junction anchors remain attached.
def straighten(circuit, original, budget=24, *, simplify=False):
    drawing = original
    length = None
    attempts = 0
    nets = sorted(original.wires, key=lambda net: -_detour(original.wires[net]))
    for net in nets:
        anchors = route_anchors(drawing, net)
        graph = wire_graph(drawing.wires.get(net, ()), anchors)
        runs = sorted(_runs_between_attachments(graph, anchors), key=lambda run: -_run_detour(run))
        for run in runs:
            start, end = run[0], run[-1]
            run_edges = list(zip(run, run[1:]))
            old_length = sum(math.dist(a, b) for a, b in run_edges)
            minimum = abs(start[0] - end[0]) + abs(start[1] - end[1])
            # Stair-step routes can have optimal length but unnecessary bends.
            # Keep deliberately diagonal leads that are shorter than Manhattan.
            if old_length < minimum - 1e-8 or (old_length <= minimum + 1e-8 and (not simplify or len(run) <= 3)):
                continue
            remaining = None
            for corner in ((start[0], end[1]), (end[0], start[1])):
                if attempts >= budget:
                    return drawing
                attempts += 1
                edges = [(a, b) for a, b in ((start, corner), (corner, end)) if a != b]
                if not clear_of_bodies(edges, drawing):
                    continue
                if remaining is None:
                    current = wire_graph(drawing.wires.get(net, ()), anchors)
                    removed = set(map(frozenset, run_edges))
                    # An earlier shortcut may already have replaced part of this run.
                    if any(b not in current.get(a, ()) for a, b in run_edges):
                        break
                    remaining = [edge for edge in graph_edges(current) if frozenset(edge) not in removed]
                trial = route_trial(drawing)
                replace_route(trial, net, remaining + edges)
                mark_crossings(trial)
                if length is None:
                    length = wire_length(drawing)
                trial_length = wire_length(trial)
                shorter = trial_length < length - 1e-8
                simpler = (simplify and trial_length <= length + 1e-8
                           and len(trial.wires.get(net, ())) < len(drawing.wires.get(net, ()))
                           and crossing_count(trial) <= crossing_count(drawing))
                if ((shorter or simpler) and wiring_cost(trial) < wiring_cost(drawing) - 1e-8
                        and preserves_nets(circuit, trial)):
                    drawing = trial
                    length = trial_length
                    break
    return drawing


def _rail_routes(anchors):
    """Propose a shared horizontal or vertical trunk with straight terminal leads."""
    for axis in (0, 1):
        across = 1 - axis
        coordinates = [point[across] for point in anchors]
        lanes = dict.fromkeys((median_low(coordinates), Counter(coordinates).most_common(1)[0][0]))
        for lane in lanes:
            taps = [tuple(lane if i == across else point[i] for i in (0, 1)) for point in anchors]
            first, last = min(taps), max(taps)
            yield [(first, last), *((a, b) for a, b in zip(anchors, taps) if a != b)]


def _share_trunks(circuit, drawing, budget=20, *, protected_nets=()):
    """Replace a zigzagging multi-pin net with a common trunk where it fits."""
    separated = {net for net, *_ in drawing.connectors}
    nets = sorted(drawing.wires, key=lambda net: -_detour(drawing.wires[net]))
    pins = tuple(drawing.pins())
    for net in nets:
        edges = drawing.wires[net]
        if (net in circuit.grounds or net in separated or net in protected_nets
                or len(edges) < 3 or ("net", net) in drawing.label_offsets):
            continue
        anchors = sorted(route_anchors(drawing, net, pins))
        if len(anchors) < 3:
            continue
        best = drawing
        previous_length = sum(math.dist(a, b) for a, b in edges)
        for route in _rail_routes(anchors):
            if sum(math.dist(a, b) for a, b in route) > previous_length + 1e-8:
                continue
            if budget <= 0:
                return best
            budget -= 1
            if not clear_of_bodies(route, drawing):
                continue
            trial = route_trial(drawing)
            replace_route(trial, net, route)
            if net in drawing.labels:
                trunk = max(trial.wires[net], key=lambda edge: (wire_axis(edge) == 0, math.dist(*edge)))
                trial.labels[net] = tuple((a + b) / 2 for a, b in zip(*trunk))
            mark_crossings(trial)
            if wiring_cost(trial) < wiring_cost(best) - 1e-8 and preserves_nets(circuit, trial):
                best = trial
        drawing = best
    return drawing


def _move_candidates(drawing, symbol, grid):
    """Pull pins toward their nearest connected fixed attachments on the grid."""
    own = replace(drawing, symbols=[symbol]).pins()
    others = replace(drawing, symbols=[s for s in drawing.symbols if s != symbol])
    offsets = []
    for net, pin in own:
        targets = route_anchors(others, net)
        if targets:
            target = min(targets, key=lambda p: (math.dist(pin, p), p))
            offsets.append(tuple(b - a for a, b in zip(pin, target)))
    if not offsets:
        return
    dx, dy = (median_low(p[axis] for p in offsets) for axis in (0, 1))
    seen = {(0, 0)}
    for x, y in ((dx, dy), (dx, 0), (0, dy), (dx/2, dy/2), (dx/2, 0), (0, dy/2)):
        delta = tuple(round(round(v / grid) * grid, 8) for v in (x, y))
        if delta not in seen:
            seen.add(delta)
            yield delta


def _close_gap(layout, axis, low, high, amount, grid):
    """Translate whole symbol groups and compress only the wiring between them."""
    def point(position):
        value = position[axis]
        if value >= high:
            value -= amount
        elif value > low:
            value -= amount * (value - low) / (high - low)
            if grid:
                value = round(value / grid) * grid
        return tuple(round(value, 8) if index == axis else coordinate
                     for index, coordinate in enumerate(position))

    ports = set(layout.grounds) | set(layout.boundary.values())
    ports.update(at for _, at, *_ in layout.connectors)
    if len({point(at) for at in ports}) != len(ports):
        # Grid rounding must not merge separately placed ports or overwrite
        # their coordinate-keyed captions and ground orientations.
        return None

    symbols = []
    for part, a, b, top in layout.symbols:
        # Stored endpoints precede rotation; translate them together to retain
        # the symbol's size and every pin's offset from its physical center.
        shift = amount if (a[axis] + b[axis]) / 2 >= high else 0
        def moved(position):
            return tuple(round(value - shift, 8) if index == axis else value
                         for index, value in enumerate(position))
        symbols.append((part, moved(a), moved(b), top))
    wires = {}
    for net, edges in layout.wires.items():
        moved_edges = [(point(a), point(b)) for a, b in edges]
        wires[net] = [(a, b) for a, b in moved_edges if a != b]
    result = replace(
        layout, symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, wires=wires,
        junctions={net: list(map(point, dots)) for net, dots in layout.junctions.items()},
        labels={net: point(at) for net, at in layout.labels.items()},
        grounds=list(map(point, layout.grounds)),
        ground_names={point(at): net for at, net in layout.ground_names.items()},
        ground_rotations={point(at): angle for at, angle in layout.ground_rotations.items()},
        connectors=[(net, point(at), *caption) for net, at, *caption in layout.connectors],
        boundary={net: point(at) for net, at in layout.boundary.items()},
        label_offsets={(key[0], *point(key[1:])) if key[0] in {'ground', 'connector'} else key: offset
                       for key, offset in layout.label_offsets.items()},
        crossings={},
    )
    mark_crossings(result)
    return result


def _occupied_intervals(drawing, axis, captions):
    for symbol in drawing.symbols:
        part, start, end, _ = symbol
        turns = drawing.rotations.get(part.name, 0)
        a, b = symbol_bounds(symbol, turns)
        low, high = sorted((a[axis], b[axis]))
        if captions and part.kind != "H":
            x, y = ((v + w) / 2 for v, w in zip(start, end))
            lines = captions[part.name]
            name_width = len(lines[0]) if len(lines[0]) <= 24 else 6
            width = .18 * max(name_width, *(len(line) for line in lines[1:]), 0)
            height = .36 * len(lines)
            axis_of_symbol = wire_axis(tuple(rotate_point(p, start, end, turns) for p in (start, end)))
            if axis == 0:
                caption_low, caption_high = (x - .62 - width, x) if axis_of_symbol == 1 else (x - width / 2, x + width / 2)
            else:
                caption_low, caption_high = (y - height / 2, y + height / 2) if axis_of_symbol == 1 else (y, y + .65 + height)
            low, high = min(low, caption_low), max(high, caption_high)
        yield low, high


# Close empty space between groups without resizing parts or changing their relative order.
def compact_spacing(circuit, drawing, *, budget=36, grid=.25, grid_safe=False, reserve_captions=False):
    """Close empty rows/columns together without resizing or reordering parts."""
    if len(drawing.symbols) < 2:
        return drawing
    captions = component_labels(circuit) if reserve_captions else None
    for axis in (0, 1):
        intervals = sorted(_occupied_intervals(drawing, axis, captions))
        gaps = []
        end = intervals[0][1]
        for low, high in intervals[1:]:
            if low - end > 1 + grid:
                gaps.append((end, low))
            end = max(end, high)
        attempts = 0
        # Closing a later gap leaves all earlier gap coordinates unchanged.
        for low, high in reversed(gaps):
            maximum = math.floor((high - low - 1) / grid) * grid
            amounts = dict.fromkeys(math.floor(maximum * fraction / grid) * grid for fraction in (1, .5, .25))
            for amount in amounts:
                if amount < grid or attempts >= budget // 2:
                    break
                attempts += 1
                trial = _close_gap(drawing, axis, low, high, amount, drawing.pin_grid)
                if trial is not None and improves_placement(circuit, drawing, trial, grid=drawing.pin_grid, compact=True):
                    if grid_safe and not trial.pin_grid and align_layout(circuit, trial) is None:
                        continue
                    drawing = trial
                    break
    return drawing


def reposition(circuit, drawing, *, budget=36, grid=0.25, fixed_parts=()):
    """Try a bounded set of local moves, keeping only shorter valid drawings."""
    names = sorted((symbol for symbol in drawing.symbols if symbol[0].name not in fixed_parts), key=lambda s: -sum(
        math.dist(a, b) for net in set(s[0].terminal_nets)
        for a, b in drawing.wires.get(net, ())))
    attempts = 0
    for original_symbol in names:
        symbol = next(s for s in drawing.symbols if s[0].name == original_symbol[0].name)
        best = drawing
        for delta in _move_candidates(drawing, symbol, grid):
            if attempts >= budget:
                return best
            attempts += 1
            trial = move_symbol(circuit, drawing, symbol[0].name, delta)
            if trial is None:
                continue
            trial = straighten(circuit, trial, budget=8, simplify=True)
            moved = next(s for s in trial.symbols if s[0].name == symbol[0].name)
            if improves_placement(circuit, best, trial, moved=moved, grid=grid):
                best = trial
        drawing = best
    return drawing


def _align_stacks(circuit, drawing, *, budget=8, grid=0.25):
    trial = min(stacked_alignment_candidates(circuit, drawing, budget=budget, grid=grid),
                key=lambda candidate: wiring_cost(candidate[0]), default=None)
    return trial if trial is not None else (drawing, frozenset())


def refine_automatic_layout(circuit, drawing):
    """Try short, grid-safe placement changes on a freshly routed drawing."""
    size = len(drawing.symbols)
    grid = drawing.pin_grid
    if not grid:
        return drawing
    stack_parts = frozenset()
    if size <= 64:
        drawing, stack_parts = _align_stacks(circuit, drawing, budget=3, grid=grid)
        drawing = align_passive_tees(circuit, drawing, budget=4, fixed_parts=stack_parts)
    drawing = shorten_symbol_leads(circuit, drawing)
    if size > 48:
        return drawing

    if not drawing.boundary:
        drawing = rotate_detoured_passives(circuit, drawing, grid=grid, fixed_parts=stack_parts)
    drawing = straighten(circuit, drawing, budget=12, simplify=True)
    drawing = reattach_tee_runs(circuit, drawing, budget=8)
    drawing, aligned_parts = _align_stacks(circuit, drawing, budget=3, grid=grid)
    stack_parts |= aligned_parts

    candidate = compact_spacing(
        circuit, drawing, budget=8 if size <= 32 else 12, grid=grid, reserve_captions=True,
    )
    if 8 < size <= 32:
        candidate = reposition(circuit, candidate, budget=8, grid=grid, fixed_parts=stack_parts)
        if not candidate.boundary and candidate.uses_packages:
            candidate = rotate_detoured_passives(
                circuit, candidate, budget=12, grid=grid, fixed_parts=stack_parts,
            )
    return candidate if crossing_count(candidate) <= crossing_count(drawing) else drawing


def _reorient_passives(circuit, drawing, budget, *, move_parts=False, fixed_parts=()):
    """Try the most heavily wired passive components first."""
    def connected_length(symbol):
        return sum(math.dist(a, b) for net in symbol[0].terminal_nets for a, b in drawing.wires.get(net, ()))

    candidates = (symbol for symbol in drawing.symbols
                  if symbol[0].name not in fixed_parts and symbol[0].kind in {"R", "C", "L", "X", "K"})
    rotations = (2, 1, 3) if move_parts else (2,)
    for symbol in sorted(candidates, key=lambda symbol: -connected_length(symbol))[:budget]:
        best = drawing
        for turns in rotations:
            trial = rotate_passive(circuit, drawing, symbol, turns)
            if trial is None:
                trial = move_symbol(circuit, drawing, symbol[0].name, (0, 0), rotate=turns)
            if trial is None:
                continue
            trial = straighten(circuit, trial, budget=8, simplify=move_parts)
            if improves_placement(circuit, best, trial, moved=symbol if turns % 2 else None):
                best = trial
        drawing = best
    return drawing


def optimize_layout(circuit, original, *, flip_budget=None, move_parts=False, grid=0.25):
    """Run bounded wiring edits that the viewer can commit as a single undo step."""
    if not move_parts:
        budget = 4 if flip_budget is None else flip_budget
        return _reorient_passives(circuit, straighten(circuit, original), budget)

    drawing = shorten_symbol_leads(circuit, original, overlaps_only=True)
    drawing = straighten(circuit, drawing, simplify=True)
    drawing, stack_parts = _align_stacks(circuit, drawing, grid=grid)
    protected_nets = {net for part, *_ in drawing.symbols if part.name in stack_parts for net in part.terminal_nets}
    drawing = _share_trunks(circuit, drawing, protected_nets=protected_nets)
    compacted = compact_spacing(circuit, drawing, grid=grid, reserve_captions=True)
    compact_enough = len(drawing.symbols) > 12 and layout_span(compacted) < .9 * layout_span(drawing)
    move_budget = 8 if compact_enough else 36
    if flip_budget is None:
        flip_budget = max(4, min(8, len(compacted.symbols) // 3))

    drawing = _reorient_passives(circuit, compacted, flip_budget, move_parts=True, fixed_parts=stack_parts)
    drawing = reposition(circuit, drawing, budget=move_budget, grid=grid, fixed_parts=stack_parts)
    drawing = align_passive_tees(circuit, drawing, fixed_parts=stack_parts)
    drawing = rotate_detoured_passives(circuit, drawing, grid=grid, fixed_parts=stack_parts)
    drawing = _align_stacks(circuit, drawing, grid=grid)[0]
    return reattach_tee_runs(circuit, drawing)
