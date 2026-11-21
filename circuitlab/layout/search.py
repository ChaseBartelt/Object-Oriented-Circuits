"""Evolve schematic placements while keeping rails and series runs intact."""
# Placement search preserves useful source rails and series runs while proposing local changes.
# A reproducible random sequence makes the optional search repeatable for the same input;
# the connectivity checks still determine which candidates are acceptable.

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


def _stage_neighbors(parts, grounds, stage_outputs, unit_for, loops):
    """Identify the feed and load units belonging to individual feedback stages."""
    feeders, loads, parents = defaultdict(list), defaultdict(list), defaultdict(set)
    feedback_parts = {branch for branches in loops.values() for branch in branches}
    passive_units = []
    for index, part in enumerate(parts):
        if part.kind in "RCLXZ" and index not in feedback_parts:
            unit = unit_for[index]
            if not feedback_parts.intersection(unit):
                passive_units.append((part, unit))
    assignments = defaultdict(set)
    for amp in stage_outputs.values():
        amplifier = parts[amp]
        inputs = {amplifier.in_plus, amplifier.in_minus} - grounds
        for part, unit in passive_units:
            nets = {part.p, part.n}
            if nets & inputs and nets - inputs - grounds:
                ext = {net for member in unit for net in parts[member].terminal_nets} - inputs - grounds
                parents[amp].update(stage_outputs[net] for net in ext & stage_outputs.keys()
                                    if stage_outputs[net] != amp)
                assignments[unit].add((amp, False))
            elif amplifier.p in nets and nets & grounds:
                assignments[unit].add((amp, True))
    for unit, roles in assignments.items():
        if len(roles) != 1:
            continue
        (amp, shunt), = roles
        (loads if shunt else feeders)[amp].append(unit)
    return feeders, loads, parents


def feedback_stage_cells(circuit, initial, units, feedback):
    """Yield placements and whether to use tighter routing for each feedback-stage seed."""
    parts, grounds = circuit.components, circuit.grounds
    loops = defaultdict(list)
    for amp, branch, _ in feedback:
        loops[amp].append(branch)
    if len(loops) < 2:
        return
    stages = sorted(loops, key=lambda amp: (parts[amp].order, amp))
    stage_outputs = {parts[amp].p: amp for amp in stages}
    if len(stage_outputs) != len(stages):
        return
    unit_for = {member: tuple(unit) for unit in units for member in unit}
    feeders, loads, parents = _stage_neighbors(parts, grounds, stage_outputs, unit_for, loops)
    # Cyclic stage graphs are valid circuits, but they do not have a left-to-right
    # feed order. Leave those to the general placement search.
    paths = {}
    pending = stages.copy()
    while pending:
        ready = next((amp for amp in pending if parents[amp] <= paths.keys()), None)
        if ready is None:
            return
        pending.remove(ready)
        previous = max((paths[amp] for amp in sorted(parents[ready])), key=len, default=())
        paths[ready] = (*previous, ready)
    sources = {i: position for i, position in initial.items() if parts[i].is_source}
    origin_y = 1 + max((row for row, _ in sources.values()), default=-1)
    origin_x = 1 + max((col for _, col in sources.values()), default=-1)
    stage_rows = 1 + max(max(len(loops[amp]), len(feeders[amp]), len(loads[amp])) for amp in stages)
    feeder_span = max((len(unit) for branches in feeders.values() for unit in branches), default=1)
    longest_path = max(paths.values(), key=len)
    chain_rank = {amp: index for index, amp in enumerate(longest_path)}
    chain_cols = {amp: origin_x + feeder_span + index * (feeder_span + 1)
                  for amp, index in chain_rank.items()}
    main_row = origin_y + max(map(len, loops.values()))
    for arrangement in ("depth", "column", "signal_row", "two_columns", "staggered", "forward_chain"):
        main_chain = arrangement == "forward_chain"
        if main_chain and len(longest_path) < 3:
            continue
        cells = sources.copy()
        rows = defaultdict(int)
        for index, (amp, path) in enumerate(paths.items()):
            if main_chain:
                # Follow the longest forward signal path. Stage branches start
                # below the most recent shared point along that chain.
                if amp in chain_cols:
                    row, col = main_row, chain_cols[amp]
                else:
                    ancestor = next((parent for parent in reversed(path) if parent in chain_cols), longest_path[0])
                    row = main_row + 2 + rows[ancestor] * stage_rows
                    col = chain_cols[ancestor]
                    rows[ancestor] += 1
            elif arrangement in {"two_columns", "staggered"}:
                rank, column = divmod(index, 2)
                col = origin_x + feeder_span + column * (feeder_span + 2)
                row = origin_y + (rank + 1) * stage_rows - 1
                if arrangement == "staggered":
                    row += column
            else:
                depth = len(path) - 1
                bucket = None if arrangement == "column" else depth
                rank = rows[bucket]
                rows[bucket] += 1
                pitch = {"column": 1, "signal_row": feeder_span + 1}.get(arrangement, feeder_span + 2)
                col = origin_x + feeder_span + depth * pitch
                row = origin_y + (rank + 1) * stage_rows - 1
            cells[amp] = row, col
            for offset, part in enumerate(loops[amp], 1):
                if len(unit_for[part]) == 1:
                    cells[part] = row - offset, col
            inputs = feeders[amp]
            if main_chain and chain_rank.get(amp, 0):
                upstream = parts[longest_path[chain_rank[amp] - 1]].p
                inputs = sorted(inputs, key=lambda unit: not any(upstream in parts[i].terminal_nets for i in unit))
            for offset, unit in enumerate(inputs):
                for n, part in enumerate(unit):
                    cells[part] = row - offset, col - len(unit) + n
            for offset, unit in enumerate(loads[amp]):
                for n, part in enumerate(unit):
                    if main_chain or arrangement == "signal_row":
                        cells[part] = row + offset + 1, col + n + int(main_chain)
                    else:
                        cells[part] = row - offset, col + 1 + n
        if len(set(cells.values())) != len(cells):
            continue
        # Keep unrelated series chains intact in spare rows below the stages.
        row = 1 + max(r for r, _ in cells.values())
        used = set(cells)
        occupied = set(cells.values())
        for unit in units:
            if used.issuperset(unit):
                continue
            if used.intersection(unit):
                break
            if main_chain and len(unit) == 1:
                part = parts[unit[0]]
                nets = set(part.terminal_nets) - grounds
                if part.kind in "RCLXZ" and len(nets) == 1:
                    nearest = [
                        cell for i, cell in cells.items()
                        if parts[i].kind in "RCLXZ" and nets.intersection(parts[i].terminal_nets)
                    ]
                    slot = next(((r + 1, c) for r, c in nearest if (r + 1, c) not in occupied), None)
                    if slot is not None:
                        cells[unit[0]] = slot
                        occupied.add(slot)
                        continue
            while any((row, col) in occupied for col in range(origin_x, origin_x + len(unit))):
                row += 1
            for col, part in enumerate(unit, origin_x):
                cells[part] = row, col
                occupied.add((row, col))
            row += 1
        else:
            yield cells, main_chain


