"""Shared drawing geometry and conventional circuit layouts."""

from collections import deque
from dataclasses import dataclass, field
from statistics import median


@dataclass
class Layout:
    """Drawing coordinates with positive y up, keyed by electrical net."""

    positions: dict = field(default_factory=dict)
    symbols: list = field(default_factory=list)
    wires: dict = field(default_factory=dict)
    junctions: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    grounds: list = field(default_factory=list)
    ground_names: dict = field(default_factory=dict)
    connectors: list = field(default_factory=list)
    crossings: dict = field(default_factory=dict)

    def pins(self):
        """Yield the physical pin coordinates and the net attached to each."""
        for part, a, b, top in self.symbols:
            if part.kind == "O":
                plus, minus = (0.35, -0.35) if top == "+" else (-0.35, 0.35)
                yield part.in_plus, (a[0], a[1] + plus)
                yield part.in_minus, (a[0], a[1] + minus)
                yield part.p, b
            else:
                yield part.p, a
                yield part.n, b

    def visible_segments(self, net):
        """Cut visual gaps at crossings while retaining the original net ID."""
        for a, b in self.wires[net]:
            cuts = self.crossings.get((net, a, b))
            if not cuts:
                yield a, b
                continue
            x, start = a
            end = b[1]
            for crossing in sorted(cuts):
                low, high = crossing - 0.12, crossing + 0.12
                if low > start:
                    yield (x, start), (x, low)
                start = max(start, high)
            if end > start:
                yield (x, start), (x, end)


def component_pins(parts, flipped=(), reversed_parts=()):
    """Describe each grid pin as (component index, net, left side, y offset)."""
    count = len(parts)
    pins = [
        (i, e.p, e.kind != "O" and i not in reversed_parts, 0.0)
        for i, e in enumerate(parts)
    ]
    pins += [
        (i, e.in_plus, True, -0.35 if i in flipped else 0.35)
        if e.kind == "O"
        else (i, e.n, i in reversed_parts, 0.0)
        for i, e in enumerate(parts)
    ]
    pins += [
        (i, e.in_minus, True, 0.35 if i in flipped else -0.35)
        for i, e in enumerate(parts)
        if e.kind == "O"
    ]
    assert len(pins) >= count * 2
    return pins


def placement_order(parts, pins, grounds=("0",), roots=None):
    """Visit nearby components without letting ground dominate placement."""
    nets = {}
    for i, net, *_ in pins:
        nets.setdefault(net, set()).add(i)
    adjacent = [set() for _ in parts]
    for net, owners in nets.items():
        if net not in grounds and len(owners) <= 6:
            for i in owners:
                adjacent[i].update(owners)
    sources = (
        [i for i, e in enumerate(parts) if e.kind in "VI"] if roots is None else roots
    )
    pending = set(range(len(parts))) - set(sources)
    queue = deque(sources)
    order = []
    while queue or pending:
        if not queue:
            i = min(pending)
            pending.remove(i)
            queue.append(i)
        i = queue.popleft()
        order.append(i)
        neighbors = sorted(adjacent[i] & pending)
        pending.difference_update(neighbors)
        queue.extend(neighbors)
    return order


def input_source_indices(parts):
    first_stage = min(
        (part.order for part in parts if not part.is_source and part.order), default=0
    )
    return [
        i
        for i, part in enumerate(parts)
        if part.is_source and (not first_stage or part.order <= first_stage)
    ]


