"""Simplify wires and optionally reposition parts, preserving electrical nets."""

import math
from dataclasses import replace
from itertools import product
from statistics import median_low

from circuitlab.layout.checks import mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.editing import _symbol_bounds, move_symbol
from circuitlab.layout.wire_edit import route_anchors, wire_graph, replace_route, terminal_run


def wire_length(layout):
    return sum(math.dist(a, b) for edges in layout.wires.values() for a, b in edges)


def clear_of_bodies(edges, layout):
    for symbol in layout.symbols:
        a, b = _symbol_bounds(symbol, layout.rotations.get(symbol[0].name, 0), body_only=True)
        left, right = sorted((a[0], b[0]))
        bottom, top = sorted((a[1], b[1]))
        borders = (((left,bottom),(right,bottom)), ((right,bottom),(right,top)),
                   ((right,top),(left,top)), ((left,top),(left,bottom)))
        for edge in edges:
            if (any(left < p[0] < right and bottom < p[1] < top for p in edge)
                    or any(segments_intersect(edge, border) for border in borders)):
                return False
    return True


def straighten(circuit, original, budget=24, *, simplify=False):
    drawing = original
    length = None
    attempts = 0
    for net in list(original.wires):
        anchors = route_anchors(drawing, net)
        graph = wire_graph(drawing.wires.get(net, ()), anchors)
        stops = anchors | {p for p, neighbors in graph.items() if len(neighbors) != 2}
        seen = set()
        runs = []
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
                    end = next(iter(choices))
                    edge = frozenset((run[-1], end))
                    if edge in seen:
                        break
                    seen.add(edge)
                    run.append(end)
                runs.append(run)
        runs.sort(key=lambda r: -(sum(math.dist(a,b) for a,b in zip(r,r[1:])) - math.dist(r[0],r[-1])))
        for run in runs:
            start, end = run[0], run[-1]
            run_edges = list(zip(run, run[1:]))
            old_length = sum(math.dist(a, b) for a, b in run_edges)
            minimum = abs(start[0]-end[0]) + abs(start[1]-end[1])
            # Stair-step routes can have optimal length but unnecessary bends.
            # Keep deliberately diagonal leads that are shorter than Manhattan.
            if old_length < minimum - 1e-8 or (old_length <= minimum + 1e-8 and (not simplify or len(run) <= 3)):
                continue
            remaining = None
            for corner in ((start[0],end[1]), (end[0],start[1])):
                if attempts >= budget:
                    return drawing
                attempts += 1
                edges = [(a,b) for a,b in ((start,corner),(corner,end)) if a != b]
                if not clear_of_bodies(edges, drawing):
                    continue
                if remaining is None:
                    current = wire_graph(drawing.wires.get(net, ()), anchors)
                    removed = set(map(frozenset, run_edges))
                    # An earlier shortcut may already have replaced part of this run.
                    if any(b not in current.get(a, ()) for a, b in run_edges):
                        break
                    remaining = [(a,b) for a,neighbors in current.items() for b in neighbors
                                 if a < b and frozenset((a,b)) not in removed]
                trial = replace(drawing, wires=dict(drawing.wires),
                                junctions={n: list(points) for n, points in drawing.junctions.items()},
                                labels=dict(drawing.labels), crossings={})
                replace_route(trial, net, remaining + edges)
                mark_crossings(trial)
                if length is None:
                    length = wire_length(drawing)
                trial_length = wire_length(trial)
                shorter = trial_length < length - 1e-8
                simpler = (simplify and trial_length <= length + 1e-8
                           and len(trial.wires.get(net, ())) < len(drawing.wires.get(net, ()))
                           and sum(map(len, trial.crossings.values())) <= sum(map(len, drawing.crossings.values())))
                if (shorter or simpler) and preserves_nets(circuit, trial):
                    drawing = trial
                    length = trial_length
                    break
    return drawing