def circuit_instance_cells(circuit, initial, units, feedback):
    """Try neighboring regions for the members of flattened circuit calls."""
    parts = circuit.components
    groups = defaultdict(list)
    for unit in units:
        owners = {circuit.owners.get(parts[member].name) for member in unit}
        owner = next(iter(owners)) if len(owners) == 1 else None
        if owner is not None and circuit.instances[owner].kind == "circuit":
            groups[owner].append(unit)
        else:
            groups[None].append(unit)
    sections = [sorted(units, key=lambda unit: min(parts[i].order for i in unit))
                for _, units in sorted((key, value) for key, value in groups.items() if key is not None)]
    if len(sections) < 2:
        return
    sources = {i: cell for i, cell in initial.items() if parts[i].is_source}
    origin_y = 1 + max((row for row, _ in sources.values()), default=-1)
    origin_x = 1 + max((col for _, col in sources.values()), default=-1)
    ungrouped = sorted(groups[None], key=lambda unit: min(parts[i].order for i in unit))
    # Leave early ungrouped stages before the instantiated regions and the
    # downstream stages beyond them.
    start_order = min(parts[i].order for group in sections for unit in group for i in unit)
    upstream = [unit for unit in ungrouped if max(parts[i].order for i in unit) < start_order]
    downstream = [unit for unit in ungrouped if unit not in upstream]
    loops = defaultdict(list)
    for amp, branch, _ in feedback:
        loops[amp].append(branch)

    def tile(units, width):
        width = max(width, max(map(len, units), default=0))
        cells = {}
        row = col = 0
        members = {i for unit in units for i in unit}
        feedback_parts = {
            branch for amp in members for branch in loops[amp]
            if (branch,) in units
        }
        for unit in units:
            if len(unit) == 1 and unit[0] in feedback_parts:
                continue
            if col + len(unit) > width:
                row += 1
                col = 0
            for member in unit:
                cells[member] = row, col
                col += 1
        if feedback_parts:
            cells = {i: (row + 1, col) for i, (row, col) in cells.items()}
            occupied = set(cells.values())
            for amp in cells.copy():
                row, col = cells[amp]
                for branch in loops[amp]:
                    if branch not in feedback_parts:
                        continue
                    target = next((pos for pos in ((row - 1, col), (0, col), (0, col - 1), (0, col + 1))
                                   if pos[1] >= 0 and pos not in occupied), None)
                    if target is None:
                        target = (0, max(c for _, c in occupied) + 1)
                    cells[branch] = target
                    occupied.add(target)
        return (cells, 1 + max((row for row, _ in cells.values()), default=-1),
                1 + max((col for _, col in cells.values()), default=-1))

    def add(cells, tile, row, col):
        cells.update({i: (row + y, col + x) for i, (y, x) in tile.items()})

    for width, columns in ((3, 2), (4, 2), (3, 1), (3, 3), (6, 1)):
        cells = sources.copy()
        before, _, before_width = tile(upstream, 2)
        add(cells, before, origin_y, origin_x)
        row, col = origin_y, origin_x + before_width
        shelf_height = 0
        far_right = col
        for index, group in enumerate(sections):
            if index and index % columns == 0:
                row, col = row + shelf_height, origin_x + before_width
                shelf_height = 0
            section, height, span = tile(group, width)
            add(cells, section, row, col)
            col += span
            far_right = max(far_right, col)
            shelf_height = max(shelf_height, height)
        after, _, _ = tile(downstream, width)
        add(cells, after, origin_y, far_right)
        yield cells


# Retain successful parent placements and mutate a reproducible candidate population.
# This search proposes geometry; electrical validity remains a separate acceptance check.
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
