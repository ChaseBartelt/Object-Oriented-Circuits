"""Match interchangeable passives to existing board positions before routing."""

from collections import defaultdict
from dataclasses import replace
from functools import lru_cache
from itertools import combinations

from scipy.optimize import linear_sum_assignment

from circuitlab.layout.geometry import routed_pins
from circuitlab.layout.wire_edit import clean_point, physical_wire_islands


PASSIVES = {"R", "C", "L", "X", "Z"}


@lru_cache(maxsize=8192)
def _tree_length(points):
    """Manhattan minimum spanning tree, independent of existing wire detours."""
    if len(points) < 2:
        return 0.0
    remaining = list(points[1:])
    costs = [abs(at[0] - points[0][0]) + abs(at[1] - points[0][1]) for at in remaining]
    total = 0.0
    while remaining:
        index = min(range(len(remaining)), key=lambda i: (costs[i], remaining[i]))
        at = remaining.pop(index)
        total += costs.pop(index)
        costs = [min(cost, abs(other[0] - at[0]) + abs(other[1] - at[1]))
                 for cost, other in zip(costs, remaining)]
    return total


def _memberships(reference, drawing):
    split = {net for net, *_ in reference.connectors}
    split.update(reference.ground_names.get(at, "0") for at in reference.grounds)
    islands = {net: physical_wire_islands(reference, net)[1] for net in split}

    def owner(net, at):
        return net, islands[net].get(clean_point(at), clean_point(at)) if net in split else None

    memberships = {}
    for symbol in reference.symbols:
        name = symbol[0].name
        for index, (net, at) in enumerate(routed_pins(symbol, reference.rotations.get(name, 0), reference.pin_grid)):
            memberships[name, index] = owner(net, at)
    anchors = []
    for net, at in drawing.boundary.items():
        anchors.append((owner(net, reference.boundary.get(net, at)), clean_point(at)))
    for index, (net, at, *_) in enumerate(drawing.connectors):
        before = reference.connectors[index][1] if index < len(reference.connectors) else at
        anchors.append((owner(net, before), clean_point(at)))
    for at in drawing.grounds:
        anchors.append((owner(drawing.ground_names.get(at, "0"), at), clean_point(at)))
    return memberships, anchors


