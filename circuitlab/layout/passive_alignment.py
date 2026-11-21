"""Align passive components with the branches they connect."""

import math
from collections import Counter
from dataclasses import replace
from itertools import product

from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.editing import boxes_overlap, move_symbol
from circuitlab.layout.geometry import routed_pins, symbol_bounds, wire_axis
from circuitlab.layout.route_quality import crossing_count, improves_placement, wire_length
from circuitlab.layout.wire_edit import (
    clean_point, graph_edges, replace_route, route_anchors, route_trial, terminal_run, wire_graph,
)


PASSIVE_KINDS = {"R", "C", "L", "X", "Z"}


def _pins(drawing, symbol):
    return routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)


def rotate_passive(circuit, drawing, symbol, turns):
    """Reconnect both terminals of a passive after rotating it."""
    name = symbol[0].name
    rotations = dict(drawing.rotations)
    rotations[name] = (rotations.get(name, 0) + turns) % 4
    base = replace(drawing, rotations=rotations)
    old_pins = list(drawing.pins())
    leads = []
    for (net, start), (_, end) in zip(old_pins, base.pins()):
        if start == end:
            continue
        anchors = route_anchors(drawing, net)
        graph = wire_graph(drawing.wires.get(net, ()), anchors)
        # A terminal shared with another component remains a fixed attachment.
        fixed = anchors - {start}
        if old_pins.count((net, start)) > 1 or start in drawing.boundary.values():
            fixed.add(start)
        run = terminal_run(graph, start, fixed)
        removed = {frozenset(edge) for edge in zip(run, run[1:])}
        remaining = [edge for edge in graph_edges(graph) if frozenset(edge) not in removed]
        target = run[-1]
        choices = []
        for corner in ((end[0], target[1]), (target[0], end[1])):
            edges = [(a, b) for a, b in ((end, corner), (corner, target)) if a != b]
            if clear_of_bodies(edges, base) and edges not in choices:
                choices.append(edges)
        if not choices:
            return None
        leads.append((net, remaining, choices))
    if len(leads) != 2 or leads[0][0] == leads[1][0]:
        return None
    best = None
    for pair in product(*(choices for _, _, choices in leads)):
        trial = route_trial(base)
        for (net, remaining, _), edges in zip(leads, pair):
            replace_route(trial, net, remaining + edges)
        mark_crossings(trial)
        if preserves_nets(circuit, trial) and (best is None or wire_length(trial) < wire_length(best)):
            best = trial
    return best


def _terminal_branches(drawing, symbol, pins):
    branches = []
    for net, pin in _pins(drawing, symbol):
        anchors = route_anchors(drawing, net, pins)
        graph = wire_graph(drawing.wires.get(net, ()), anchors)
        fixed = anchors if pins.count((net, pin)) > 1 else anchors - {pin}
        run = terminal_run(graph, pin, fixed | set(drawing.boundary.values()))
        cut = set(map(frozenset, zip(run, run[1:])))
        rest = [edge for edge in graph_edges(graph) if frozenset(edge) not in cut]
        branches.append((net, run, rest))
    return branches


