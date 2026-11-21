"""Shorten the built-in leads around fixed-size symbol bodies."""

from collections import Counter, defaultdict
from math import floor

from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.geometry import rotate_point, routed_pins, wire_axis
from circuitlab.layout.wire_edit import clean_point, replace_route, route_anchors, route_trial, terminal_run, wire_graph


# The viewer's fixed glyph bodies stay unchanged. These terminal spans leave
# room for both the body and a short lead, including the switch's contacts.
TERMINAL_SPANS = {
    "R": 2, "C": .75, "L": 2, "X": 2, "Z": 2,
    "V": 1.5, "I": 1.5, "K": 2, "O": 2.5,
}


def _shorter_symbol(symbol, grid, limit=None):
    part, a, b, top = symbol
    if part.kind not in TERMINAL_SPANS or (axis := wire_axis((a, b))) is None:
        return symbol
    half = abs(a[axis] - b[axis]) / 2
    available = half - TERMINAL_SPANS[part.kind] / 2
    if limit is not None:
        available = min(available, limit)
    trim = floor((available + 1e-8) / grid) * grid
    if trim <= 0:
        return symbol
    shift = trim if a[axis] < b[axis] else -trim
    first, last = list(a), list(b)
    first[axis] += shift
    last[axis] -= shift
    return part, clean_point(first), clean_point(last), top


def _shift(point, old, new):
    return clean_point((point[0] + new[0] - old[0], point[1] + new[1] - old[1]))


def _bend_index(run, axis):
    return next((i for i, edge in enumerate(zip(run, run[1:])) if wire_axis(edge) != axis), len(run) - 1)


def _terminal_contacts(drawing):
    pins = [(net, clean_point(at)) for net, at in drawing.pins()]
    fixed = {(net, clean_point(at)) for net, at in drawing.boundary.items()}
    fixed.update((net, clean_point(at)) for net, at, *_ in drawing.connectors)
    return pins, Counter(pins), fixed


def _shorten_overlapping_symbols(drawing, grid):
    """Find terminal runs drawn inward along their own symbol axis."""
    pins, counts, fixed = _terminal_contacts(drawing)
    fixed.update((drawing.ground_names.get(at, "0"), clean_point(at)) for at in drawing.grounds)
    routes, overlaps = {}, {}
    symbols = []
    for symbol in drawing.symbols:
        part, a, b, _ = symbol
        if _shorter_symbol(symbol, grid) == symbol:
            symbols.append(symbol)
            continue
        turns = drawing.rotations.get(part.name, 0)
        first, last = (clean_point(rotate_point(pos, a, b, turns)) for pos in (a, b))
        axis = wire_axis((first, last))
        middle = (first[axis] + last[axis]) / 2
        limits = []
        for net, pin in routed_pins(symbol, turns, drawing.pin_grid):
            start = clean_point(pin)
            if start[axis] not in (first[axis], last[axis]):
                continue
            if counts[net, start] != 1 or (net, start) in fixed:
                continue
            if net not in routes:
                anchors = route_anchors(drawing, net, pins)
                routes[net] = anchors, wire_graph(drawing.wires.get(net, ()), anchors)
            anchors, graph = routes[net]
            run = terminal_run(graph, start, anchors - {start})
            stop = _bend_index(run, axis)
            direction = 1 if start[axis] < middle else -1
            distance = direction * (run[stop][axis] - start[axis])
            if distance > 1e-8:
                overlaps[net, start] = distance
                limits.append(distance)
        # Moving both endpoints preserves the center of the drawn glyph.
        # A non-overlapping contact retains its original route.
        symbols.append(_shorter_symbol(symbol, grid, min(limits)) if limits else symbol)
    return symbols, overlaps


def _retarget_terminal_run(net, before, after, graph, anchors, counts, fixed, grounds, pull_bends):
    """Return old and new route points for this endpoint's local geometry."""
    if counts[net, before] != 1 or (net, before) in fixed:
        return (), (before, after)
    run = terminal_run(graph, before, anchors - {before})
    end = run[-1]
    if (end in grounds and len(run) <= 2 and counts[net, end] == int(end == before)
            and (net, end) not in fixed and len(graph.get(end, ())) <= 1):
        # A ground on this branch only can follow the terminal without
        # leaving the removed part of the symbol drawn as an ordinary wire.
        grounds[end] = _shift(end, before, after)
        return run, tuple(_shift(at, before, after) for at in run)
    if before not in graph and len(anchors) == 1:
        return (), ()
    if len(run) < 2 or not pull_bends:
        return (), (before, after)
    first = wire_axis((before, run[1]))
    if first is not None and first != wire_axis((before, after)):
        # Pull the first perpendicular run in with the terminal. Its far
        # corner still connects to the original route along the same axis.
        stop = _bend_index(run, first)
        lead = run[:stop + 1]
        return lead, (*(_shift(at, before, after) for at in lead), lead[-1])
    if any(segments_intersect((after, after), edge) for edge in zip(run, run[1:])):
        # The original wire doubles back through the removed tail. The new
        # terminal already lands on it; route cleanup will trim the old stub.
        return (), ()
    return (), (before, after)