def series_runs(circuit):
    """Keep declared series groups and unbranched graph paths together."""
    parts = circuit.components
    incident = {}
    for i, net, *_ in component_pins(parts):
        incident.setdefault(net, []).append(i)
    passive = {
        i for i, part in enumerate(parts)
        if not part.is_source and part.kind != "O" and part.p != part.n
    }
    joins = {i: {} for i in passive}

    def join(a, b, net):
        if net not in joins[a] and net not in joins[b]:
            joins[a][net] = b
            joins[b][net] = a

    # A named tap can have a third connection and still belong to the
    # original series run. Use the compiled terminals to verify each join.
    by_name = {parts[i].name: i for i in passive}
    by_order = {parts[i].order: i for i in passive}
    sequences = {}
    for index in reversed(range(len(circuit.groups))):
        group = circuit.groups[index]
        if group.mode != "series":
            continue
        sequence = []
        for position, (kind, name, value) in enumerate(group.entries):
            if kind == "G":
                sequence.extend(sequences.get(value, [None]))
            elif kind in "RCLXP":
                sequence.append(by_name.get(name, by_order.get(group.entry_order.get(position))))
            else:
                sequence.append(None)
        for a, b in zip(sequence, sequence[1:]):
            if a is not None and b is not None and a != b:
                shared = {parts[a].p, parts[a].n} & {parts[b].p, parts[b].n}
                if len(shared) == 1 and not shared & circuit.grounds:
                    join(a, b, shared.pop())
        sequences[index] = sequence

    for net, owners in incident.items():
        if net not in circuit.grounds and len(owners) == 2:
            a, b = owners
            if a in passive and b in passive:
                join(a, b, net)

    # A run begins at an open end. Leave closed rings to the general router.
    drivers = {
        net for part in parts if part.is_source or part.kind == "O"
        for net in ((part.p, part.n) if part.is_source else (part.p,))
    } - circuit.grounds
    ends = []
    for i, neighbors in joins.items():
        if len(neighbors) == 1:
            part = parts[i]
            net = part.n if part.p in neighbors else part.p
            ends.append((net in circuit.grounds, net not in drivers, part.order, i, net))

    runs, visited, reversed_parts = [], set(), set()
    for _, _, _, i, incoming in sorted(ends):
        run = []
        while i not in visited:
            run.append(i)
            visited.add(i)
            part = parts[i]
            if part.p == incoming:
                outgoing = part.n
            else:
                reversed_parts.add(i)
                outgoing = part.p
            if outgoing not in joins[i]:
                break
            i, incoming = joins[i][outgoing], outgoing
        if run:
            runs.append(run)
    return runs, reversed_parts


def source_pin_is_upper(part, positive, grounds):
    """The grounded terminal goes below the active terminal, regardless of polarity."""
    return positive != (part.p in grounds and part.n not in grounds)


def source_cells(circuit, order, columns, runs=(), *, source_rails=False):
    """Stack the input sources in a left column, and place later sources by their nets."""
    parts = circuit.components
    sources = [i for i in order if parts[i].is_source]
    other = [i for i in order if not parts[i].is_source]
    if not other:
        return {i: (row, 0) for row, i in enumerate(sources)}

    inputs = input_source_indices(parts)
    early = [i for i in sources if i in inputs]
    late = [i for i in sources if i not in early]
    if source_rails:
        cells = {
            i: (row, int(bool(early) and i in late))
            for row, i in enumerate(early + late)
        }
        start_column = 1 + max((col for _, col in cells.values()), default=-1)
        first_row = len(cells)
        late = []
    else:
        cells = {i: (row, 0) for row, i in enumerate(early)}
        start_column = int(bool(early))
        first_row = 0
    columns = min(max(columns, max(map(len, runs), default=0)), len(other) + len(late))
    occupied = set(cells.values())
    by_member = {i: run for run in runs for i in run}
    row, column = first_row, 0
    for i in other:
        if i in cells:
            continue
        run = by_member.get(i, (i,))
        if column + len(run) > columns:
            row += 1
            column = 0
        for member in run:
            cells[member] = row, column + start_column
            column += 1
    occupied.update(cells.values())

    for i in late:
        nets = {parts[i].p, parts[i].n} - circuit.grounds
        neighbors = [
            cells[j]
            for j in other
            if nets.intersection(
                (parts[j].p, parts[j].n, parts[j].in_plus, parts[j].in_minus)
            )
        ]
        if not neighbors:
            preceding = [j for j in other if 0 < parts[j].order < parts[i].order]
            nearest = (
                max(preceding, key=lambda j: parts[j].order) if preceding else other[0]
            )
            neighbors = [cells[nearest]]
        row = round(median(r for r, _ in neighbors))
        column = round(median(c for _, c in neighbors))
        candidates = [
            (r, c)
            for r in range(max(row, max(r for r, _ in occupied)) + 2)
            for c in range(start_column, columns + start_column)
            if (r, c) not in occupied
        ]
        cell = min(
            candidates,
            key=lambda cell: (abs(cell[1] - column), abs(cell[0] - row), cell),
        )
        cells[i] = cell
        occupied.add(cell)
    return cells