def _dock_rotated_passive(drawing, symbol, branches, grid):
    """Place a passive between two overlapping stretches of parallel wire."""
    part, start, end, top = symbol
    turns = drawing.rotations.get(part.name, 0)
    previous_axis = wire_axis(tuple(pin for _, pin in _pins(drawing, symbol)))
    if previous_axis is None:
        return
    axis = 1 - previous_axis
    across = 1 - axis
    center = tuple((a + b) / 2 for a, b in zip(start, end))
    half = math.dist(start, end) / 2
    tracks = []
    for _, run, rest in branches:
        tap = run[-1]
        tracks.append([tuple(sorted((a[across], b[across]))) for a, b in rest
                       if tap in (a, b) and a[axis] == b[axis]] or [(tap[across], tap[across])])
    lanes = [run[-1][axis] for _, run, _ in branches]
    low, high = sorted(lanes)
    if high - low < 2 * half:
        return
    axis_center = max(low + half, min(high - half, center[axis]))
    options = set()
    for (a, b), (c, d) in product(*tracks):
        left, right = max(a, c), min(b, d)
        if left <= right:
            options.update((max(left, min(right, center[across])), left, right))
    for position in sorted(options, key=lambda value: (abs(value - center[across]), value)):
        target = [0, 0]
        target[across] = position
        target[axis] = axis_center
        delta = tuple(round((a - b) / grid) * grid for a, b in zip(target, center))
        moved = (part, *(clean_point(tuple(x + dx for x, dx in zip(at, delta))) for at in (start, end)), top)
        for change in (1, 3):
            angle = (turns + change) % 4
            pins = [pin for _, pin in routed_pins(moved, angle, drawing.pin_grid)]
            if any((at[axis] - axis_center) * (lane - axis_center) < 0
                   for at, lane in zip(pins, lanes)):
                continue
            if any(boxes_overlap(symbol_bounds(moved, angle, body_only=True),
                                 symbol_bounds(other, drawing.rotations.get(other[0].name, 0), body_only=True))
                   for other in drawing.symbols if other[0] != part):
                continue
            yield moved, angle, [
                (net, [*rest, (pin, tuple(lane if i == axis else value for i, value in enumerate(pin)))])
                for (net, _, rest), pin, lane in zip(branches, pins, lanes)
            ]


def _feedback_pairs(drawing):
    """Protect feedback branches whose existing orientation is intentional."""
    feedback = {frozenset((part.p, net)) for part, *_ in drawing.symbols if part.kind == "O"
                for net in (part.in_plus, part.in_minus)}
    parallel = Counter(frozenset((part.p, part.n)) for part, *_ in drawing.symbols if part.kind in PASSIVE_KINDS)
    for part, *_ in drawing.symbols:
        if part.kind == "H":
            contacts = set(part.terminal_nets)
            feedback.update(nets for nets, count in parallel.items() if count > 1 and nets <= contacts)
    return feedback


def _detoured_passives(circuit, drawing, pins, fixed_parts):
    feedback = _feedback_pairs(drawing)
    for symbol in drawing.symbols:
        part = symbol[0]
        if (part.kind not in PASSIVE_KINDS or part.name in fixed_parts
                or part.p == part.n or {part.p, part.n} & circuit.grounds
                or frozenset((part.p, part.n)) in feedback
                or any(("net", net) in drawing.label_offsets for net in (part.p, part.n))):
            continue
        branches = _terminal_branches(drawing, symbol, pins)
        if any(len(run) < 2 for _, run, _ in branches):
            continue
        axis = wire_axis(tuple(pin for _, pin in _pins(drawing, symbol)))
        sideways = axis is not None and all(wire_axis(run[:2]) == 1 - axis for _, run, _ in branches)
        if sideways or sum(len(run) for _, run, _ in branches) >= 6:
            length = sum(math.dist(a, b) for _, run, _ in branches for a, b in zip(run, run[1:]))
            yield length, symbol


def _elbow_moves(drawing, symbol, branches, grid):
    """Try a straight lead at one end when the tracks are too close to span."""
    _, a, b, _ = symbol
    center = tuple((x + y) / 2 for x, y in zip(a, b))
    axis = wire_axis(tuple(pin for _, pin in _pins(drawing, symbol)))
    if axis is None:
        return
    direction = 1 - axis
    target = sum(at[direction] for _, run, _ in branches for at in run[1:2] + run[-1:]) / 4
    for _, run, _ in branches:
        delta = [0, 0]
        delta[axis] = round((run[1][axis] - center[axis]) / grid) * grid
        delta[direction] = round((target - center[direction]) / grid) * grid
        for turns in (1, 3):
            yield delta, turns


