"""Route fixed perfboard placements on the component and solder faces."""

from collections import defaultdict
from dataclasses import replace
from heapq import heappop, heappush

from circuitlab.layout.board_layers import layer_wires
from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets
from circuitlab.layout.geometry import routed_pins, symbol_bounds
from circuitlab.layout.perfboard import _compress, _routing_groups, _routing_scale
from circuitlab.layout.route_quality import crossing_count, wire_length, wiring_cost
from circuitlab.layout.wire_edit import clean_point, route_anchors, wire_graph


def _physical_owners(drawing, net):
    """Identify physical islands without joining labels or insulated crossings."""
    terminals = route_anchors(drawing, net)
    vias = {at for edge in drawing.wires.get(net, ()) for at in edge
            if all(abs(value - round(value)) < 1e-7 for value in at)}
    vias.update(at for edge in drawing.underside_wires.get(net, ()) for at in edge)
    faces = [wire_graph(layer_wires(drawing, underside=back).get(net, ()), terminals | vias)
             for back in (False, True)]
    graph = defaultdict(set)
    for side, face in enumerate(faces):
        for at in terminals:
            face.setdefault(at, set())
        for at, others in face.items():
            graph[side, at].update((side, other) for other in others)
    for at in terminals | vias:
        if at in faces[0] and at in faces[1]:
            graph[0, at].add((1, at))
            graph[1, at].add((0, at))
    islands = {}
    for start in sorted(graph):
        if start in islands:
            continue
        owner, pending = len(islands), [start]
        while pending:
            at = pending.pop()
            if at not in islands:
                islands[at] = owner
                pending.extend(graph[at])
    return {at: islands[0, at] for at in terminals}


def _groups(reference, drawing):
    groups = _routing_groups(reference, drawing)
    if not reference.underside_wires:
        return groups
    split = {net for net, *_ in reference.connectors}
    split.update(reference.ground_names.get(at, "0") for at in reference.grounds)
    owners = {net: _physical_owners(reference, net) for net in split}
    result = {key: group for key, group in groups.items() if key[0] not in split}

    def add(net, before, after):
        if net in split:
            key = net, owners[net].get(clean_point(before), clean_point(before))
            result.setdefault(key, {"points": [], "net": net})["points"].append(after)

    previous = {symbol[0].name: symbol for symbol in reference.symbols}
    for symbol in drawing.symbols:
        name = symbol[0].name
        old = previous.get(name, symbol)
        old_pins = routed_pins(old, reference.rotations.get(name, 0), reference.pin_grid)
        new_pins = routed_pins(symbol, drawing.rotations.get(name, 0), drawing.pin_grid)
        for (net, before), (_, after) in zip(old_pins, new_pins):
            add(net, before, after)
    for net, at in drawing.boundary.items():
        add(net, reference.boundary.get(net, at), at)
    for index, (net, at, *_) in enumerate(drawing.connectors):
        add(net, reference.connectors[index][1] if index < len(reference.connectors) else at, at)
    for index, at in enumerate(drawing.grounds):
        add(drawing.ground_names.get(at, "0"), reference.grounds[index] if index < len(reference.grounds) else at, at)
    return result