def grid_symbol(part, x, y, grounds, *, top="+", reverse=False):
    """Place a symbol in a grid cell, keeping grounded sources vertical."""
    if part.is_source:
        start, end = (x + 2.15, y), (x + 2.15, y - 2.5)
        if not source_pin_is_upper(part, True, grounds):
            start, end = end, start
    else:
        start, end = (x, y), (x + 4.3, y)
        if reverse:
            start, end = end, start
    return part, start, end, top


@dataclass(frozen=True)
class Block:
    """A two-terminal component or series/parallel network for compact layout."""

    p: str
    n: str
    kind: str
    part: object = None
    children: tuple = ()
    width: float = 4.3
    depth: float = 0

    def oriented(self, start):
        if self.p == start:
            return self
        children = reversed(self.children) if self.kind == "series" else self.children
        return Block(
            self.n,
            self.p,
            self.kind,
            self.part,
            tuple(child.oriented(child.n) for child in children),
            self.width,
            self.depth,
        )


def _combine(kind, children, p, n):
    flattened = []
    for child in children:
        if kind == "parallel":
            child = child.oriented(p)
        flattened.extend(child.children if child.kind == kind else (child,))
    widths = [b.width for b in flattened]
    depths = [b.depth for b in flattened]
    width = max(widths) + 1.4 if kind == "parallel" else sum(widths)
    depth = (
        sum(depths) + 2 * (len(flattened) - 1) if kind == "parallel" else max(depths)
    )
    return Block(p, n, kind, children=tuple(flattened), width=width, depth=depth)


def series_parallel(parts, terminals):
    """Reduce shared endpoints and degree-two junctions into a drawing tree."""
    if not parts or any(e.p == e.n for e in parts):
        return None
    blocks = [Block(e.p, e.n, "part", part=e) for e in parts]
    while len(blocks) > 1:
        for i, block in enumerate(blocks):
            indices = [
                j for j, b in enumerate(blocks) if {b.p, b.n} == {block.p, block.n}
            ]
            if len(indices) > 1:
                blocks[i] = _combine(
                    "parallel", [blocks[j] for j in indices], block.p, block.n
                )
                for j in reversed(indices[1:]):
                    del blocks[j]
                break
        else:
            nodes = dict.fromkeys(node for b in blocks for node in (b.p, b.n))
            for node in nodes:
                if node in terminals:
                    continue
                incident = [i for i, b in enumerate(blocks) if node in (b.p, b.n)]
                if len(incident) != 2:
                    continue
                i, j = incident
                first, second = blocks[i], blocks[j]
                first = first.oriented(first.n if first.p == node else first.p)
                second = second.oriented(node)
                if first.p != second.n:
                    blocks[i] = _combine("series", (first, second), first.p, second.n)
                    del blocks[j]
                    break
            else:
                return None
    b = blocks[0]
    if terminals[0] != terminals[1] and set(terminals) == {b.p, b.n}:
        return b.oriented(terminals[0])
    return None


def add_wire(layout, net, *points):
    layout.wires.setdefault(net, []).extend(zip(points, points[1:]))


def label_node(layout, circuit, point, node):
    if not circuit.grouped or node in circuit.named_wires or node in ("0", "in"):
        layout.junctions.setdefault(node, []).append(point)
        layout.labels[node] = point


def ground_at(layout, point, name="0"):
    layout.grounds.append(point)
    if name != "0":
        layout.ground_names[point] = name
    layout.junctions.setdefault(name, []).append(point)


def place_tree(layout, circuit, block, x, y):
    if block.kind == "part":
        start, end = (x, y), (x + block.width, y)
        if block.p != block.part.p:
            start, end = end, start
        layout.symbols.append((block.part, start, end, "+"))
        layout.positions[block.part.name] = start
    elif block.kind == "series":
        for i, child in enumerate(block.children):
            place_tree(layout, circuit, child, x, y)
            x += child.width
            if i + 1 < len(block.children):
                label_node(layout, circuit, (x, y), child.n)
    else:
        top = y
        right = x + block.width
        for i, child in enumerate(block.children):
            left = x + (block.width - child.width) / 2
            add_wire(layout, block.p, (x, y), (left, y))
            place_tree(layout, circuit, child, left, y)
            add_wire(layout, block.n, (left + child.width, y), (right, y))
            if i + 1 < len(block.children):
                layout.junctions.setdefault(block.p, []).append((x, y))
                layout.junctions.setdefault(block.n, []).append((right, y))
                y -= child.depth + 2
        add_wire(layout, block.p, (x, top), (x, y))
        add_wire(layout, block.n, (right, top), (right, y))


