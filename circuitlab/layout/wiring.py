"""Readable views of a resolved circuit graph."""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import groupby

from circuitlab.layout.geometry import Layout, add_wire, component_pins, grid_symbol, ground_at, input_source_indices, placement_order
from circuitlab.layout.automatic import series_runs, source_cells


def connector_names(nets):
    """Keep readable labels without collisions between distinct electrical nets."""
    names = {}
    reserved = Counter(net.casefold() for net in nets)
    alias = 0
    for net in nets:
        if (
            "." in net
            or net.startswith("@")
            or len(net) > 16
            or reserved[net.casefold()] > 1
        ):
            while True:
                alias += 1
                short = f"N{alias}"
                if short.casefold() not in reserved:
                    break
            names[net] = short
        else:
            names[net] = net
    return names


def labeled(circuit, labels):
    """Use local ground symbols and matching names to connect distant pins."""
    parts = circuit.components
    layout = Layout()
    if not parts:
        return layout
    runs, reversed_parts = series_runs(circuit)
    pins = component_pins(parts, reversed_parts=reversed_parts)
    columns = math.ceil(math.sqrt(len(parts) * 0.75))
    order = placement_order(
        parts,
        pins,
        circuit.grounds,
        sorted(input_source_indices(parts), key=lambda i: parts[i].order),
    )
    cells = source_cells(circuit, order, columns, runs)
    nets = defaultdict(list)
    for pin in pins:
        nets[pin[1]].append(pin)
    names = connector_names(nets)

    label_width = max(len(line) for lines in labels.values() for line in lines)
    pitch = max(11, 6.1 + max(map(len, names.values()), default=0) * 0.7, label_width * 0.55 + 1)
    row_pitch = max(5.6, max((abs(p[3]) for p in pins), default=0) * 2 + 3)
    if any(part.kind == "H" for part in parts):
        pitch += 2.5
        row_pitch += 0.7
    for i, part in enumerate(parts):
        row, col = cells[i]
        symbol = grid_symbol(
            part, col * pitch, -row * row_pitch, circuit.grounds, reverse=i in reversed_parts
        )
        layout.positions[part.name] = symbol[1]
        layout.symbols.append(symbol)

    def terminal(pin):
        i, _, left, offset = pin
        if parts[i].is_source:
            return layout.symbols[i][1 if left else 2]
        _, a, b, _ = layout.symbols[i]
        return (min(a[0], b[0]) if left else max(a[0], b[0]), a[1] + offset)

    for net, group in nets.items():
        if len(group) == 2 and net not in circuit.grounds:
            first, second = sorted(group, key=lambda p: cells[p[0]])
            r1, c1 = cells[first[0]]
            r2, c2 = cells[second[0]]
            if (
                r1 == r2
                and c2 == c1 + 1
                and not first[2]
                and second[2]
                and first[3] == second[3]
                and not parts[first[0]].is_source
                and not parts[second[0]].is_source
            ):
                a, b = terminal(first), terminal(second)
                add_wire(layout, net, a, b)
                layout.labels[net] = ((a[0] + b[0]) / 2, a[1])
                continue
        for pin in group:
            a = terminal(pin)
            if net in circuit.grounds and parts[pin[0]].is_source:
                lower = min(point[1] for point in layout.symbols[pin[0]][1:3])
                b = (a[0], a[1] - 0.3) if a[1] == lower else (a[0] + 0.72, a[1])
                add_wire(layout, net, a, b)
                ground_at(layout, b, net)
                continue
            left = pin[2] and not parts[pin[0]].is_source
            b = (a[0] + (-0.72 if left else 0.72), a[1])
            if net in circuit.grounds and left and pin[3] > 0:
                # Keep the ground symbol outside the other input's approach.
                b = (b[0] - 0.95, b[1])
            add_wire(layout, net, a, b)
            if net in circuit.grounds:
                ground_at(layout, b, net)
            else:
                below = pin[3] < 0 and parts[pin[0]].kind != "H"
                layout.connectors.append((net, b, names[net], left, below))
    return layout


@dataclass(frozen=True)
class Stage:
    """One buffered stage with its input component and shunt branches."""

    amp: object
    feed: object
    shunts: tuple
    input: str