class _BoardRouter:
    def __init__(self, circuit, drawing, reference, groups):
        self.drawing, self.groups = drawing, groups
        coordinates = [at for group in groups.values() for at in group["points"]]
        coordinates.extend(at for edges in reference.wires.values() for edge in edges for at in edge)
        self.scale = scale = _routing_scale(coordinates)
        self.pins = {owner: list(dict.fromkeys(self.grid(at) for at in group["points"]))
                     for owner, group in groups.items()}
        extent = [self.grid(at) for at in coordinates]
        spec = circuit.perfboard
        self.limits = (min([-scale, *(x - scale for x, _ in extent)]),
                       max([scale * spec.column_count, *(x + scale for x, _ in extent)]),
                       min([-scale * spec.row_count, *(y - scale for _, y in extent)]),
                       max([scale, *(y + scale for _, y in extent)]))
        self.bodies = []
        for symbol in drawing.symbols:
            a, b = symbol_bounds(symbol, drawing.rotations.get(symbol[0].name, 0), body_only=True)
            padding = .25 - 1e-7 if symbol[0].kind == "H" else 0
            self.bodies.append((min(a[0], b[0]) - padding, min(a[1], b[1]) - padding,
                                max(a[0], b[0]) + padding, max(a[1], b[1]) + padding))
        self.contacts, self.tracks, self.corners = defaultdict(set), defaultdict(dict), defaultdict(set)
        for owner, points in self.pins.items():
            for at in points:
                self.contacts[at].add(owner)
        self.wires, self.back = defaultdict(list), defaultdict(list)

    def grid(self, at):
        return tuple(round(value * self.scale) for value in at)

    def world(self, at):
        return at[0] / self.scale, at[1] / self.scale

    def whole(self, at):
        return not any(value % self.scale for value in at)

    def allowed(self, at, side, owner):
        x, y = at
        if not (self.limits[0] <= x <= self.limits[1] and self.limits[2] <= y <= self.limits[3]):
            return False
        if self.contacts[at] - {owner}:
            return False
        if any(other != owner and other[0] == owner[0]
               for face in (0, 1) for other in self.tracks.get((at, face), {})):
            return False
        if side == 0:
            x, y = self.world(at)
            if any(left - 1e-8 <= x <= right + 1e-8 and bottom - 1e-8 <= y <= top + 1e-8
                   for left, bottom, right, top in self.bodies):
                return False
        return True

    def physical(self, at, owner):
        return not any(other != owner for side in (0, 1) for other in self.tracks.get((at, side), {}))

    def connect(self, start, tree, owner):
        goals = {(at, side) for at, side in tree}
        points = [at for at, _ in tree]
        left, right = min(x for x, _ in points), max(x for x, _ in points)
        bottom, top = min(y for _, y in points), max(y for _, y in points)
        queue, costs, previous = [], {}, {}
        for side in (0, 1):
            if self.allowed(start, side, owner):
                state = start, side, -1
                costs[state] = side * .01
                heappush(queue, (side * .01, side * .01, state))
        end = None
        while queue:
            _, cost, state = heappop(queue)
            if cost != costs[state]:
                continue
            at, side, axis = state
            if (at, side) in goals and (not (side or self.whole(at)) or self.physical(at, owner)):
                end = state
                break
            options = [((at[0] + dx, at[1] + dy), side, direction)
                       for dx, dy, direction in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1))]
            if self.whole(at) and self.physical(at, owner):
                options.append((at, 1 - side, -1))
            for other, face, direction in options:
                if not self.allowed(other, face, owner):
                    continue
                transition = face != side
                here = self.tracks.get((at, side), {})
                there = self.tracks.get((other, face), {})
                turn = not transition and axis != -1 and direction != axis
                if turn and (side or self.whole(at)) and not self.physical(at, owner):
                    continue
                if not transition:
                    if any(name != owner and (axis != direction or at in self.corners[name]
                                              or direction in axes) for name, axes in here.items()):
                        continue
                    if any(name != owner and (direction in axes or other in self.corners[name])
                           for name, axes in there.items()):
                        continue
                step = 1.2 * self.scale if transition else 1 + turn * .6 * self.scale
                step += 4 * self.scale * sum(name != owner for name in there)
                new = cost + step
                candidate = other, face, direction
                if new >= costs.get(candidate, float("inf")):
                    continue
                costs[candidate], previous[candidate] = new, state
                x, y = other
                heuristic = max(left - x, 0, x - right) + max(bottom - y, 0, y - top)
                heappush(queue, (new + heuristic, new, candidate))
        if end is None:
            return None
        path = [end]
        while path[-1] in previous:
            path.append(previous[path[-1]])
        return list(reversed(path))

    def reserve(self, owner, path):
        runs = []
        for at, side, _ in path:
            if not runs or runs[-1][0] != side:
                runs.append((side, [at]))
            elif runs[-1][1][-1] != at:
                runs[-1][1].append(at)
        for side, points in runs:
            compact = _compress(points)
            for at in compact:
                self.corners[owner].add(at)
                if side or self.whole(at):
                    self.contacts[at].add(owner)
            for a, b in zip(points, points[1:]):
                axis = int(a[0] == b[0])
                for at in (a, b):
                    self.tracks[at, side].setdefault(owner, set()).add(axis)
            for a, b in zip(compact, compact[1:]):
                edge = self.world(a), self.world(b)
                self.wires[owner[0]].append(edge)
                if side:
                    self.back[owner[0]].append(edge)

    def route(self, order):
        if any(len(owners) > 1 for owners in self.contacts.values()):
            return None
        for owner in order:
            points = list(self.pins[owner])
            if len(points) < 2:
                continue
            root = points.pop(0)
            tree = {(root, 0), (root, 1)}
            while points:
                start = min(points, key=lambda at: (min(abs(at[0] - p[0]) + abs(at[1] - p[1]) for p, _ in tree), at))
                points.remove(start)
                path = self.connect(start, tree, owner)
                if path is None:
                    return None
                self.reserve(owner, path)
                tree.update((at, side) for at, side, _ in path)
                tree.update(((start, 0), (start, 1)))
        # Preserve exact legacy label coordinates with small component-side stubs.
        for owner, group in self.groups.items():
            for at in group["points"]:
                snapped = self.world(self.grid(at))
                if at != snapped:
                    compact = _compress(list(dict.fromkeys((at, (at[0], snapped[1]), snapped))))
                    self.wires[owner[0]].extend(zip(compact, compact[1:]))
        result = replace(self.drawing, wires=dict(self.wires), underside_wires=dict(self.back),
                         crossings={}, junctions={}, labels=dict(self.drawing.labels))
        mark_crossings(result)
        return result


def optimize_board_wiring(circuit, drawing, *, reference=None):
    """Use both faces without moving parts, retaining a safe original on failure."""
    if circuit.perfboard is None:
        return drawing
    reference = reference or drawing
    groups = _groups(reference, drawing)
    order = sorted(groups, key=lambda owner: (owner[0] in circuit.grounds, -len(groups[owner]["points"]), repr(owner)))
    safe_original = preserves_nets(circuit, drawing) and clear_of_bodies(
        [edge for edges in drawing.wires.values() for edge in edges], drawing)
    best = drawing if safe_original else None
    for sequence in (order, list(reversed(order))):
        candidate = _BoardRouter(circuit, drawing, reference, groups).route(sequence)
        if candidate is None or not preserves_nets(circuit, candidate):
            continue
        if not clear_of_bodies([edge for edges in layer_wires(candidate).values() for edge in edges], candidate, underside=False):
            continue
        if safe_original and (wire_length(candidate) > wire_length(drawing) + 1e-7
                              or crossing_count(candidate) > crossing_count(drawing)):
            continue
        if best is None or wiring_cost(candidate) < wiring_cost(best) - 1e-7:
            best = candidate
    return best if best is not None else drawing
