"""Place physical contacts on perfboard holes without changing electrical nets."""

from collections import defaultdict
from dataclasses import replace
from heapq import heappop, heappush
from itertools import chain
from math import ceil, floor

from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.geometry import (
    ChipPackage, Layout, add_wire, package_width, rotate_point, routed_pins, symbol_bounds,
)
from circuitlab.model.graph import CircuitError
from circuitlab.layout.wire_edit import clean_point, physical_wire_islands


TWO_TERMINAL = {"R", "C", "L", "X", "Z", "D", "V", "I", "K", "B"}


def excluded_parts(circuit):
    """Resolve exclusions against source identities and visible package names."""
    specification = circuit.perfboard
    selected = set()
    for name in specification.exclusions:
        key = name.casefold()
        matches = [part.name for part in circuit.components
                   if key in {part.name.casefold(), (part.label or "").casefold()}]
        if not matches:
            raise CircuitError(f'Perfboard exclusion "{name}" does not match a component or chip.',
                               specification.line, path=specification.path)
        selected.update(matches)
    return frozenset(selected)


def _bounds(symbol):
    first, last = symbol_bounds(symbol, 0, body_only=True)
    pins = [at for _, at in routed_pins(symbol, 0, 1)]
    return (min(first[0], last[0], *(p[0] for p in pins)),
            min(first[1], last[1], *(p[1] for p in pins)),
            max(first[0], last[0], *(p[0] for p in pins)),
            max(first[1], last[1], *(p[1] for p in pins)))


def _holes(symbol):
    left, bottom, right, top = _bounds(symbol)
    return {(x, y) for x in range(ceil(left - 1e-7), floor(right + 1e-7) + 1)
            for y in range(ceil(bottom - 1e-7), floor(top + 1e-7) + 1)}


def _physical(part):
    return replace(part, pin_pitch=1) if isinstance(part, ChipPackage) else part


