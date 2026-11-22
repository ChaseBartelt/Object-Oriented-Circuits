"""Place parallel two-terminal devices side by side between shared rails."""

from collections import defaultdict
from itertools import product
from math import ceil, dist
from statistics import median

from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets
from circuitlab.layout.editing import boxes_overlap
from circuitlab.layout.geometry import along, routed_pins, symbol_bounds
from circuitlab.layout.lead_spacing import TERMINAL_SPANS
from circuitlab.layout.route_quality import wiring_cost
from circuitlab.layout.wire_edit import (
    clean_point, compact_wire, physical_wire_islands, replace_route,
    route_anchors, route_trial, wire_graph,
)


PASSIVES = {"R", "C", "L", "X", "Z", "D"}


def parallel_banks(drawing):
    banks = defaultdict(list)
    for symbol in drawing.symbols:
        part = symbol[0]
        if part.kind in PASSIVES and part.p != part.n:
            banks[frozenset((part.p, part.n))].append(symbol)
    return tuple(tuple(bank) for bank in banks.values() if len(bank) > 1)


def parallel_bank_names(subject):
    """Protect aligned banks from individual component placement cleanup."""
    if hasattr(subject, "symbols"):
        return frozenset(symbol[0].name for bank in parallel_banks(subject) for symbol in bank)
    banks = defaultdict(list)
    for part in subject.components:
        if part.kind in PASSIVES and part.p != part.n:
            banks[frozenset((part.p, part.n))].append(part.name)
    return frozenset(name for bank in banks.values() if len(bank) > 1 for name in bank)


def _aligned_bank(drawing, bank):
    rows = defaultdict(list)
    columns = set()
    centers, upper_nets = set(), set()
    for symbol in bank:
        first, last = routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)
        if first[1][0] != last[1][0]:
            return False
        columns.add(first[1][0])
        centers.add((first[1][1] + last[1][1]) / 2)
        upper_nets.add(max((first, last), key=lambda entry: entry[1][1])[0])
        for net, at in (first, last):
            rows[net].append(at[1])
    if len(columns) != len(bank) or len(centers) != 1 or len(upper_nets) != 1:
        return False
    for net in rows:
        _, islands = physical_wire_islands(drawing, net)
        contacts = [clean_point(at) for symbol in bank
                    for name, at in routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)
                    if name == net]
        if len({islands[at] for at in contacts}) != 1:
            return False
    left, right = min(columns), max(columns)
    for net, levels in rows.items():
        rail_rows = defaultdict(list)
        upper = net in upper_nets
        for a, b in drawing.wires.get(net, ()):
            if a[1] == b[1] and (a[1] >= max(levels) if upper else a[1] <= min(levels)):
                rail_rows[a[1]].append(tuple(sorted((a[0], b[0]))))
        for intervals in rail_rows.values():
            reach = left
            for start, end in sorted(intervals):
                if start <= reach:
                    reach = max(reach, end)
            if reach >= right:
                break
        else:
            return False
    return True


def _local_ground_marks(circuit, drawing, bank):
    """Collect ground marks belonging only to this bank's physical islands."""
    names = {symbol[0].name for symbol in bank}
    moving = [(net, clean_point(at)) for symbol in bank
              for net, at in routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)]
    stationary = [(net, clean_point(at)) for symbol in drawing.symbols if symbol[0].name not in names
                  for net, at in routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)]
    stationary.extend((net, clean_point(at)) for net, at in drawing.boundary.items())
    stationary.extend((net, clean_point(at)) for net, at, *_ in drawing.connectors)
    marks = {}
    for net in circuit.grounds.intersection(net for net, _ in moving):
        _, islands = physical_wire_islands(drawing, net)
        bank_islands = {islands[at] for name, at in moving if name == net}
        shared = {islands[at] for name, at in stationary if name == net}
        for at in drawing.grounds:
            if (drawing.ground_names.get(at, "0") == net
                    and islands[clean_point(at)] in bank_islands - shared):
                marks[at] = net
    return marks


def _routes_without_bank(drawing, bank, moving_grounds=()):
    names = {symbol[0].name for symbol in bank}
    moving = [(net, clean_point(at)) for symbol in bank
              for net, at in routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)]
    stationary = [(net, clean_point(at)) for symbol in drawing.symbols if symbol[0].name not in names
                  for net, at in routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)]
    routes = {}
    for net in {net for net, _ in moving}:
        fixed = route_anchors(drawing, net, stationary) - set(moving_grounds)
        physical = {at for name, at in stationary if name == net}
        original = wire_graph(drawing.wires.get(net, ()), route_anchors(drawing, net))
        protected = set(fixed)
        # Retain the outward lead at a fixed chip contact. Turning sideways
        # directly on a four-sided package can touch the neighboring pins.
        for at in physical:
            neighbors = original.get(at, ())
            if len(neighbors) == 1:
                end, = neighbors
                protected.add(clean_point(along(at, end, min(1, .75 / dist(at, end)))))
        rest, _ = compact_wire(drawing.wires.get(net, ()), protected, prune_cycles=True)
        _, before = physical_wire_islands(drawing, net)
        touched = {before[at] for name, at in moving if name == net}
        relevant = {at for at in fixed if before[at] in touched}
        graph = wire_graph(rest, fixed)
        islands, seen = [], set()
        for point in sorted(graph.keys() | relevant):
            if point in seen:
                continue
            pending, island = [point], set()
            while pending:
                at = pending.pop()
                if at not in island:
                    island.add(at)
                    pending.extend(graph.get(at, ()))
            seen.update(island)
            if island & relevant:
                islands.append(island - physical or island)
        routes[net] = rest, islands
    return routes