def place_symbol(layout, part, first_net, start, end):
    a, b = (start, end) if part.p == first_net else (end, start)
    layout.symbols.append((part, a, b, "+"))
    layout.positions[part.name] = a


def grounded_bridge(parts, source):
    """Draw a passive bridge with two legs and a link between their midpoints."""
    if not source or len(parts) != 6 or (source.p == "0") == (source.n == "0"):
        return None
    supply = source.n if source.p == "0" else source.p
    branches = {}
    for part in parts:
        if part is source:
            continue
        terminals = frozenset((part.p, part.n))
        if (
            part.kind not in {"R", "C", "L", "X"}
            or len(terminals) != 2
            or terminals in branches
        ):
            return None
        branches[terminals] = part
    midpoints = [
        part.n if part.p == supply else part.p
        for edge, part in branches.items()
        if supply in edge and "0" not in edge
    ]
    if len(midpoints) != 2:
        return None
    left, right = midpoints
    left_x, right_x = 6.5, 14
    mid_y, ground_y = -3.6, -7.2
    placements = (
        (supply, left, (left_x, 0), (left_x, mid_y)),
        (left, "0", (left_x, mid_y), (left_x, ground_y)),
        (supply, right, (right_x, 0), (right_x, mid_y)),
        (right, "0", (right_x, mid_y), (right_x, ground_y)),
        (left, right, (left_x, mid_y), (right_x, mid_y)),
    )
    if any(frozenset((a, b)) not in branches for a, b, _, _ in placements):
        return None

    layout = Layout()
    place_symbol(layout, source, supply, (0, 0), (0, ground_y))
    for a, b, start, end in placements:
        place_symbol(layout, branches[frozenset((a, b))], a, start, end)
    add_wire(layout, supply, (0, 0), (left_x, 0), (right_x, 0))
    layout.junctions[supply] = [(left_x, 0)]
    layout.labels[supply] = ((left_x + right_x) / 2, 0)
    for node, x in ((left, left_x), (right, right_x)):
        layout.junctions[node] = [(x, mid_y)]
        layout.labels[node] = (x, mid_y)
    for x in (0, left_x, right_x):
        ground_at(layout, (x, ground_y))
    return layout


def _parallel_tees(circuit, source, labels):
    """Draw T sections with a shared input and output, with or without a load."""
    if source is None or (source.p in circuit.grounds) == (source.n in circuit.grounds):
        return None
    supply, ground = (
        (source.n, source.p) if source.p in circuit.grounds else (source.p, source.n)
    )
    branches = {}
    neighbors = {}
    for part in circuit.components:
        if part is source:
            continue
        edge = frozenset((part.p, part.n))
        if part.kind not in "RCLX" or len(edge) != 2 or edge in branches:
            return None
        branches[edge] = part
        neighbors.setdefault(part.p, set()).add(part.n)
        neighbors.setdefault(part.n, set()).add(part.p)

    taps = list(
        dict.fromkeys(
            part.n if part.p == supply else part.p
            for part in circuit.components
            if part is not source and supply in (part.p, part.n)
        )
    )
    if len(taps) < 2 or circuit.grounds.intersection(taps):
        return None
    legs = []
    used = set()
    output = None
    for tap in taps:
        returns = neighbors[tap] & circuit.grounds
        onward = neighbors[tap] - returns - {supply}
        if len(returns) != 1 or len(onward) != 1:
            return None
        (return_net,) = returns
        (next_net,) = onward
        if next_net in taps or (output is not None and next_net != output):
            return None
        output = next_net
        edges = [
            frozenset(pair)
            for pair in ((supply, tap), (tap, output), (tap, return_net))
        ]
        legs.append((tap, return_net, *(branches[edge] for edge in edges)))
        used.update(edges)
    loads = branches.keys() - used
    if len(loads) > 1:
        return None
    load = next(iter(loads), None)
    if load is not None and (
        output not in load or not (load - {output}) & circuit.grounds
    ):
        return None

    span = 6.5
    if labels:
        span = max(
            span,
            1
            + 0.40
            * max(
                min(len(line), 24) if i == 0 else len(line)
                for part in branches.values()
                for i, line in enumerate(labels[part.name])
            ),
        )
    left_x, tap_x = 3.0, 3.0 + span
    right_x = tap_x + span
    rows = [-5.8 * i for i in range(len(legs))]
    drawing = Layout()
    place_symbol(drawing, source, supply, (0, 0), (0, -3.3))
    ground_at(drawing, (0, -3.3), ground)
    drawing.labels[supply] = (0, 0)
    add_wire(drawing, supply, (0, 0), *((left_x, y) for y in rows))
    add_wire(drawing, output, *((right_x, y) for y in rows))
    drawing.junctions[supply] = [(left_x, y) for y in rows[:-1]]
    drawing.junctions[output] = [(right_x, y) for y in rows[1:]]

    for y, (tap, return_net, first, second, shunt) in zip(rows, legs):
        center = (tap_x, y)
        place_symbol(drawing, first, supply, (left_x, y), center)
        place_symbol(drawing, second, tap, center, (right_x, y))
        place_symbol(drawing, shunt, tap, center, (tap_x, y - 3.3))
        ground_at(drawing, (tap_x, y - 3.3), return_net)
        drawing.junctions[tap] = [center]
        drawing.labels[tap] = center

    bottom = (right_x, rows[-1])
    terminal = (right_x + 3.0, rows[-1])
    add_wire(drawing, output, bottom, terminal)
    drawing.connectors.append(
        (output, terminal, output.replace("_", " "), False, False)
    )
    if load is not None:
        (return_net,) = load - {output}
        end = (right_x, rows[-1] - 3.3)
        place_symbol(drawing, branches[load], output, bottom, end)
        ground_at(drawing, end, return_net)
    return drawing


