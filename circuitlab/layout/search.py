"""Evolve schematic placements while keeping rails and series runs intact."""

from collections import defaultdict
from random import Random


def feedback_pairs(parts):
    """Find passive branches that connect an op-amp output to a sensing pin."""
    by_terminals = defaultdict(list)
    for i, part in enumerate(parts):
        if part.kind in "RCLXZK":
            by_terminals[frozenset((part.p, part.n))].append(i)
    return tuple(
        (i, j, pin)
        for i, amp in enumerate(parts) if amp.kind == "O"
        for pin in dict.fromkeys((amp.in_minus, amp.in_plus))
        if pin not in (amp.p, amp.n)
        for j in by_terminals[frozenset((amp.p, pin))]
    )


def placement_penalty(cells, feedback):
    """Favor a feedback component above its amp, with matching pin columns."""
    penalty = 0
    for amp, resistor, _ in feedback:
        amp_row, amp_col = cells[amp]
        row, col = cells[resistor]
        penalty += 30 * abs(amp_col - col) + 16 * abs(amp_row - row - 1)
    return penalty


def evolve_placements(seeds, units, feedback, evaluate, *, population_size=6):
    """Keep fit parents and mutate layouts with a reproducible random sequence."""
    rng = Random(0)
    population = list(seeds)
    movable = {unit[0] for unit in units if len(unit) == 1}
    if len(units) < 2:
        return
    owner = {i: unit for unit in units for i in unit}
    feedback = [(amp, part) for amp, part, _ in feedback if part in movable]

    for _ in range(4 if len(units) <= 30 else 2):
        parents = sorted(population, key=lambda candidate: candidate[0])[:population_size]
        population = parents.copy()
        for _, parent in parents:
            first_row = min(parent[unit[0]][0] for unit in units)
            for _ in range(5):
                cells = parent.copy()
                if feedback and rng.random() < 0.45:
                    amp, part = rng.choice(feedback)
                    first = (part,)
                    row, col = cells[amp]
                    target = (row - 1, col)
                    # Keep the source rows reserved, even if they have spare cells.
                    if target[0] < first_row:
                        cells = {
                            i: (r + (i in owner), c) for i, (r, c) in cells.items()
                        }
                        target = (row, col)
                else:
                    first, second = rng.sample(units, 2)
                    target = cells[second[0]]
                origin = cells[first[0]]
                occupied = {cell: i for i, cell in cells.items()}
                row, col = target
                blockers = {
                    owner.get(occupied[(row, col + offset)])
                    for offset in range(len(first)) if (row, col + offset) in occupied
                } - {first}
                if len(blockers) > 1 or None in blockers:
                    continue
                second = next(iter(blockers), ())
                if second and (len(second) != len(first) or cells[second[0]] != target):
                    continue
                for unit, (r, c) in ((first, target), (second, origin)):
                    for offset, i in enumerate(unit):
                        cells[i] = r, c + offset
                score = evaluate(cells)
                if score is not None:
                    population.append((score, cells))