def _rail_routes(drawing, rail, islands, strategy):
    first, last = rail
    left, right = sorted((first[0], last[0]))
    row = first[1]
    edges = [rail] if first != last else []
    for island in islands:
        choices = []
        for at in sorted(island, key=lambda at: abs(at[1] - row) + max(left - at[0], at[0] - right, 0))[:4]:
            tap = min(right, max(left, at[0])), row
            corners = ((at[0], row), (tap[0], at[1]))
            side = (left - .75, right + .75)[strategy % 2]
            plans = ((at, corners[strategy % 2], tap),
                     (at, (side, at[1]), (side, row), tap))
            for plan in plans if strategy < 2 else reversed(plans):
                path = [(a, b) for a, b in zip(plan, plan[1:]) if a != b]
                if clear_of_bodies(path, drawing):
                    choices.append((sum(dist(a, b) for a, b in path), path))
        if not choices:
            return None
        edges.extend(min(choices, key=lambda option: option[0])[1])
    return edges


def _bank_trial(drawing, bank, upper_net, center, grid):
    names = {symbol[0].name for symbol in bank}
    spans = [ceil(TERMINAL_SPANS[symbol[0].kind] / (2 * grid)) * 2 * grid for symbol in bank]
    pitch = max(1.5, 6 * grid)
    half = max(spans) / 2
    left = center[0] - ceil((len(bank) - 1) * pitch / (2 * grid)) * grid
    symbols = {}
    for index, (old, span) in enumerate(zip(bank, spans)):
        part, _, _, top = old
        x, y = left + index * pitch, center[1]
        upper, lower = (x, y + span / 2), (x, y - span / 2)
        a, b = (upper, lower) if part.p == upper_net else (lower, upper)
        symbols[part.name] = part, clean_point(a), clean_point(b), top
    for symbol in symbols.values():
        bounds = symbol_bounds(symbol, 0, body_only=True)
        if any(boxes_overlap(bounds, symbol_bounds(other, drawing.rotations.get(other[0].name, 0), body_only=True))
               for other in drawing.symbols if other[0].name not in names):
            return None
    return route_trial(
        drawing,
        symbols=[symbols.get(symbol[0].name, symbol) for symbol in drawing.symbols],
        positions={**drawing.positions, **{name: symbol[1] for name, symbol in symbols.items()}},
        rotations={name: turns for name, turns in drawing.rotations.items() if name not in names},
    ), (left, left + (len(bank) - 1) * pitch), half


def align_parallel_banks(circuit, drawing, *, grid=.25):
    """Try checked, compact shared rails without changing electrical connections."""
    for bank in parallel_banks(drawing):
        if _aligned_bank(drawing, bank):
            continue
        pins = [tuple(routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid))
                for symbol in bank]
        first = pins[0]
        upper_net = next((net for net, _ in first if net not in circuit.grounds), first[0][0])
        if not any(net in circuit.grounds for net, _ in first):
            upper_net = max(first, key=lambda pin: pin[1][1])[0]
        lower_net, = {net for net, _ in first} - {upper_net}
        center = tuple(round(median((a[axis] + b[axis]) / 2 for _, a, b, _ in bank) / grid) * grid
                       for axis in (0, 1))
        moving_grounds = _local_ground_marks(circuit, drawing, bank)
        routes = _routes_without_bank(drawing, bank, moving_grounds)
        external = [at for _, islands in routes.values() for island in islands for at in island]
        centers = [center]
        if external:
            near = tuple(round(median(at[axis] for at in external) / grid) * grid for axis in (0, 1))
            centers.extend((near, (near[0], center[1]), (center[0], near[1])))
        best = None
        # Nearby positions let a bank clear a chip or an unrelated branch.
        placements = dict.fromkeys(clean_point((x + dx, y + dy)) for x, y in centers
                                  for dx, dy in ((0, 0), (-1.5, 0), (1.5, 0), (0, 1.5), (0, -1.5), (-3, 0), (3, 0)))
        for position in placements:
            proposal = _bank_trial(drawing, bank, upper_net,
                                   position, grid)
            if proposal is None:
                continue
            base, (left, right), half = proposal
            new_marks = {clean_point(((left + right) / 2, position[1] + (half if net == upper_net else -half))): net
                         for net in moving_grounds.values()}
            if moving_grounds:
                base = route_trial(
                    base, grounds=[at for at in base.grounds if at not in moving_grounds] + list(new_marks),
                    ground_names={**{at: net for at, net in base.ground_names.items() if at not in moving_grounds},
                                  **new_marks},
                    ground_rotations={at: turns for at, turns in base.ground_rotations.items() if at not in moving_grounds},
                )
            for first_strategy, second_strategy in product(range(4), repeat=2):
                trial = route_trial(base)
                for net, row, strategy in ((upper_net, position[1] + half, first_strategy),
                                           (lower_net, position[1] - half, second_strategy)):
                    rest, islands = routes[net]
                    added = _rail_routes(trial, ((left, row), (right, row)), islands, strategy)
                    if added is None:
                        break
                    names = {symbol[0].name for symbol in bank}
                    added.extend((at, (at[0], row)) for symbol in trial.symbols if symbol[0].name in names
                                 for name, at in routed_pins(symbol, 0, trial.pin_grid) if name == net and at[1] != row)
                    replace_route(trial, net, rest + added)
                    if net in drawing.labels:
                        trial.labels[net] = (left + right) / 2, row
                else:
                    mark_crossings(trial)
                    edges = [edge for route in trial.wires.values() for edge in route]
                    if (clear_of_bodies(edges, trial) and preserves_nets(circuit, trial)
                            and (best is None or wiring_cost(trial) < wiring_cost(best))):
                        best = trial
            if best is not None:
                break
        if best is not None:
            drawing = best
    return drawing