def signal_flow(circuit):
    """Draw trees of buffered stages with one feed and parallel shunts."""
    parts = circuit.components
    sources = [p for p in parts if p.kind == "V" and p.n in circuit.grounds]
    amps = [p for p in parts if p.kind == "O"]
    if len(sources) != 1 or not amps:
        return None
    source = sources[0]
    if any(
        part.order and source.order > part.order for part in parts if part is not source
    ):
        return None
    ground = source.n
    drivers = {source.p, *(p.p for p in amps)}
    taps = {p.in_plus for p in amps}
    if (
        len(drivers) != len(amps) + 1
        or len(taps) != len(amps)
        or bool(circuit.grounds & (drivers | taps))
        or drivers & taps
        or any(p.in_minus != p.p or p.n not in circuit.grounds for p in amps)
    ):
        return None

    by_node = defaultdict(list)
    for part in parts:
        if part.kind in {"R", "C", "L", "X"} and part.p != part.n:
            by_node[part.p].append((part.n, part))
            by_node[part.n].append((part.p, part))

    children = defaultdict(list)
    used = {source, *amps}
    for amp in amps:
        connections = by_node[amp.in_plus]
        shunts = tuple(
            (node, part) for node, part in connections if node in circuit.grounds
        )
        feeds = [(node, part) for node, part in connections if node in drivers]
        if not shunts or len(feeds) != 1 or len(connections) != len(shunts) + 1:
            return None
        incoming, feed = feeds[0]
        used.add(feed)
        used.update(part for _, part in shunts)
        children[incoming].append(Stage(amp, feed, shunts, incoming))

    # Each load is a shunt or a series element followed by a shunt to ground.
    loads = {}
    for node in [source.p, *(amp.p for amp in amps)]:
        remaining = [(other, part) for other, part in by_node[node] if part not in used]
        if not remaining:
            continue
        if len(remaining) != 1:
            return None
        other, part = remaining[0]
        if other in circuit.grounds:
            loads[node] = (None, part, node, other)
            used.add(part)
        elif other not in drivers | taps:
            tail = [(net, p) for net, p in by_node[other] if p != part]
            if len(tail) != 1 or tail[0][0] not in circuit.grounds:
                return None
            return_net, shunt = tail[0]
            loads[node] = (part, shunt, other, return_net)
            used.update((part, shunt))
    if used != set(parts) or any(children[node] for node in loads):
        return None

    coordinates = {}
    leaf_rows = 0

    def place(node, depth):
        nonlocal leaf_rows
        start = leaf_rows
        for stage in children[node]:
            place(stage.amp.p, depth + 1)
        if leaf_rows == start:
            leaf_rows += 1
        coordinates[node] = (depth, -(start + leaf_rows - 1) * 4.1)

    # A cycle cannot be reachable from this root because every amp has one feed.
    place(source.p, 0)
    if set(coordinates) != drivers:
        return None

    layout = Layout()
    shunt_spacing = 4.3
    stage_pitch = 15 + shunt_spacing * (
        max(len(stage.shunts) for group in children.values() for stage in group) - 1
    )

    def feed_x(depth):
        return 3 + (depth - 1) * stage_pitch

    def symbol(part, positive, a, b, top="+"):
        if part.p != positive:
            a, b = b, a
        layout.symbols.append((part, a, b, top))
        layout.positions[part.name] = a

    def shunt(part, node, x, y, return_net):
        symbol(part, node, (x, y), (x, y - 3.2))
        ground_at(layout, (x, y - 3.2), return_net)

    def caption(net, anchor):
        if "." not in net and not net.startswith("@"):
            layout.labels[net] = anchor

    source_y = coordinates[source.p][1]
    if len(children[source.p]) > 1:
        source_y = max(coordinates[stage.amp.p][1] for stage in children[source.p]) + 3.2
    shunt(source, source.p, 0, source_y, ground)
    caption(source.p, (0, source_y))
    outputs = {source.p: (0, source_y)}
    for group in children.values():
        for stage in group:
            amp = stage.amp
            depth, y = coordinates[amp.p]
            x = feed_x(depth)
            tap = amp.in_plus
            first = x + 4
            symbol(stage.feed, stage.input, (x, y), (first, y))
            ground_y = y - 3.2
            positions = [first + i * shunt_spacing for i in range(len(stage.shunts))]
            for (_, part), xx in zip(stage.shunts, positions):
                symbol(part, tap, (xx, y), (xx, ground_y))
            for return_net, branches in groupby(
                zip(stage.shunts, positions), key=lambda branch: branch[0][0]
            ):
                rail = [(xx, ground_y) for _, xx in branches]
                if len(rail) > 1:
                    add_wire(layout, return_net, *rail)
                ground_at(layout, rail[0], return_net)
                layout.junctions[return_net].extend(rail[1:-1])
            if len(positions) > 1:
                add_wire(layout, tap, *((xx, y) for xx in positions))
            layout.junctions.setdefault(tap, []).extend((xx, y) for xx in positions)
            last = positions[-1]
            caption(tap, (first, y))
            add_wire(
                layout,
                tap,
                (last, y),
                (last + 1, y),
                (last + 1, y - 0.35),
                (last + 2, y - 0.35),
            )
            symbol(amp, amp.p, (last + 2, y), (last + 6.3, y), "-")
            output = (last + 7.2, y)
            add_wire(
                layout,
                amp.p,
                (last + 6.3, y),
                output,
                (output[0], y + 2.6),
                (last + 1.5, y + 2.6),
                (last + 1.5, y + 0.35),
                (last + 2, y + 0.35),
            )
            layout.junctions.setdefault(amp.p, []).append(output)
            outputs[amp.p] = output

    for node, (x, y) in outputs.items():
        next_stages = children[node]
        if next_stages:
            rail = x + 0.85
            ends = sorted({y, *(coordinates[s.amp.p][1] for s in next_stages)})
            add_wire(layout, node, (x, y), (rail, y))
            add_wire(layout, node, *((rail, yy) for yy in ends))
            for stage in next_stages:
                depth, yy = coordinates[stage.amp.p]
                add_wire(layout, node, (rail, yy), (feed_x(depth), yy))
            # An interior intersection is a T. At a rail end the wires bend.
            layout.junctions.setdefault(node, []).extend(
                (rail, yy) for yy in ends[1:-1]
            )
            if (
                len(next_stages) > 1
                and any(coordinates[s.amp.p][1] == y for s in next_stages)
                and y in {ends[0], ends[-1]}
            ):
                layout.junctions.setdefault(node, []).append((rail, y))
            if node != source.p:
                caption(node, (rail, y))
        elif node in loads:
            feed, load, output, return_net = loads[node]
            if feed:
                add_wire(layout, node, (x, y), (x + 3.4, y))
                caption(node, (x + 1.1, y))
                symbol(feed, node, (x + 3.4, y), (x + 7.4, y))
                x += 7.4
            shunt(load, output, x, y, return_net)
            add_wire(layout, output, (x, y), (x + 2, y))
            layout.junctions.setdefault(output, []).append((x, y))
            caption(output, (x + 2, y))
        else:
            add_wire(layout, node, (x, y), (x + 1.5, y))
            caption(node, (x + 1.5, y))
    return layout