def _branch_columns(block):
    if block.kind == "part":
        return 1
    spans = [_branch_columns(child) for child in block.children]
    return sum(spans) if block.kind == "parallel" else max(spans)


def _vertical_tree(layout, circuit, block, x, top, height, pitch):
    """Draw a series-parallel block downward, with local symbols at ground returns."""
    bottom = top - height
    if block.kind == "part":
        place_symbol(layout, block.part, block.p, (x, top), (x, bottom))
        if block.n in circuit.grounds:
            ground_at(layout, (x, bottom), block.n)
        return x
    if block.kind == "series":
        right = x
        for i, child in enumerate(block.children):
            child_height = height * child.width / block.width
            edge = _vertical_tree(layout, circuit, child, x, top, child_height, pitch)
            if i == 0:
                right = edge
            top -= child_height
            if i + 1 < len(block.children):
                label_node(layout, circuit, (x, top), child.n)
        return right

    branches = []
    for child in block.children:
        branches.append(x)
        right = _vertical_tree(layout, circuit, child, x, top, height, pitch)
        x += pitch * _branch_columns(child)
    add_wire(layout, block.p, *((x, top) for x in branches))
    layout.junctions.setdefault(block.p, []).extend((x, top) for x in branches[:-1])
    if block.n not in circuit.grounds:
        add_wire(layout, block.n, *((x, bottom) for x in branches))
        layout.junctions.setdefault(block.n, []).extend(
            (x, bottom) for x in branches[:-1]
        )
    return right


def _grounded_tree(circuit, source, tree, labels):
    ground, supply = (
        (source.p, source.n) if source.p in circuit.grounds else (source.n, source.p)
    )
    tree = tree.oriented(supply)
    if bank := _divider_bank(circuit, source, tree, ground):
        return bank
    *feed, load = tree.children if tree.kind == "series" else (tree,)
    # Leave room for the vertical component captions to the left of each leg.
    pitch = 5.8
    if labels:
        pitch = max(
            pitch,
            1.4
            + 0.42
            * max(
                min(len(line), 24) if i == 0 else len(line)
                for part in circuit.components
                if part is not source
                for i, line in enumerate(labels[part.name])
            ),
        )
    feed_depth = max((block.depth for block in feed), default=0)
    feed_width = sum(block.width for block in feed)
    layout = Layout()
    height = max(load.width, 3.6)
    place_symbol(layout, source, supply, (0, 0), (0, -height))
    ground_at(layout, (0, -height), ground)
    layout.labels[supply] = (0, 0)
    x = max(1.0, pitch - feed_width) if feed else 0.0
    if feed:
        add_wire(layout, supply, (0, 0), (x, 0))
    for block in feed:
        place_tree(layout, circuit, block, x, 0)
        x += block.width
        if block is not feed[-1]:
            label_node(layout, circuit, (x, 0), block.n)
    load_x = x + pitch if feed_depth else max(x, pitch)
    if x < load_x:
        add_wire(layout, load.p, (x, 0), (load_x, 0))
    right = _vertical_tree(layout, circuit, load, load_x, 0, height, pitch)
    if not load.p.startswith("@") and (feed or load.kind == "parallel"):
        terminal = (right + 2.5, 0)
        add_wire(layout, load.p, (right, 0), terminal)
        dots = layout.junctions.setdefault(load.p, [])
        if (right, 0) not in dots:
            dots.append((right, 0))
        layout.connectors.append(
            (load.p, terminal, load.p.replace("_", " "), False, False)
        )
    return layout