def _symbol(part, x, top, *, vertical=False, first=None):
    """Use one hole per chip pin and at least three hole positions per passive."""
    if part.kind == "H":
        if part.base:
            return part, (x, top - 1), (x + 3, top - 1), "+"
        span = int(package_width(part))
        if isinstance(part, ChipPackage) and part.physical_pin_count > 32:
            y = top - span / 2
        else:
            count = part.physical_pin_count if isinstance(part, ChipPackage) else len(part.package_pins)
            y = top - (((count + 1) // 2) - 1) / 2
        return part, (x, y), (x + span, y), "+"
    span = 2 if part.kind in TWO_TERMINAL else 4
    a, b = ((x, top), (x, top - span)) if vertical else ((x, top), (x + span, top))
    if first is not None and part.p != first:
        a, b = b, a
    return part, a, b, "+"


def _fits(symbols, occupied, columns, rows):
    used = set()
    for symbol in symbols:
        holes = _holes(symbol)
        if (not holes or holes & (occupied | used)
                or any(x < 0 or x >= columns or y > 0 or y <= -rows for x, y in holes)):
            return None
        # Body edges may sit within the half-hole margin, but not beyond it.
        left, bottom, right, top = _bounds(symbol)
        if left < -.5 or right > columns - .5 or top > .5 or bottom < -rows + .5:
            return None
        used.update(holes)
    return used


def _pack(circuit, off_board, *, editable=False):
    spec = circuit.perfboard
    columns, rows = spec.column_count, spec.row_count
    included = [_physical(part) for part in circuit.components if part.name not in off_board]
    packages = [part for part in included if part.kind == "H"]
    packages.sort(key=lambda part: (int(part.name[8:]) if part.name.startswith("@method:") else part.order, part.name))
    symbols, occupied, overflow = [], set(), []
    cursor = 0
    for part in packages:
        span = int(package_width(part))
        left = (columns - span) // 2
        placed = None
        for top in range(cursor, rows):
            candidate = _symbol(part, left, -top)
            holes = _fits([candidate], occupied, columns, rows)
            if holes is not None:
                placed = candidate
                occupied.update(holes)
                symbols.append(candidate)
                cursor = -min(y for _, y in holes) + 2
                break
        if placed is None:
            if editable:
                overflow.append([part])
                continue
            raise CircuitError(f'Chip "{part.label or part.name}" does not fit on this perfboard. '
                               "Use a larger perf range or exclude it.", spec.line, path=spec.path)

    banks = defaultdict(list)
    for part in included:
        if part not in packages:
            banks[frozenset(part.terminal_nets) if part.kind in TWO_TERMINAL else (part.name,)].append(part)
    units = list(banks.values())
    # Parts with direct chip connections go first; their internal feed banks then
    # use those newly placed contacts rather than following off-board supplies.
    chip_nets = {net for part in packages for net in part.terminal_nets} - circuit.grounds
    units.sort(key=lambda unit: (not any(chip_nets.intersection(p.terminal_nets) for p in unit),
                                 min(part.order for part in unit), unit[0].name))
    for unit in units:
        targets = defaultdict(list)
        for symbol in symbols:
            for net, at in routed_pins(symbol, 0, 1):
                if net not in circuit.grounds:
                    targets[net].append(at)
        plans = []
        parallel = len(unit) > 1
        first = next((net for net in unit[0].terminal_nets if net not in circuit.grounds), unit[0].p)
        orientations = (True,) if parallel else (False, True) if unit[0].kind in TWO_TERMINAL else (False,)
        for vertical in orientations:
            for row in range(rows):
                for x in range(columns):
                    candidates = [_symbol(part, x + index if parallel else x, -row,
                                          vertical=vertical, first=first if parallel else None)
                                  for index, part in enumerate(unit)]
                    holes = _fits(candidates, occupied, columns, rows)
                    if holes is None:
                        continue
                    score = 0
                    for symbol in candidates:
                        for net, at in routed_pins(symbol, 0, 1):
                            if targets[net]:
                                score += min(abs(at[0] - p[0]) + abs(at[1] - p[1]) for p in targets[net])
                    if not any(targets[net] for part in unit for net in part.terminal_nets):
                        score = abs(x - (columns - 1) / 2) + row
                    plans.append(((score, row, x, not vertical), candidates, holes))
        if not plans:
            if editable:
                overflow.append(unit)
                continue
            names = ", ".join(part.label or part.name for part in unit)
            raise CircuitError(f'Parts {names} do not fit on this perfboard. '
                               "Use a larger perf range or exclude them.", spec.line, path=spec.path)
        _, placed, holes = min(plans, key=lambda item: item[0])
        occupied.update(holes)
        symbols.extend(placed)

    external = [part for part in circuit.components if part.name in off_board]
    cursor, row_bottom, row_height = 2, 3, 0
    for original in external:
        part = _physical(original)
        prototype = _symbol(part, 0, 0, vertical=part.kind in TWO_TERMINAL)
        left, bottom, right, top = _bounds(prototype)
        width, height = ceil(right - left), ceil(top - bottom)
        if cursor > 2 and cursor + width > columns - 1:
            cursor, row_bottom, row_height = 2, row_bottom + row_height + 3, 0
        x, y = ceil(cursor - left), ceil(row_bottom - bottom)
        symbols.append(_symbol(part, x, y, vertical=part.kind in TWO_TERMINAL))
        cursor += width + 3
        row_height = max(row_height, height)

    # Keep unplaced parts in a visible staging column. They still have physical
    # pin spacing and can be dragged onto free holes without recompiling them.
    top = 0
    for unit in overflow:
        first = next((net for net in unit[0].terminal_nets if net not in circuit.grounds), unit[0].p)
        placed = [_symbol(part, columns + 3 + index, top, vertical=part.kind in TWO_TERMINAL,
                          first=first if len(unit) > 1 else None) for index, part in enumerate(unit)]
        symbols.extend(placed)
        top = floor(min(_bounds(symbol)[1] for symbol in placed)) - 3
    return symbols


def _compress(path):
    result = []
    for point in path:
        if len(result) > 1 and ((result[-2][0] == result[-1][0] == point[0])
                                or (result[-2][1] == result[-1][1] == point[1])):
            result[-1] = point
        else:
            result.append(point)
    return result


def _routing_scale(points):
    return 4 if any(abs(value * 2 - round(value * 2)) > 1e-7 for at in points for value in at) else 2


def _routing_groups(reference, drawing):
    """Retain each explicitly split physical island through a geometry edit."""
    split = {net for net, *_ in reference.connectors}
    split.update(reference.ground_names.get(at, "0") for at in reference.grounds)
    islands = {net: physical_wire_islands(reference, net)[1] for net in split}
    groups = {}

    def group(net, before):
        island = islands[net].get(clean_point(before), clean_point(before)) if net in split else None
        return groups.setdefault((net, island), {"net": net, "points": [], "fixed": [], "moves": [], "edges": []})

    old_symbols = {symbol[0].name: symbol for symbol in reference.symbols}
    for symbol in drawing.symbols:
        name = symbol[0].name
        old = old_symbols.get(name, symbol)
        previous = routed_pins(old, reference.rotations.get(name, 0), reference.pin_grid)
        current = routed_pins(symbol, drawing.rotations.get(name, 0), drawing.pin_grid)
        for (net, before), (_, after) in zip(previous, current):
            owner = group(net, before)
            owner["points"].append(after)
            if clean_point(before) == clean_point(after):
                owner["fixed"].append(after)
            else:
                owner["moves"].append((before, after))
    for net, at in drawing.boundary.items():
        owner = group(net, reference.boundary.get(net, at))
        owner["points"].append(at)
        owner["fixed"].append(at)
    for index, (net, at, *_) in enumerate(drawing.connectors):
        before = reference.connectors[index][1] if index < len(reference.connectors) else at
        owner = group(net, before)
        owner["points"].append(at)
        owner["fixed"].append(at)
    for at in drawing.grounds:
        net = drawing.ground_names.get(at, "0")
        owner = group(net, at)
        owner["points"].append(at)
        owner["fixed"].append(at)
    for net, edges in reference.wires.items():
        for edge in edges:
            group(net, edge[0])["edges"].append(edge)
    return groups


def _grid_path(points, scale=2):
    """Expand an orthogonal route into half-hole coordinates."""
    result = []
    for a, b in zip(points, points[1:]):
        a, b = (tuple(round(value * scale) for value in point) for point in (a, b))
        if a[0] != b[0] and a[1] != b[1]:
            return None
        axis = int(a[0] == b[0])
        step = 1 if b[axis] >= a[axis] else -1
        for value in range(a[axis], b[axis] + step, step):
            point = (value, a[1]) if axis == 0 else (a[0], value)
            if not result or point != result[-1]:
                result.append(point)
    return result


def _island_barriers(groups, owner):
    return [edge for other, group in groups.items() if other != owner and other[0] == owner[0]
            for edge in chain(group["edges"], ((at, at) for at in group["points"]))]


def _route(circuit, drawing, boundary, *, editable=False, nets=None, groups=None, targets=None, extent=()):
    """Route on a half-hole grid, reserving bodies and crossing wires cleanly."""
    groups = _routing_groups(drawing, drawing) if groups is None else groups
    coordinates = [at for group in groups.values() for at in group["points"]]
    coordinates.extend(at for edges in drawing.wires.values() for edge in edges for at in edge)
    coordinates.extend(extent)
    scale = _routing_scale(coordinates)
    pins = {owner: [tuple(round(value * scale) for value in at) for at in group["points"]]
            for owner, group in groups.items()}
    selected = pins.keys() if targets is None else pins.keys() & targets
    if nets is not None:
        selected = [owner for owner in selected if owner[0] in nets]
    for owner in selected:
        group = groups[owner]
        for at, snapped in zip(group["points"], pins[owner]):
            snapped = snapped[0] / scale, snapped[1] / scale
            if at != snapped:
                # Older Split endpoints were not always on the routing grid.
                # Retain their exact coordinates with a short orthogonal stub.
                path = _compress(list(dict.fromkeys((at, (at[0], snapped[1]), snapped))))
                edges = list(zip(path, path[1:]))
                group["edges"].extend(edges)
                drawing.wires.setdefault(group["net"], []).extend(edges)
    spec = circuit.perfboard
    extent = [tuple(round(value * scale) for value in at) for at in coordinates]
    high = max([scale, *(at[1] + scale for at in extent)])
    limits = (min([-scale, *(at[0] - scale for at in extent)]),
              max([scale * spec.column_count - 1, *(at[0] + scale for at in extent)]),
              min([-scale * spec.row_count + 1, *(at[1] - scale for at in extent)]), high)
    bodies = []
    for symbol in drawing.symbols:
        a, b = symbol_bounds(symbol, drawing.rotations.get(symbol[0].name, 0), body_only=True)
        padding = .25 if symbol[0].kind == "H" else 0
        bodies.append(((min(a[0], b[0]) - padding, min(a[1], b[1]) - padding),
                       (max(a[0], b[0]) + padding, max(a[1], b[1]) + padding)))
    all_pins = {at: net for net, points in pins.items() for at in points}
    tracks, corners = defaultdict(dict), set()

    def reserve(net, points):
        compact = _compress(points)
        corners.update(compact)
        for a, b in zip(points, points[1:]):
            axis = int(a[0] == b[0])
            for at in (a, b):
                tracks[at].setdefault(net, set()).add(axis)

    # During a drag, other nets and the surviving portion of this net are fixed.
    # Reserve their actual tracks instead of routing the whole board again.
    for owner, group in groups.items():
        for edge in group["edges"]:
            path = _grid_path(edge, scale)
            if path:
                reserve(owner, path)

    def allowed(point, net):
        x, y = point
        if not (limits[0] <= x <= limits[1] and limits[2] <= y <= limits[3]):
            return False
        if point in all_pins and all_pins[point] != net:
            return False
        at = x / scale, y / scale
        return not any(min(a[0], b[0]) - 1e-8 <= at[0] <= max(a[0], b[0]) + 1e-8
                       and min(a[1], b[1]) - 1e-8 <= at[1] <= max(a[1], b[1]) + 1e-8
                       for a, b in bodies)

    def connect(start, goals, net):
        queue = [(0, 0, start, -1)]
        costs = {(start, -1): 0}
        previous = {}
        goal = None
        while queue:
            _, cost, point, axis = heappop(queue)
            state = point, axis
            if cost != costs[state]:
                continue
            if point in goals:
                goal = state
                break
            for dx, dy, direction in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
                other = point[0] + dx, point[1] + dy
                if not allowed(other, net):
                    continue
                crossing = tracks.get(point, {})
                next_crossing = tracks.get(other, {})
                # Matching labels share an electrical net, but their separated
                # physical branches must not be joined by a routing shortcut.
                if any(owner != net and owner[0] == net[0] for owner in next_crossing):
                    continue
                if (any(name != net for name in crossing)
                        and (axis != direction or point in corners or any(direction in axes for name, axes in crossing.items() if name != net))):
                    continue
                if any(name != net and direction in axes for name, axes in next_crossing.items()):
                    continue
                if other in corners and any(name != net for name in next_crossing):
                    continue
                new = cost + 1 + (axis != direction and axis != -1) * .6 + bool(next_crossing) * .15
                next_state = other, direction
                if new >= costs.get(next_state, float("inf")):
                    continue
                costs[next_state] = new
                previous[next_state] = state
                heuristic = min(abs(other[0] - p[0]) + abs(other[1] - p[1]) for p in goals)
                heappush(queue, (new + heuristic, new, other, direction))
        if goal is None:
            return None
        path = []
        while goal in previous:
            path.append(goal[0])
            goal = previous[goal]
        path.append(start)
        return list(reversed(path))

    ordered = sorted(selected, key=lambda owner: (owner[0] in circuit.grounds, -len(pins[owner]), repr(owner)))
    for owner in ordered:
        net = groups[owner]["net"]
        points = list(dict.fromkeys(pins[owner]))
        if len(points) < 2:
            continue
        tree = {point for edge in groups[owner]["edges"] for point in (_grid_path(edge, scale) or [])}
        if tree:
            points = [point for point in points if point not in tree]
        else:
            tree = {points.pop(0)}
        while points:
            start = min(points, key=lambda at: min(abs(at[0] - p[0]) + abs(at[1] - p[1]) for p in tree))
            points.remove(start)
            path = connect(start, tree, owner)
            if path is None:
                if editable:
                    root = min(tree, key=lambda at: (abs(at[0] - start[0]) + abs(at[1] - start[1]), at))
                    connection = _direct_connection(drawing, net, (start[0] / scale, start[1] / scale),
                                                    (root[0] / scale, root[1] / scale),
                                                    barriers=_island_barriers(groups, owner))
                    if connection is None:
                        tree.add(start)
                        continue
                    groups[owner]["edges"].extend(zip(connection, connection[1:]))
                    path = _grid_path(connection, scale)
                    tree.update(path)
                    reserve(owner, path)
                    continue
                raise CircuitError(f'Could not route perfboard net "{net}". '
                                   "Use a larger board or move parts to leave wire space.", spec.line, path=spec.path)
            tree.update(path)
            compact = _compress(path)
            reserve(owner, path)
            for a, b in zip(compact, compact[1:]):
                edge = (a[0] / scale, a[1] / scale), (b[0] / scale, b[1] / scale)
                add_wire(drawing, net, *edge)
                groups[owner]["edges"].append(edge)
    mark_crossings(drawing)
    return drawing


def _direct_connection(drawing, net, start, end, *, barriers=()):
    """Keep an obstructed connection orthogonal and local, with a warning."""
    foreign = [(a, b) for other, wires in drawing.wires.items() if other != net for a, b in wires]
    contacts = {at for other, at in drawing.pins() if other != net}
    contacts.update(at for other, at in drawing.boundary.items() if other != net)
    contacts.update(at for other, at, *_ in drawing.connectors if other != net)
    contacts.update(at for at in drawing.grounds if drawing.ground_names.get(at, "0") != net)
    contacts.update(at for edge in foreign for at in edge)
    candidates = [(start, (start[0], end[1]), end), (start, (end[0], start[1]), end)]
    for axis in (0, 1):
        values = {start[axis], end[axis]}
        for value in tuple(values):
            values.update(value + step / 2 for step in range(-8, 9))
        if barriers:
            values.update((min(at[axis] for edge in barriers for at in edge) - .5,
                           max(at[axis] for edge in barriers for at in edge) + .5))
        for value in sorted(values):
            candidates.append((start, (value, start[1]), (value, end[1]), end) if axis == 0
                              else (start, (start[0], value), (end[0], value), end))
    best = None
    for candidate in candidates:
        points = _compress(list(dict.fromkeys(candidate)))
        edges = list(zip(points, points[1:]))
        if any(segments_intersect(edge, barrier) for edge in edges for barrier in barriers):
            continue
        conflicts = (sum(segments_intersect((at, at), edge) for at in points for edge in foreign)
                     + sum(segments_intersect((at, at), edge) for at in contacts for edge in edges))
        score = (conflicts, not clear_of_bodies(edges, drawing),
                 sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in edges), len(edges))
        if best is None or score < best[0]:
            best = score, points
    if best is None:
        return None
    add_wire(drawing, net, *best[1])
    return best[1]


def _fallback_connections(drawing, groups=None):
    """Finish missing connections without replacing existing valid tracks."""
    groups = _routing_groups(drawing, drawing) if groups is None else groups
    for owner, group in sorted(groups.items(), key=lambda item: -len(item[1]["points"])):
        net = group["net"]
        points = list(dict.fromkeys(group["points"]))
        if not points:
            continue
        existing = group["edges"]
        connected = sorted({at for edge in existing for at in edge})
        if connected:
            points = [at for at in points if not any(segments_intersect((at, at), edge) for edge in existing)]
        else:
            connected = [points.pop(0)]
        while points:
            point, target = min(((point, target) for point in points for target in connected),
                                key=lambda pair: abs(pair[0][0] - pair[1][0]) + abs(pair[0][1] - pair[1][1]))
            route = _direct_connection(drawing, net, point, target, barriers=_island_barriers(groups, owner))
            if route is not None:
                group["edges"].extend(zip(route, route[1:]))
            connected.append(point)
            points.remove(point)
    mark_crossings(drawing)
    return drawing


def reroute_perfboard(circuit, drawing, *, editable=True, reference=None):
    """Reconnect current placements, including a board that still needs fixing."""
    groups = _routing_groups(reference or drawing, drawing)
    for group in groups.values():
        group["edges"] = []
    fresh = replace(drawing, wires={}, crossings={}, junctions={}, labels=dict(drawing.labels))
    try:
        extent = [at for edges in (reference or drawing).wires.values() for edge in edges for at in edge]
        routed = _route(circuit, fresh, tuple(drawing.boundary), editable=editable, groups=groups, extent=extent)
    except CircuitError:
        if not editable:
            raise
        return _fallback_connections(fresh, groups)
    return routed


def _trim_leaves(edges, terminals):
    """Remove the old free lead up to its first fixed pin or branch."""
    scale = _routing_scale([*terminals, *(at for edge in edges for at in edge)])
    paths = [_grid_path(edge, scale) for edge in edges]
    if any(path is None for path in paths):
        return list(edges)
    neighbors = defaultdict(set)
    for path in paths:
        for a, b in zip(path, path[1:]):
            neighbors[a].add(b)
            neighbors[b].add(a)
    fixed = {tuple(round(value * scale) for value in at) for at in terminals}
    pending = [point for point, adjacent in neighbors.items() if len(adjacent) == 1 and point not in fixed]
    while pending:
        point = pending.pop()
        if len(neighbors[point]) != 1 or point in fixed:
            continue
        other = neighbors[point].pop()
        neighbors[other].remove(point)
        if len(neighbors[other]) == 1 and other not in fixed:
            pending.append(other)
    retained = []
    for path in paths:
        run = []
        for a, b in zip(path, path[1:]):
            if b in neighbors[a]:
                if not run:
                    run.append(a)
                run.append(b)
            elif run:
                compact = _compress(run)
                retained.extend(zip(compact, compact[1:]))
                run = []
        if run:
            compact = _compress(run)
            retained.extend(zip(compact, compact[1:]))
    return [((a[0] / scale, a[1] / scale), (b[0] / scale, b[1] / scale)) for a, b in retained]


def _transform_parts(circuit, original, symbols, rotations):
    drawing = replace(original, symbols=symbols, positions={part.name: a for part, a, *_ in symbols},
                      rotations=rotations, wires=dict(original.wires), crossings={}, junctions={},
                      labels=dict(original.labels))
    groups = _routing_groups(original, drawing)
    targets = {owner for owner, group in groups.items() if group["moves"]}
    affected = {owner[0] for owner in targets}
    for owner in targets:
        group = groups[owner]
        if group["fixed"]:
            group["edges"] = _trim_leaves(group["edges"], group["fixed"])
        else:
            shifts = {clean_point((after[0] - before[0], after[1] - before[1]))
                      for before, after in group["moves"]}
            if len(shifts) == 1:
                dx, dy = shifts.pop()
                group["edges"] = [((a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy))
                                  for a, b in group["edges"]]
                foreign = [at for net, at in drawing.pins() if net != owner[0]]
                if (not clear_of_bodies(group["edges"], drawing)
                        or any(segments_intersect((at, at), edge) for at in foreign for edge in group["edges"])):
                    group["edges"] = []
            else:
                group["edges"] = []
    for net in affected:
        drawing.wires[net] = [edge for owner, group in groups.items() if owner[0] == net for edge in group["edges"]]
    return _route(circuit, drawing, tuple(drawing.boundary), editable=True, groups=groups, targets=targets)


def move_perfboard_parts(circuit, original, names, delta):
    """Move parts without changing rotations or unrelated board wiring."""
    selected = [symbol for symbol in original.symbols if symbol[0].name in names]
    if not selected or not any(delta):
        return original

    def shift(at):
        return at[0] + delta[0], at[1] + delta[1]

    symbols = [(part, shift(a), shift(b), top) if part.name in names else (part, a, b, top)
               for part, a, b, top in original.symbols]
    return _transform_parts(circuit, original, symbols, dict(original.rotations))


def rotate_perfboard_part(circuit, original, name, turns, *, route=True):
    """Rotate one part by relative quarter turns, keeping other routes fixed."""
    if not turns % 4:
        return original
    rotations = dict(original.rotations)
    rotation = (rotations.get(name, 0) + turns) % 4
    if rotation:
        rotations[name] = rotation
    else:
        rotations.pop(name, None)
    symbol = next(symbol for symbol in original.symbols if symbol[0].name == name)
    anchor = routed_pins(symbol, rotation, original.pin_grid)[0][1]
    dx, dy = (round(value) - value for value in anchor)
    symbols = [(part, (a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy), top)
               if part.name == name else (part, a, b, top) for part, a, b, top in original.symbols]
    if not route:
        return replace(original, symbols=symbols, rotations=rotations,
                       positions={part.name: a for part, a, *_ in symbols})
    return _transform_parts(circuit, original, symbols, rotations)


def resize_perfboard_part(circuit, original, name, holes, *, route=True):
    """Set endpoint-inclusive hole length while keeping the first contact fixed."""
    specification = circuit.perfboard

    def invalid(message):
        raise CircuitError(message, specification.line if specification else None,
                           path=specification.path if specification else None)

    if specification is None:
        invalid("Adjust length is available for parts placed on a perfboard.")
    if not isinstance(holes, int) or isinstance(holes, bool) or not 3 <= holes <= 256:
        invalid("Part length must be an integer from 3 to 256 hole positions, including both contacts.")
    symbol = next((symbol for symbol in original.symbols if symbol[0].name == name), None)
    if symbol is None:
        invalid(f'Part "{name}" is not in this perfboard layout.')
    if symbol[0].kind not in TWO_TERMINAL:
        invalid("Adjust length is available for two-terminal parts such as resistors and capacitors.")
    turns = original.rotations.get(name, 0)
    (_, first), (_, second) = routed_pins(symbol, turns, original.pin_grid)
    dx, dy = second[0] - first[0], second[1] - first[1]
    if (dx and dy) or not (dx or dy):
        invalid("Keep the part contacts on a horizontal or vertical axis before adjusting length.")
    span = abs(dx) + abs(dy)
    if abs(span - (holes - 1)) < 1e-7:
        return original
    end = clean_point((first[0] + dx / span * (holes - 1), first[1] + dy / span * (holes - 1)))
    # Stored endpoints precede the rotation. Invert around the new midpoint so
    # a changed pivot cannot pull the first physical contact off its hole.
    a, b = (rotate_point(at, first, end, -turns) for at in (first, end))
    resized = symbol[0], a, b, symbol[3]
    symbols = [resized if current[0].name == name else current for current in original.symbols]
    if not route:
        return replace(original, symbols=symbols, positions={part.name: a for part, a, *_ in symbols})
    return _transform_parts(circuit, original, symbols, dict(original.rotations))


def _transform_selection(circuit, original, names, transform, *, route):
    selected = set(names)
    if not selected:
        return original
    missing = selected - {symbol[0].name for symbol in original.symbols}
    if missing:
        specification = circuit.perfboard
        raise CircuitError(f'Part "{sorted(missing)[0]}" is not in this perfboard layout.',
                           specification.line if specification else None,
                           path=specification.path if specification else None)
    drawing = original
    for symbol in original.symbols:
        if symbol[0].name in selected:
            drawing = transform(drawing, symbol[0].name)
    if drawing is original or not route:
        return drawing
    return _transform_parts(circuit, original, drawing.symbols, dict(drawing.rotations))


def resize_perfboard_parts(circuit, original, names, holes, *, route=True):
    """Resize a selection atomically, reconnecting its shared buses once."""
    return _transform_selection(circuit, original, names,
                                lambda drawing, name: resize_perfboard_part(circuit, drawing, name, holes, route=False),
                                route=route)


def rotate_perfboard_parts(circuit, original, names, turns, *, route=True):
    """Rotate each selected part about its own pivot, then reconnect once."""
    return _transform_selection(circuit, original, names,
                                lambda drawing, name: rotate_perfboard_part(circuit, drawing, name, turns, route=False),
                                route=route)


def valid_perfboard_layout(circuit, drawing, *, wiring=True):
    """Check the physical hole and footprint contract for generated or saved views."""
    if circuit.perfboard is None:
        return True
    spec = circuit.perfboard
    off_board = excluded_parts(circuit)
    used, occupied = set(), set()
    for symbol in drawing.symbols:
        part = symbol[0]
        turns = drawing.rotations.get(part.name, 0)
        contacts = [at for _, at in routed_pins(symbol, turns, drawing.pin_grid)]
        if part.name in off_board:
            lo, hi = symbol_bounds(symbol, turns, body_only=True)
            left, right = min(lo[0], hi[0], *(p[0] for p in contacts)), max(lo[0], hi[0], *(p[0] for p in contacts))
            bottom, top = min(lo[1], hi[1], *(p[1] for p in contacts)), max(lo[1], hi[1], *(p[1] for p in contacts))
            if not (right < -.5 or left > spec.column_count - .5 or top < -spec.row_count + .5 or bottom > .5):
                return False
            continue
        if any(abs(value - round(value)) > 1e-7 for at in contacts for value in at):
            return False
        if any(x < 0 or x >= spec.column_count or y > 0 or y <= -spec.row_count for x, y in contacts):
            return False
        if len(set(contacts)) != len(contacts) or used.intersection(contacts):
            return False
        used.update(contacts)
        if part.kind in TWO_TERMINAL:
            a, b = contacts
            if a[0] != b[0] and a[1] != b[1] or abs(a[0] - b[0]) + abs(a[1] - b[1]) < 2:
                return False
        # Rotations preserve the occupancy rectangle, recomputed in world space.
        first, last = symbol_bounds(symbol, turns, body_only=True)
        left, bottom = min(first[0], last[0], *(p[0] for p in contacts)), min(first[1], last[1], *(p[1] for p in contacts))
        right, top = max(first[0], last[0], *(p[0] for p in contacts)), max(first[1], last[1], *(p[1] for p in contacts))
        holes = {(x, y) for x in range(ceil(left - 1e-7), floor(right + 1e-7) + 1)
                 for y in range(ceil(bottom - 1e-7), floor(top + 1e-7) + 1)}
        if holes & occupied or left < -.5 or right > spec.column_count - .5 or top > .5 or bottom < -spec.row_count + .5:
            return False
        occupied.update(holes)
    if not wiring or not getattr(drawing, "wires_visible", True):
        return True
    edges = [edge for route in drawing.wires.values() for edge in route]
    return preserves_nets(circuit, drawing) and clear_of_bodies(edges, drawing)


def perfboard_issues(circuit, drawing):
    """Recompute recoverable placement warnings after rendering or editing."""
    if circuit.perfboard is None:
        return ()
    spec = circuit.perfboard
    off_board = excluded_parts(circuit)
    outside = []
    for symbol in drawing.symbols:
        if symbol[0].name in off_board:
            continue
        contacts = [at for _, at in routed_pins(symbol, drawing.rotations.get(symbol[0].name, 0), drawing.pin_grid)]
        if any(x < 0 or x >= spec.column_count or y > 0 or y <= -spec.row_count for x, y in contacts):
            outside.append(symbol[0].label or symbol[0].name)
    issues = []
    if outside:
        issues.append(CircuitError(
            f'Parts outside the perfboard: {", ".join(outside)}. '
            "Move them onto free holes, use a larger perf range, or exclude external parts. "
            "The whole circuit remains visible and editable.", spec.line, "PerfboardFit", path=spec.path,
        ))
    elif not valid_perfboard_layout(circuit, drawing, wiring=False):
        issues.append(CircuitError(
            "The perfboard placement still needs adjustment. Keep pins on distinct holes, "
            "leave at least three hole positions per two-terminal part, and move overlapping parts apart.",
            spec.line, "PerfboardFit", path=spec.path,
        ))
    edges = [edge for route in drawing.wires.values() for edge in route]
    if getattr(drawing, "wires_visible", True) and (
            not preserves_nets(circuit, drawing) or not clear_of_bodies(edges, drawing)):
        issues.append(CircuitError(
            "Some perfboard connections are obstructed. The circuit is still shown; "
            "move parts to leave wire space or use a larger board, then optimize wiring.",
            spec.line, "PerfboardRouting", path=spec.path,
        ))
    return tuple(issues)


def perfboard_layout(circuit, labels=None, boundary=(), *, editable=False):
    """Pack a board at one schematic unit per 2.54 mm hole pitch and route it."""
    off_board = excluded_parts(circuit)
    symbols = _pack(circuit, off_board, editable=editable)
    drawing = Layout(symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=1)
    drawing = reroute_perfboard(circuit, drawing, editable=editable)
    if editable:
        return drawing
    edges = [edge for route in drawing.wires.values() for edge in route]
    if not valid_perfboard_layout(circuit, drawing) or not clear_of_bodies(edges, drawing):
        specification = circuit.perfboard
        raise CircuitError("Could not fit this circuit to the perfboard without overlapping pins or parts. "
                           "Use a larger perf range or exclude external parts.", specification.line, path=specification.path)
    return drawing