def rotate_detoured_passives(circuit, drawing, *, budget=16, grid=.25, fixed_parts=()):
    """Rotate a two-terminal part onto its wires when the route gets cleaner."""
    pins = list(drawing.pins())
    candidates = _detoured_passives(circuit, drawing, pins, fixed_parts)
    for _, symbol in sorted(candidates, key=lambda entry: -entry[0]):
        part = symbol[0]
        branches = _terminal_branches(drawing, symbol, pins)
        if any(len(run) < 2 for _, run, _ in branches):
            continue
        best = drawing
        for moved, angle, routes in _dock_rotated_passive(drawing, symbol, branches, grid):
            if budget <= 0:
                return drawing
            budget -= 1
            trial = route_trial(
                drawing, symbols=[moved if s == symbol else s for s in drawing.symbols],
                positions={**drawing.positions, part.name: moved[1]},
                rotations={**drawing.rotations, part.name: angle},
            )
            for net, edges in routes:
                replace_route(trial, net, edges)
            mark_crossings(trial)
            if crossing_count(trial) <= crossing_count(best) and improves_placement(circuit, best, trial, moved=moved, grid=grid):
                best = trial
                break
        if best is drawing:
            for delta, turns in _elbow_moves(drawing, symbol, branches, grid):
                if budget <= 0:
                    return best
                budget -= 1
                trial = move_symbol(circuit, drawing, part.name, delta, rotate=turns)
                if trial is None or crossing_count(trial) > crossing_count(best):
                    continue
                moved = next(s for s in trial.symbols if s[0] == part)
                if improves_placement(circuit, best, trial, moved=moved, grid=grid):
                    best = trial
        drawing = best
        pins = list(drawing.pins())
    return drawing


def align_passive_tees(circuit, drawing, *, budget=8, fixed_parts=()):
    """Align a passive lead with a straight-through tee without lengthening the drawing."""
    drivers = {
        net for part in circuit.components if part.is_source or part.kind in {"O", "A"}
        for net in ((part.p, part.n) if part.is_source else (part.p,))
    } - circuit.grounds
    all_pins = [(net, clean_point(pos)) for net, pos in drawing.pins()]
    pin_counts = Counter(all_pins)
    for symbol in drawing.symbols:
        part = symbol[0]
        if part.name in fixed_parts or part.kind not in PASSIVE_KINDS or part.p == part.n:
            continue
        pins = [(net, clean_point(pos)) for net, pos in _pins(drawing, symbol)]
        axis = wire_axis(tuple(pos for _, pos in pins))
        if axis is None:
            continue
        across = 1 - axis
        span = abs(pins[0][1][axis] - pins[1][1][axis])
        for net, pin in sorted(pins, key=lambda entry: entry[0] not in drivers):
            if net in circuit.grounds or pin_counts[net, pin] != 1:
                continue
            anchors = route_anchors(drawing, net, all_pins)
            graph = wire_graph(drawing.wires.get(net, ()), anchors)
            run = terminal_run(graph, pin, anchors - {pin})
            junction = run[-1]
            delta = junction[across] - pin[across]
            if len(run) < 3 or len(graph.get(junction, ())) < 3 or not 0 < abs(delta) <= span:
                continue
            direction = pin[axis] - junction[axis]
            if not any(
                pos[across] == junction[across] and (pos[axis] - junction[axis]) * direction < 0
                for pos in graph[junction]
            ):
                continue
            if budget <= 0:
                return drawing
            budget -= 1
            shift = (0, delta) if axis == 0 else (delta, 0)
            trial = move_symbol(circuit, drawing, part.name, shift)
            if trial is None:
                continue
            end = clean_point(tuple(value + step for value, step in zip(pin, shift)))
            trial_graph = wire_graph(trial.wires.get(net, ()), {end, junction})
            aligned = terminal_run(trial_graph, end, {junction})
            if aligned[-1] != junction or any(wire_axis(edge) != axis for edge in zip(aligned, aligned[1:])):
                continue
            moved = next(candidate for candidate in trial.symbols if candidate[0].name == part.name)
            # An equal-cost shift may straighten the incoming signal, but may
            # not move a part back and forth between two driven nets.
            input_lead = net in drivers and not all(name in drivers for name, _ in pins)
            if improves_placement(circuit, drawing, trial, moved=moved, straightens_input=input_lead):
                for name, _ in pins:
                    label = drawing.labels.get(name)
                    if name not in trial.labels and label is not None and any(
                        segments_intersect((label, label), edge) for edge in trial.wires.get(name, ())
                    ):
                        trial.labels[name] = label
                drawing = trial
                all_pins = [(name, clean_point(pos)) for name, pos in drawing.pins()]
                pin_counts = Counter(all_pins)
                break
    return drawing