def _divider_bank(circuit, source, tree, ground):
    if tree.kind != "parallel" or any(
        branch.kind != "series"
        or any(child.kind != "part" for child in branch.children)
        or branch.children[-1].p not in circuit.named_wires
        for branch in tree.children
    ):
        return None

    drawing = Layout()
    supply = tree.p
    place_symbol(drawing, source, supply, (0, 0), (0, -3.6))
    ground_at(drawing, (0, -3.6), ground)
    drawing.labels[supply] = (0, 0)
    columns = [7.0 * (i + 1) for i in range(len(tree.children))]
    add_wire(drawing, supply, (0, 0), *((x, 0) for x in columns))
    drawing.junctions[supply] = [(x, 0) for x in columns[:-1]]
    for branch, x in zip(tree.children, columns):
        shunt = branch.children[-1]
        _vertical_tree(drawing, circuit, branch, x, 0, branch.width, 7.0)
        tap = (x, -branch.width + shunt.width)
        terminal = (x + 2.5, tap[1])
        add_wire(drawing, shunt.p, tap, terminal)
        drawing.connectors.append(
            (shunt.p, terminal, shunt.p.replace("_", " "), False, False)
        )
    return drawing


def compact(circuit, labels=None):
    """Choose a conventional layout when the resolved topology supports one."""
    parts = circuit.components
    if len(parts) > 12:
        return None
    if any(part.kind == "O" for part in parts):
        return compact_amplifier(circuit)
    source = next((p for p in parts if p.is_source), None)
    if source and not input_source_indices(parts):
        return None
    if bridge := grounded_bridge(parts, source):
        return bridge
    if tees := _parallel_tees(circuit, source, labels):
        return tees
    terminals = (source.p, source.n) if source else circuit.terminals
    if not terminals:
        return None
    tree = series_parallel([p for p in parts if p is not source], terminals)
    if not tree:
        return None
    if any(p.is_source for p in parts if p is not source):
        return None
    if (
        source
        and bool(set(terminals) & circuit.grounds)
        and all(part.kind in "RCLX" for part in parts if part is not source)
    ):
        return _grounded_tree(circuit, source, tree, labels)
    layout = Layout()
    place_tree(layout, circuit, tree, 1, 0)
    if not source:
        label_node(layout, circuit, (1, 0), terminals[0])
        label_node(layout, circuit, (tree.width + 1, 0), terminals[1])
    else:
        bottom, right = -tree.depth - 3.2, tree.width + 2
        add_wire(layout, source.p, (0, 0), (1, 0))
        add_wire(
            layout,
            source.n,
            (tree.width + 1, 0),
            (right, 0),
            (right, bottom),
            (0, bottom),
        )
        layout.positions[source.name] = (0, 0)
        layout.symbols.append((source, (0, 0), (0, bottom), "+"))
        label_node(layout, circuit, (0, 0), source.p)
        if source.n == "0":
            ground_at(layout, (right / 2, bottom))
        else:
            label_node(layout, circuit, (right / 2, bottom), source.n)
    return layout


def _paths(parts, start, terminals):
    visited = {start}
    while True:
        edges = [e for e in parts if e.p in visited or e.n in visited]
        reached = visited | (
            {node for e in edges for node in (e.p, e.n)} - set(terminals)
        )
        if reached == visited:
            return edges
        visited = reached