def assign_interchangeable_parts(circuit, drawing, *, reference=None):
    """Improve passive identities at fixed physical slots; return unchanged on ties.

    Route the result with ``reference=reference or drawing``. That unchanged
    reference retains source pin identities and each explicitly split island.
    Nonpolar terminal reversal is permitted, but electrical p/n roles are kept.
    """
    if circuit.perfboard is None:
        return drawing
    reference = reference or drawing
    excluded = {name.casefold() for name in circuit.perfboard.exclusions}
    groups = defaultdict(list)
    for index, (part, *_) in enumerate(drawing.symbols):
        if (part.kind not in PASSIVES or part.value is None
                or excluded.intersection((part.name.casefold(), part.label.casefold()))):
            continue
        signature = replace(part, name="", p="", n="", label="", order=0)
        groups[signature].append(index)
    groups = [indices for indices in groups.values() if len(indices) > 1]
    if not groups:
        return drawing

    base = drawing.symbols
    turns = [drawing.rotations.get(symbol[0].name, 0) for symbol in base]
    memberships, anchors = _memberships(reference, drawing)
    state = tuple((index, False) for index in range(len(base)))

    def symbol(item, slot):
        source, reverse = item
        _, a, b, top = base[slot]
        return base[source][0], b if reverse else a, a if reverse else b, top

    def contacts(candidate):
        result = list(anchors)
        for slot, item in enumerate(candidate):
            placed = symbol(item, slot)
            for pin, (net, at) in enumerate(routed_pins(placed, turns[slot], drawing.pin_grid)):
                result.append((memberships.get((placed[0].name, pin), (net, None)), clean_point(at)))
        return result

    def conflicts(points):
        owners = defaultdict(set)
        for owner, at in points:
            owners[at].add(owner)
        return {(at, frozenset((a, b))) for at, names in owners.items() for a, b in combinations(names, 2)}

    existing_conflicts = conflicts(contacts(state))

    @lru_cache(maxsize=4096)
    def score(candidate):
        points = contacts(candidate)
        if not conflicts(points).issubset(existing_conflicts):
            return float("inf")
        trees = defaultdict(set)
        for owner, at in points:
            trees[owner].add(at)
        return sum(_tree_length(tuple(sorted(points))) for points in trees.values())

    initial = score(state)
    cost = initial
    for _ in range(4):
        improved = False
        for indices in groups:
            sources = sorted(item[0] for slot, item in enumerate(state) if slot in indices)
            targets = defaultdict(list)
            for slot, item in enumerate(state):
                if slot in indices:
                    continue
                placed = symbol(item, slot)
                for pin, (net, at) in enumerate(routed_pins(placed, turns[slot], drawing.pin_grid)):
                    targets[memberships.get((placed[0].name, pin), (net, None))].append(at)
            for owner, at in anchors:
                targets[owner].append(at)
            choices, matrix = {}, []
            current_points = contacts(state)
            for source in sources:
                row = []
                part = base[source][0]
                for slot in indices:
                    alternatives = []
                    for reverse in (False, True):
                        value = 0.0
                        for pin, (net, at) in enumerate(routed_pins(symbol((source, reverse), slot), turns[slot], drawing.pin_grid)):
                            owner = memberships.get((part.name, pin), (net, None))
                            destinations = targets[owner] or [point for other, point in current_points if other == owner]
                            if destinations:
                                value += min(abs(at[0] - p[0]) + abs(at[1] - p[1]) for p in destinations)
                        alternatives.append((value, reverse))
                    value, reverse = min(alternatives)
                    choices[source, slot] = reverse
                    row.append(value)
                matrix.append(row)
            rows, columns = linear_sum_assignment(matrix)
            candidate = list(state)
            for row, column in zip(rows, columns):
                source, slot = sources[row], indices[column]
                candidate[slot] = source, choices[source, slot]
            candidate = tuple(candidate)
            best, best_cost = (candidate, score(candidate)) if score(candidate) < cost - 1e-7 else (state, cost)
            # Refine the global matching with strict improvements, never a
            # greedy equal-cost swap that can oscillate between assignments.
            for slot in indices:
                trial = list(state)
                source, reverse = trial[slot]
                trial[slot] = source, not reverse
                trial = tuple(trial)
                value = score(trial)
                if value < best_cost - 1e-7:
                    best, best_cost = trial, value
            if len(indices) <= 16:
                for first, second in combinations(indices, 2):
                    for a in (False, True):
                        for b in (False, True):
                            trial = list(state)
                            trial[first], trial[second] = (state[second][0], a), (state[first][0], b)
                            trial = tuple(trial)
                            value = score(trial)
                            if value < best_cost - 1e-7:
                                best, best_cost = trial, value
            if best_cost < cost - 1e-7:
                state, cost, improved = best, best_cost, True
        if not improved:
            break
    if cost >= initial - 1e-7:
        return drawing

    placements = {base[item[0]][0].name: (symbol(item, slot), turns[slot], base[slot][0].name)
                  for slot, item in enumerate(state)}
    symbols = [placements[part.name][0] for part, *_ in base]
    rotations = {name: rotation for name, (_, rotation, _) in placements.items() if rotation}
    incoming = {old: name for name, (_, _, old) in placements.items()}
    offsets = {(key[0], incoming.get(key[1], key[1]), *key[2:]) if key[0] in {"component", "terminal"}
               and len(key) > 1 else key: value for key, value in drawing.label_offsets.items()}
    compact = frozenset(incoming.get(name, name) for name in drawing.compact_labels)
    return replace(drawing, symbols=symbols, positions={part.name: a for part, a, *_ in symbols},
                   rotations=rotations, label_offsets=offsets, compact_labels=compact)