def _rotate_passive(circuit, drawing, symbol, turns):
    """Reconnect both rotated terminals together, avoiding an invalid half-turn."""
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
        remaining = [(a,b) for a,neighbors in graph.items() for b in neighbors
                     if a < b and frozenset((a,b)) not in removed]
        target = run[-1]
        choices = []
        for corner in ((end[0], target[1]), (target[0], end[1])):
            edges = [(a,b) for a,b in ((end,corner),(corner,target)) if a != b]
            if clear_of_bodies(edges, base) and edges not in choices:
                choices.append(edges)
        if not choices:
            return None
        leads.append((net, remaining, choices))
    if len(leads) != 2 or leads[0][0] == leads[1][0]:
        return None
    best = None
    for pair in product(*(choices for _, _, choices in leads)):
        trial = replace(base, wires=dict(base.wires), labels=dict(base.labels),
                        junctions={n: list(points) for n, points in base.junctions.items()}, crossings={})
        for (net, remaining, _), edges in zip(leads, pair):
            replace_route(trial, net, remaining + edges)
        mark_crossings(trial)
        if preserves_nets(circuit, trial) and (best is None or wire_length(trial) < wire_length(best)):
            best = trial
    return best


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


def _improves(circuit, before, after, *, moved=None, grid=0):
    if wire_length(after) >= wire_length(before) - 1e-8:
        return False
    old_edges = {frozenset(edge) for edges in before.wires.values() for edge in edges}
    changed = [edge for edges in after.wires.values() for edge in edges
               if frozenset(edge) not in old_edges]
    grid = grid or before.pin_grid
    if grid and any(abs(v / grid - round(v / grid)) > 1e-6
                    for edge in changed for point in edge for v in point):
        return False
    if not clear_of_bodies(changed, after):
        return False
    if moved is not None:
        # Moving a body must not cover an existing wire or an unattached pin.
        body = replace(after, symbols=[moved])
        edges = [edge for edges in after.wires.values() for edge in edges]
        edges += [(pin, pin) for _, pin in after.pins()]
        edges += [(p, p) for p in after.grounds + list(after.boundary.values())]
        edges += [(p, p) for _, p, *_ in after.connectors]
        if not clear_of_bodies(edges, body):
            return False
    return preserves_nets(circuit, after)


def reposition(circuit, drawing, *, budget=36, grid=0.25):
    """Try a bounded set of local moves, keeping only shorter valid drawings."""
    names = sorted(drawing.symbols, key=lambda s: -sum(
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
            if _improves(circuit, best, trial, moved=moved, grid=grid):
                best = trial
        drawing = best
    return drawing


def optimize_layout(circuit, original, *, flip_budget=4, move_parts=False, grid=0.25):
    """Keep all edits optional, deterministic, bounded and electrically checked."""
    drawing = straighten(circuit, original, simplify=move_parts)
    # Try the most heavily wired passive components first. Sources and devices
    # with directional behavior are never flipped by this pass.
    candidates = sorted((s for s in drawing.symbols if s[0].kind in {'R','C','L','X'}),
        key=lambda s: -sum(math.dist(a,b) for net in s[0].terminal_nets for a,b in drawing.wires.get(net, ())))
    for symbol in candidates[:flip_budget]:
        best = drawing
        # The explicit Optimize action can also align vertical passives with
        # horizontal signal flow (and vice versa). Initial render stays bounded.
        for turns in ((2, 1, 3) if move_parts else (2,)):
            trial = _rotate_passive(circuit, drawing, symbol, turns)
            if trial is None:
                trial = move_symbol(circuit, drawing, symbol[0].name, (0,0), rotate=turns)
            if trial is None:
                continue
            trial = straighten(circuit, trial, budget=8, simplify=move_parts)
            if _improves(circuit, best, trial, moved=symbol if turns % 2 else None):
                best = trial
        drawing = best
    return reposition(circuit, drawing, grid=grid) if move_parts else drawing