def compact_amplifier(circuit):
    parts = circuit.components
    sources = [e for e in parts if e.kind == "V" and e.is_source]
    amps = [e for e in parts if e.kind == "O"]
    if len(sources) > 1 and len(amps) == 1:
        return _summing_amplifier(circuit, sources, amps[0])
    first = [e for e in amps if e.in_plus == "0"]
    if len(parts) < 4 or len(sources) != 1 or len(first) != 1:
        return None
    if not input_source_indices(parts):
        return None
    source, chip = sources[0], first[0]
    terminals = [source.p, chip.in_minus, "0", *(e.p for e in amps)]
    if source.n != "0" or len(set(terminals)) != len(terminals):
        return None
    chain = []
    previous = chip.p
    for _ in range(len(amps) - 1):
        candidates = [
            e
            for e in amps
            if e is not chip and e.in_plus == previous and e.in_minus == e.p
        ]
        if len(candidates) != 1:
            return None
        chain.append(candidates[0])
        previous = chain[-1].p
    branches = [e for e in parts if e.kind not in "VO"]
    if any(e.kind not in "RCLX" for e in branches):
        return None
    inputs = _paths(branches, source.p, terminals)
    feedback = _paths(
        [e for e in branches if e not in inputs], chip.in_minus, terminals
    )
    input_tree = series_parallel(inputs, (source.p, chip.in_minus))
    feedback_tree = series_parallel(feedback, (chip.in_minus, chip.p))
    if not input_tree or not feedback_tree:
        return None
    loads = [e for e in branches if e not in inputs and e not in feedback]
    load_tree = None
    if loads:
        stage_nodes = set(terminals) | {
            node for e in inputs + feedback for node in (e.p, e.n)
        }
        nodes = list(dict.fromkeys(node for e in loads for node in (e.p, e.n)))
        if set(nodes) & (stage_nodes - {previous, "0"}):
            return None
        for n in ["0"] if "0" in nodes else [n for n in nodes if n != previous]:
            load_tree = series_parallel(loads, (previous, n))
            if load_tree:
                break
        if not load_tree:
            return None
    d = Layout()
    x = input_tree.width
    chip_x = x + 1.15
    out_x = max(chip_x + 4.7, x + feedback_tree.width + 0.8)
    fy = 2.25 + feedback_tree.depth
    gy = -3.3 - input_tree.depth
    place_tree(d, circuit, input_tree, 0, 0.35)
    d.positions[source.name] = (0, 0.35)
    d.symbols.append((source, (0, 0.35), (0, gy), "+"))
    d.positions[chip.name] = (chip_x, 0)
    d.symbols.append((chip, (chip_x, 0), (chip_x + 4.3, 0), "-"))
    add_wire(d, chip.in_minus, (x, 0.35), (chip_x, 0.35))
    add_wire(d, chip.in_minus, (x, 0.35), (x, fy))
    place_tree(d, circuit, feedback_tree, x, fy)
    add_wire(
        d,
        chip.p,
        (x + feedback_tree.width, fy),
        (out_x, fy),
        (out_x, 0),
        (chip_x + 4.3, 0),
    )
    add_wire(
        d, "0", (chip_x, -0.35), (chip_x - 0.55, -0.35), (chip_x - 0.55, gy), (0, gy)
    )
    ground_at(d, (chip_x - 0.55, gy))
    label_node(d, circuit, (0, 0.35), source.p)
    label_node(d, circuit, (x, 0.35), chip.in_minus)
    d.junctions.setdefault(chip.p, []).append((out_x, 0))
    for follower in chain:
        nx = out_x + 2.4
        add_wire(d, chip.p, (out_x, 0), (nx - 0.65, 0), (nx - 0.65, 0.35), (nx, 0.35))
        d.labels[chip.p] = (out_x + 0.4, 0.22)
        d.positions[follower.name] = (nx, 0)
        d.symbols.append((follower, (nx, 0), (nx + 4.3, 0), "+"))
        out_x = nx + 4.8
        add_wire(d, follower.p, (nx + 4.3, 0), (out_x, 0))
        add_wire(
            d,
            follower.p,
            (out_x, 0),
            (out_x, -1.65),
            (nx - 0.55, -1.65),
            (nx - 0.55, -0.35),
            (nx, -0.35),
        )
        d.junctions.setdefault(follower.p, []).append((out_x, 0))
        chip = follower
    if not load_tree:
        add_wire(d, chip.p, (out_x, 0), (out_x + 0.9, 0))
        d.labels[chip.p] = (out_x + 1.05, 0)
    else:
        lx, ly = out_x + 1.2, -2.6
        add_wire(d, chip.p, (out_x, 0), (lx, 0), (lx, ly))
        d.labels[chip.p] = (lx, 0.27)
        place_tree(d, circuit, load_tree, lx, ly)
        end = lx + load_tree.width
        if load_tree.n == "0":
            add_wire(d, "0", (end, ly), (end + 0.55, ly), (end + 0.55, ly - 0.85))
            ground_at(d, (end + 0.55, ly - 0.85))
        else:
            add_wire(d, load_tree.n, (end, ly), (end + 0.65, ly))
            d.labels[load_tree.n] = (end + 0.8, ly)
    return d