def _moved_pins(drawing, symbols):
    moves = defaultdict(list)
    for old, new in zip(drawing.symbols, symbols):
        if old == new:
            continue
        turns = drawing.rotations.get(old[0].name, 0)
        for (net, prior), (_, updated) in zip(
            routed_pins(old, turns, drawing.pin_grid), routed_pins(new, turns, drawing.pin_grid),
        ):
            if prior != updated:
                moves[net].append((prior, updated))
    return moves


def _retarget_wires(drawing, symbols, pull_bends, overlaps):
    pins, counts, fixed = _terminal_contacts(drawing)
    grounds_by_net = defaultdict(dict)
    for at in drawing.grounds:
        grounds_by_net[drawing.ground_names.get(at, "0")][clean_point(at)] = at

    moves = _moved_pins(drawing, symbols)
    routes = {}
    for net, updates in moves.items():
        anchors = route_anchors(drawing, net, pins)
        graph = wire_graph(drawing.wires.get(net, ()), anchors)
        removed, added = set(), []
        for old, new in updates:
            if overlaps is not None:
                old, new = clean_point(old), clean_point(new)
                travel = max(abs(a - b) for a, b in zip(old, new))
                if overlaps.get((net, old), 0) < travel - 1e-8:
                    added.append((old, new))
                continue
            before, after = _retarget_terminal_run(
                net, clean_point(old), clean_point(new), graph, anchors, counts,
                fixed, grounds_by_net[net], pull_bends,
            )
            removed.update(frozenset(edge) for edge in zip(before, before[1:]))
            added.extend((a, b) for a, b in zip(after, after[1:]) if a != b)
        routes[net] = [
            (a, b) for a, neighbors in graph.items() for b in neighbors
            if a < b and frozenset((a, b)) not in removed
        ] + added

    grounds = {at: grounds_by_net[drawing.ground_names.get(at, "0")][clean_point(at)]
               for at in drawing.grounds}
    if len(set(grounds.values())) != len(grounds):
        return None
    revised = route_trial(
        drawing, symbols=symbols, positions={p.name: a for p, a, *_ in symbols},
        grounds=list(grounds.values()),
        ground_names={grounds[at]: name for at, name in drawing.ground_names.items() if at in grounds},
        ground_rotations={grounds[at]: turns for at, turns in drawing.ground_rotations.items() if at in grounds},
        label_offsets={
            (key[0], *grounds.get(key[1:], key[1:])) if key[0] == "ground" else key: offset
            for key, offset in drawing.label_offsets.items()
        },
    )
    for net, edges in routes.items():
        replace_route(revised, net, edges, prune_cycles=overlaps is None)
        if overlaps is not None:
            for _, pin in moves[net]:
                pin = clean_point(pin)
                # Two wire branches and the newly attached part form a tee,
                # even after the wire-only representation loses its stub.
                if sum(pin in edge for edge in revised.wires.get(net, ())) >= 2:
                    dots = revised.junctions.setdefault(net, [])
                    if pin not in dots:
                        dots.append(pin)
        if net in drawing.labels:
            at = grounds.get(drawing.labels[net], drawing.labels[net])
            if any(segments_intersect((at, at), edge) for edge in revised.wires.get(net, ())):
                revised.labels[net] = at
    mark_crossings(revised)
    return revised


def shorten_symbol_leads(circuit, drawing, *, overlaps_only=False):
    """Reduce terminal spans, optionally limiting edits to doubled-back leads."""
    grid = drawing.pin_grid or .25
    if overlaps_only:
        symbols, overlaps = _shorten_overlapping_symbols(drawing, grid)
    else:
        symbols = [_shorter_symbol(symbol, grid) for symbol in drawing.symbols]
        overlaps = None
    if symbols == drawing.symbols:
        return drawing
    original_edges = {frozenset(edge) for edges in drawing.wires.values() for edge in edges}
    crossing_count = sum(map(len, drawing.crossings.values()))
    choices = [symbols]
    without_opamps = [old if old[0].kind == "O" else new for old, new in zip(drawing.symbols, symbols)]
    if without_opamps != symbols and without_opamps != drawing.symbols:
        choices.append(without_opamps)
    for shortened in choices:
        for pull_bends in ((False,) if overlaps_only else (True, False)):
            trial = _retarget_wires(drawing, shortened, pull_bends, overlaps)
            if trial is None or sum(map(len, trial.crossings.values())) > crossing_count:
                continue
            added = [edge for edges in trial.wires.values() for edge in edges if frozenset(edge) not in original_edges]
            if clear_of_bodies(added, trial) and preserves_nets(circuit, trial):
                return trial
    return drawing