def _summing_amplifier(circuit, sources, amp):
    """Draw independent input legs around one inverting summing junction."""
    ground, tap, output = amp.in_plus, amp.in_minus, amp.p
    inputs = {part.p for part in sources}
    if (
        len(input_source_indices(circuit.components)) != len(sources)
        or ground not in circuit.grounds
        or amp.n != ground
        or len(inputs) != len(sources)
        or len(inputs | {tap, output, ground}) != len(inputs) + 3
        or any(source.n != ground for source in sources)
    ):
        return None
    passive = [part for part in circuit.components if part.kind in "RCLX"]
    if len(passive) + len(sources) + 1 != len(circuit.components):
        return None
    legs = []
    for source in sources:
        matches = [part for part in passive if {part.p, part.n} == {source.p, tap}]
        if len(matches) != 1:
            return None
        legs.append(matches[0])
    remaining = [part for part in passive if part not in legs]
    feedback = [part for part in remaining if {part.p, part.n} == {tap, output}]
    loads = [part for part in remaining if {part.p, part.n} == {output, ground}]
    if (
        len(feedback) != 1
        or len(loads) > 1
        or len(feedback) + len(loads) != len(remaining)
    ):
        return None

    drawing = Layout()
    sum_x, amp_x, out_x = 8.0, 12.4, 19.1
    rows = [-5.3 * i for i in range(len(sources))]
    for source, leg, y in zip(sources, legs, rows):
        place_symbol(drawing, source, source.p, (0, y), (0, y - 2.9))
        ground_at(drawing, (0, y - 2.9), ground)
        add_wire(drawing, source.p, (0, y), (1.4, y))
        place_symbol(drawing, leg, source.p, (1.4, y), (sum_x, y))
        drawing.labels[source.p] = (0, y)
    add_wire(drawing, tap, (sum_x, 3.0), *((sum_x, y) for y in rows))
    drawing.junctions[tap] = [(sum_x, y) for y in rows[:-1]]
    drawing.labels[tap] = (sum_x + 1.0, 0)
    add_wire(
        drawing, tap, (sum_x, 0), (amp_x - 1.4, 0), (amp_x - 1.4, -0.65), (amp_x, -0.65)
    )
    drawing.symbols.append((amp, (amp_x, -1), (amp_x + 4.3, -1), "-"))
    drawing.positions[amp.name] = (amp_x, -1)
    add_wire(
        drawing, ground, (amp_x, -1.35), (amp_x - 0.85, -1.35), (amp_x - 0.85, -3.6)
    )
    ground_at(drawing, (amp_x - 0.85, -3.6), ground)
    place_symbol(drawing, feedback[0], tap, (sum_x, 3.0), (out_x, 3.0))
    end = (out_x + 2.5, -1)
    add_wire(drawing, output, (out_x, 3.0), (out_x, -1), (amp_x + 4.3, -1))
    add_wire(drawing, output, (out_x, -1), end)
    drawing.junctions[output] = [(out_x, -1)]
    drawing.connectors.append((output, end, output.replace("_", " "), False, False))
    if loads:
        place_symbol(drawing, loads[0], output, (out_x, -1), (out_x, -4.6))
        ground_at(drawing, (out_x, -4.6), ground)
    return drawing


def route(circuit, labels):
    """Reproduce the historical MATLAB geometry for existing callers."""
    from .legacy_layout import route as legacy_route

    return legacy_route(circuit, labels)
