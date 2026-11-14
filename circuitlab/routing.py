"""Place and route arbitrary circuit graphs with orthogonal wire channels."""

import math
from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import replace
from statistics import median_low

from .layout import (
    BJT_PIN_OFFSET,
    Layout,
    PACKAGE_WIDTH,
    add_wire,
    along,
    compact,
    component_pins,
    grid_symbol,
    ground_at,
    input_source_indices,
    rotate_point,
    series_runs,
    source_cells,
    source_pin_is_upper,
    symbol_pins,
    wire_axis,
)
from .layout_checks import intersection, preserves_nets, segments_intersect
from .layout_search import evolve_placements, feedback_pairs, placement_penalty
from .wiring import connector_names, labeled, signal_flow
from .wire_edit import (
    clean_point, replace_route, route_anchors, terminal_run, wire_graph, wire_slide_candidates,
)

SYMBOL_WIDTH = 4.3
LANE_PITCH = 0.46
TRACK_PITCH = 0.96


def schematic_layout(circuit, labels, mode):
    """Choose a layout and check that its visible connections preserve the graph."""
    if mode == "labeled":
        return labeled(circuit, labels)
    if len(circuit.components) > 64 or any(part.kind == "H" for part in circuit.components):
        return fast_layout(circuit, labels)
    conventional = compact(circuit, labels) or signal_flow(circuit)
    if conventional and preserves_nets(circuit, conventional):
        return conventional
    layout = connected_layout(circuit, labels, simplify=mode == "auto")
    return layout if preserves_nets(circuit, layout) else labeled(circuit, labels)


def fast_layout(circuit, labels, *, boundary=()):
    """Route a small fixed set of placements and skip iterative refinement."""
    parts = circuit.components
    if not parts:
        return Layout()
    runs, reversed_parts = series_runs(circuit)
    pins = component_pins(parts, reversed_parts=reversed_parts)
    if not pins:
        return labeled(circuit, labels)
    order = next(_orders(parts, pins, circuit.grounds))
    center = math.ceil(math.sqrt(len(parts) * 0.72))
    widths = (center,) if len(parts) > 32 else range(max(1, center - 1), center + 2)
    sources = [i for i in order if parts[i].is_source]
    others = [i for i in order if not parts[i].is_source]
    candidates = []
    for width in widths:
        cells = source_cells(circuit, order, width, runs)
        candidates.append(cells)
        if len(parts) <= 32:
            compact_cells = {
                i: divmod(j, width) for j, i in enumerate(sources + others)
            }
            candidates.append(compact_cells)
    best = None
    for regular in (False, True):
        for cells in candidates:
            layout = _channel_layout(
                circuit, labels, pins, set(), reversed_parts, cells, set(), set(),
                tight=True, regular=regular, boundary=boundary,
            )
            _mark_crossings(layout)
            cost = _layout_cost(layout)
            if best is not None and cost >= best[0]:
                continue
            if preserves_nets(circuit, layout):
                best = cost, layout
        if best is not None:
            return best[1]
    return labeled(circuit, labels)


def move_symbol(circuit, original, name, delta, *, diagonal=False, rotate=0, align_axis=None):
    """Move or rotate one symbol and reroute the leads to its pins."""
    drawing = replace(
        original,
        symbols=list(original.symbols),
        positions=dict(original.positions),
        wires={net: list(edges) for net, edges in original.wires.items()},
        junctions={net: list(dots) for net, dots in original.junctions.items()},
        labels=dict(original.labels),
        crossings={},
        rotations=dict(original.rotations),
    )
    i, symbol = next(
        (i, symbol) for i, symbol in enumerate(drawing.symbols)
        if symbol[0].name == name
    )
    part, a, b, top = symbol

    def shift(point):
        return tuple(round(coordinate + change, 8) for coordinate, change in zip(point, delta))

    moved = part, shift(a), shift(b), top
    previous_turns = original.rotations.get(name, 0)
    turns = (previous_turns + rotate) % 4
    if moved == symbol and not rotate and align_axis is None:
        return original
    if turns:
        drawing.rotations[name] = turns
    else:
        drawing.rotations.pop(name, None)

    def bounds(symbol, turns):
        part, a, b, _ = symbol
        height = (
            max(0.8, (len(part.package_pins) + 1) // 2 / 2 + 0.2)
            if part.kind == "H" else 0.9 if part.kind in {"Q", "O"} else 0.45
        )
        return (
            rotate_point((min(a[0], b[0]) - 0.45, min(a[1], b[1]) - height), a, b, turns),
            rotate_point((max(a[0], b[0]) + 0.45, max(a[1], b[1]) + height), a, b, turns),
        )

    body = bounds(moved, turns)
    if any(
        boxes_overlap(body, bounds(other, original.rotations.get(other[0].name, 0)))
        for j, other in enumerate(drawing.symbols) if j != i
    ):
        return None
    drawing.symbols[i] = moved
    drawing.positions[name] = moved[1]

    return _reroute_move(
        circuit, original, drawing,
        _routed_pins(symbol, previous_turns), _routed_pins(moved, turns),
        (a[0] + b[0]) / 2,
        stationary_pins=[
            pin for j, placed in enumerate(original.symbols) if j != i
            for pin in _routed_pins(placed, original.rotations.get(placed[0].name, 0))
        ],
        diagonal=diagonal, rotate=rotate, align_axis=align_axis,
    )


def _routed_pins(symbol, turns):
    _, a, b, _ = symbol
    return tuple(
        (net, rotate_point(pin, a, b, turns)) for net, pin in symbol_pins(*symbol)
    )


def symbol_wire_alignments(drawing, name):
    """Find offsets that place the symbol's pins on their connected wire runs."""
    symbol = next(symbol for symbol in drawing.symbols if symbol[0].name == name)
    _, a, b, _ = symbol
    turns = drawing.rotations.get(name, 0)
    axis = wire_axis((a, b))
    if axis is None:
        return None, {}
    axis = (axis + turns) % 2
    pins = list(drawing.pins())
    moving_pins = _routed_pins(symbol, turns)
    for pin in moving_pins:
        pins.remove(pin)
    return 1 - axis, _terminal_wire_alignments(drawing, moving_pins, axis, pins)


def ground_wire_alignments(drawing, point):
    """Align a ground with the run or terminal that feeds its lead."""
    net = drawing.ground_names.get(point, "0")
    axis = 1 - drawing.ground_rotations.get(point, 0) % 2
    return 1 - axis, _terminal_wire_alignments(
        drawing, ((net, point),), axis, moving_ground=point
    )


def ground_attachment(drawing, point):
    net = drawing.ground_names.get(point, "0")
    fixed = route_anchors(drawing, net, moving_ground=point)
    graph = wire_graph(drawing.wires.get(net, ()), route_anchors(drawing, net))
    return terminal_run(graph, clean_point(point), fixed)[-1]


def _terminal_wire_alignments(drawing, moving_pins, axis, stationary_pins=None, *, moving_ground=None):
    coordinate = 1 - axis
    nets = {net for net, _ in moving_pins}
    fixed = {
        net: route_anchors(drawing, net, stationary_pins, moving_ground=moving_ground)
        for net in nets
    }
    graphs = {
        net: wire_graph(drawing.wires.get(net, ()), route_anchors(drawing, net))
        for net in nets
    }
    offsets = Counter()
    for net, pin in moving_pins:
        pin = clean_point(pin)
        graph = graphs[net]
        run = terminal_run(graph, pin, fixed[net])
        # A tee can have a straight continuation one corner past the end of the lead.
        junction = run[-1]
        edges = list(zip(run, run[1:]))
        for neighbor in graph.get(junction, ()):
            edges.append((junction, neighbor))
            if wire_axis((junction, neighbor)) == coordinate:
                edges.extend((neighbor, point) for point in graph[neighbor])
        targets = {start[coordinate] for start, end in edges if wire_axis((start, end)) == axis}
        offsets.update({round(target - pin[coordinate], 8) for target in targets})
        if moving_ground is not None and junction in fixed[net]:
            # Prefer the source pin over vertical stubs introduced by previous moves.
            offsets[round(junction[coordinate] - pin[coordinate], 8)] = 2
    return offsets


def move_ground(circuit, original, point, delta, *, diagonal=False, align_axis=None):
    """Move one ground symbol and reconnect its existing branch."""
    end = clean_point((point[0] + delta[0], point[1] + delta[1]))
    if end == clean_point(point) and align_axis is None:
        return original
    if any(other != point and end == clean_point(other) for other in original.grounds):
        return None
    net = original.ground_names.get(point, "0")
    drawing = replace(
        original,
        grounds=[end if pos == point else pos for pos in original.grounds],
        ground_names=dict(original.ground_names),
        ground_rotations=dict(original.ground_rotations),
    )
    if point in drawing.ground_names:
        drawing.ground_names[end] = drawing.ground_names.pop(point)
    if point in drawing.ground_rotations:
        drawing.ground_rotations[end] = drawing.ground_rotations.pop(point)
    return _reroute_move(
        circuit, original, drawing, ((net, point),), ((net, end),), point[0],
        moving_ground=point, diagonal=diagonal, align_axis=align_axis,
        direct=end == ground_attachment(original, point),
    )


def move_connector(circuit, original, index, end, *, join=None, diagonal=False):
    """Move a labeled wire end, optionally joining it to a matching end."""
    net, start, name, left, below = original.connectors[index]
    end = clean_point(end)
    if join is None and end == clean_point(start):
        return original
    if join is not None:
        other_net, other_end, other_name, *_ = original.connectors[join]
        if index == join or net != other_net or name != other_name or end != clean_point(other_end):
            return None
    if join is None:
        ports = list(original.connectors)
        ports[index] = net, end, name, left, below
    else:
        ports = [port for i, port in enumerate(original.connectors) if i not in (index, join)]
    drawing = _reroute_move(
        circuit, original, replace(original, connectors=ports),
        ((net, start),), ((net, end),), start[0], moving_connector=index, diagonal=diagonal,
    )
    if drawing is None and join is not None and any(port[0] == net for port in ports):
        # A third disconnected end still needs a label on the newly joined run.
        drawing = _reroute_move(
            circuit, original, replace(original, connectors=[*ports, original.connectors[join]]),
            ((net, start),), ((net, end),), start[0], moving_connector=index, diagonal=diagonal,
        )
    return drawing


def split_wire(circuit, original, net, edge, at):
    """Replace a short span of wire with two matching labeled ends."""
    a, b = sorted(map(clean_point, edge))
    if a == b or net in circuit.grounds:
        return None
    anchors = route_anchors(original, net) | {a, b}
    graph = wire_graph(original.wires[net], anchors)
    runs = [
        (p, q) for p, neighbors in graph.items() for q in neighbors if p < q
        and math.dist(p, q) >= 0.25
        and all(segments_intersect((point, point), (a, b)) for point in (p, q))
    ]
    if not runs:
        return None
    axis = max(range(2), key=lambda i: abs(b[i] - a[i]))
    a, b = min(
        runs,
        key=lambda run: max(0, min(p[axis] for p in run) - at[axis],
                            at[axis] - max(p[axis] for p in run)),
    )
    length = math.dist(a, b)
    direction = tuple((v - u) / length for u, v in zip(a, b))
    along_run = sum((coordinate - origin) * step for coordinate, origin, step in zip(at, a, direction))
    margin = min(0.6, length / 4)
    center = max(margin, min(length - margin, along_run))
    low, high = (
        clean_point(along(a, b, (center + side * margin / 2) / length))
        for side in (-1, 1)
    )
    graph[a].remove(b)
    graph[b].remove(a)
    existing = {wire: caption for wire, _, caption, *_ in original.connectors}
    name = existing.get(net)
    if name is None:
        used = {caption.casefold() for caption in existing.values()}
        name = connector_names((net,))[net]
        suffix = 1
        while name.casefold() in used:
            name = f"N{suffix}"
            suffix += 1
    drawing = replace(
        original, wires=dict(original.wires), junctions=dict(original.junctions),
        labels=dict(original.labels), crossings={},
        connectors=[*original.connectors, (net, low, name, True, False),
                    (net, high, name, False, True)],
    )
    edges = [(p, q) for p, neighbors in graph.items() for q in neighbors if p < q]
    replace_route(drawing, net, [*edges, (a, low), (high, b)])
    _mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


def _reroute_move(
    circuit, original, drawing, old_pins, new_pins, center_x, *,
    stationary_pins=None, moving_ground=None, moving_connector=None,
    diagonal=False, rotate=0, align_axis=None, direct=False,
):
    affected = {net for net, _ in old_pins}
    # Attach at the first fixed corner, branch, or terminal. The old lead
    # is then shortened or pruned instead of leaving a stub at the old pin.
    fixed_anchors = {
        net: route_anchors(
            original, net, stationary_pins,
            moving_ground=moving_ground, moving_connector=moving_connector,
        )
        for net in affected
    }
    graphs = {
        net: wire_graph(original.wires.get(net, ()), route_anchors(original, net))
        for net in affected
    }
    connections = []
    for index, ((net, start), (_, end)) in enumerate(zip(old_pins, new_pins)):
        start, end = clean_point(start), clean_point(end)
        graph = graphs[net]
        fixed = fixed_anchors[net] | {
            clean_point(pin) for i, (other, pin) in enumerate(old_pins)
            if i != index and other == net
        }
        run = terminal_run(graph, start, fixed)
        # Keep the first bend, unless a later point can meet the pin in a straight line.
        stop = next((
            i for i in range(1, len(run) - 1)
            if not segments_intersect((run[i], run[i]), (run[i - 1], run[i + 1]))
        ), len(run) - 1)
        if direct:
            stop = len(run) - 1
        elif align_axis is not None:
            aligned = next((
                i for i in range(stop, len(run)) if run[i][align_axis] == end[align_axis]
            ), None)
            if aligned is not None:
                stop = aligned
        attachment = run[stop]
        if align_axis is not None and attachment[align_axis] != end[align_axis]:
            junction = run[-1]
            for neighbor in graph.get(junction, ()):
                if (
                    wire_axis((junction, neighbor)) == align_axis
                    and neighbor[align_axis] == end[align_axis]
                    and any(
                        wire_axis((neighbor, point)) == 1 - align_axis
                        for point in graph[neighbor]
                    )
                ):
                    stop, attachment = len(run) - 1, neighbor
                    break
        for previous, neighbor in zip(run[:stop], run[1:stop + 1]):
            graph[previous].remove(neighbor)
            graph[neighbor].remove(previous)
        connections.append((net, attachment, end))
    fixed_edges = {
        net: [(a, b) for a, neighbors in graph.items() for b in neighbors if a < b]
        for net, graph in graphs.items()
    }
    bends = (
        ("x", 0.5), ("y", 0.5), ("x", 0), ("y", 0),
        ("x", 1), ("y", 1), ("x", 0.25), ("y", 0.25),
        ("outside", 0.35), ("outside", 0.6),
    )
    if diagonal:
        bends = (("diagonal", 0), *bends)
    if rotate:
        bends += (("diagonal", 0), ("around", 0.7), ("around", 1.3))
    best = _shortest_move(drawing, circuit, affected, connections, fixed_edges, bends, center_x)
    if best is not None:
        return best
    # Preserve the old junction geometry if trimming leaves no valid route.
    connections = [
        (net, clean_point(start), clean_point(end))
        for (net, start), (_, end) in zip(old_pins, new_pins)
    ]
    return _shortest_move(
        drawing, circuit, affected, connections, original.wires, bends, center_x
    )


def boxes_overlap(first, second):
    return all(
        max(min(a, b), min(c, d)) <= min(max(a, b), max(c, d))
        for (a, b), (c, d) in zip(zip(*first), zip(*second))
    )


def _shortest_move(drawing, circuit, affected, connections, fixed_edges, bends, center_x):
    best = None
    for axis, ratio in bends:
        routed = replace(
            drawing,
            wires={net: list(edges) for net, edges in drawing.wires.items()},
            junctions={net: list(dots) for net, dots in drawing.junctions.items()},
            labels=dict(drawing.labels), crossings={},
        )
        for net in affected:
            routed.wires[net] = list(fixed_edges.get(net, ()))
        for index, (net, start, end) in enumerate(connections):
            x, y = start
            u, v = end
            if axis == "diagonal":
                points = (start, end)
            elif axis == "around":
                approach = min(x, u) - 0.6 if x <= center_x else max(x, u) + 0.6
                exit_lane = min(x, u) - 0.4 if u <= center_x else max(x, u) + 0.4
                across = min(y, v) - ratio if index % 2 == 0 else max(y, v) + ratio
                points = (
                    start, (approach, y), (approach, across),
                    (exit_lane, across), (exit_lane, v), end,
                )
            elif axis == "y":
                lane = y + (v - y) * ratio
                points = (start, (x, lane), (u, lane), end)
            else:
                lane = x + (u - x) * ratio
                if axis == "outside":
                    spacing = 0.5 + index * ratio
                    lane = min(x, u) - spacing if x <= center_x else max(x, u) + spacing
                points = (start, (lane, y), (lane, v), end)
            for first, second in zip(points, points[1:]):
                if first != second:
                    add_wire(routed, net, *sorted((first, second)))
        for net in affected:
            replace_route(routed, net, routed.wires[net])
        length = sum(
            math.dist(a, b) for net in affected for a, b in routed.wires.get(net, ())
        )
        if best is not None and length >= best[0] - 1e-8:
            continue
        _mark_crossings(routed)
        if preserves_nets(circuit, routed):
            best = length, routed
    return best[1] if best else None


def move_wire(circuit, original, net, edge, distance, *, snap=0):
    """Return the first electrically valid slide and its snapped offset, if any."""
    for drawing, offset in wire_slide_candidates(original, net, edge, distance, snap=snap):
        if drawing is original:
            return drawing, None
        _mark_crossings(drawing)
        if preserves_nets(circuit, drawing):
            return drawing, offset
    return None, None


def _orders(parts, pins, grounds):
    pin_nets = [set() for _ in parts]
    owners = defaultdict(set)
    for i, net, *_ in pins:
        if net not in grounds:
            pin_nets[i].add(net)
            owners[net].add(i)
    sources = sorted(input_source_indices(parts), key=lambda i: parts[i].order)
    roots = [*sources, *range(len(parts))]
    for depth_first in (True, False):
        order, seen, used_nets = [], set(), set()
        for root in roots:
            pending = deque([root])
            while pending:
                i = pending.popleft()
                if i in seen:
                    continue
                order.append(i)
                seen.add(i)
                neighbors = []
                for net in sorted(pin_nets[i], key=lambda n: (len(owners[n]), n)):
                    if net not in used_nets:
                        used_nets.add(net)
                        neighbors.extend(sorted(owners[net] - seen))
                if depth_first:
                    pending.extendleft(reversed(neighbors))
                else:
                    pending.extend(neighbors)
        yield order


def _channel_layout(
    circuit, labels, pins, flipped, reversed_parts, cells, separated, local,
    *, tight=False, rotate=False, source_offsets=None, source_flips=(), regular=False,
    boundary=(),
):
    """Route from pins through column gutters to shared horizontal net tracks."""
    parts, grounds = circuit.components, circuit.grounds
    drawing = Layout()
    columns = 1 + max(column for _, column in cells.values())
    rows = [cells[i][0] for i, *_ in pins]
    gutters = [cells[i][1] + (parts[i].is_source or not left) for i, _, left, _ in pins]
    groups, lanes = defaultdict(list), defaultdict(list)
    for i, (_, net, _, _) in enumerate(pins):
        groups[net].append(i)
    names = connector_names(groups)
    # Reserve a horizontal track at the source terminal. Pins on the rest
    # of the circuit reach that track from below.
    source_rails = {}
    for net, indices in groups.items():
        sources = [i for i in indices if parts[pins[i][0]].is_source]
        if (
            not regular and net not in grounds and net not in boundary
            and sources and len(indices) > 1
        ):
            source_rails[net] = min(sources, key=lambda i: (rows[i], i))

    rotated = {}
    if rotate:
        for i, part in enumerate(parts):
            if part.kind not in "RCLX" or part.p == part.n:
                continue
            if rotate is not True and i not in rotate:
                continue
            if (part.p in grounds) != (part.n in grounds):
                live = part.n if part.p in grounds else part.p
            elif (part.p in source_rails) != (part.n in source_rails):
                live = part.p if part.p in source_rails else part.n
            elif rotate is not True:
                # A link between two ordinary signal nets can also turn when
                # one side is above the other.
                def neighbor_row(net):
                    return median_low([
                        rows[j] for j in groups[net] if pins[j][0] != i
                    ] or [cells[i][0]])

                if neighbor_row(part.p) == neighbor_row(part.n):
                    continue
                live = min((part.p, part.n), key=neighbor_row)
            else:
                continue
            if {part.p, part.n} & (local | separated):
                continue
            neighbors = [rows[j] for j in groups[live] if pins[j][0] != i]
            # A vertical shunt is useful when its connection arrives from
            # above. Keep end-to-end series connections horizontal.
            if neighbors and (live in source_rails or min(neighbors) < cells[i][0]):
                rotated[i] = live

    direct = {}
    vertical = {}
    for net, indices in groups.items():
        if (
            regular or net in grounds or net in boundary or net in separated or net in source_rails
            or (len(indices) != 2 and net not in local)
        ):
            continue
        right_pins, left_pins = {}, {}
        for i in indices:
            part, _, left, offset = pins[i]
            if part in rotated:
                continue
            key = rows[i], gutters[i], offset
            if left and not parts[part].is_source:
                left_pins[key] = i
            elif not parts[part].is_source or source_pin_is_upper(parts[part], left, grounds):
                right_pins[key] = i
        if key := next(iter(sorted(right_pins.keys() & left_pins.keys())), None):
            direct[net] = right_pins[key], left_pins[key]
        elif len(indices) == 2:
            a, b = indices
            if (
                gutters[a] == gutters[b] and rows[a] != rows[b]
                and all(pins[i][0] not in rotated for i in indices)
            ):
                vertical[net] = a, b

    padding = [[0.0, 0.0] for _ in range(columns + 1)]
    direct_pins = {i for pair in direct.values() for i in pair}
    vertical_lanes = defaultdict(list)
    for net, (a, _) in vertical.items():
        vertical_lanes[gutters[a]].append(net)
    for i, (part, net, left, _) in enumerate(pins):
        g = gutters[i]
        left = left and not parts[part].is_source
        single = len(groups[net]) == 1 and net not in boundary
        if net not in grounds and (net in separated or single):
            padding[g][left] = max(padding[g][left], 0.43 * len(names[net]))
        elif (
            net not in grounds and net not in vertical and net != rotated.get(part)
            and i not in direct_pins
            and i != source_rails.get(net)
        ):
            lanes[gutters[i]].append(i)

    widths = [4.7 if tight else 5.3] * columns
    for i, part in enumerate(parts):
        col = cells[i][1]
        name, *values = labels[part.name]
        label_width = max([min(len(name), 24), *map(len, values)])
        if not part.is_source:
            widths[col] = max(widths[col], 0.35 * label_width + (0.55 if tight else 0.8))
        if part.kind == "H":
            widths[col] = max(widths[col], PACKAGE_WIDTH + 0.4)

    centers, lane_x, vertical_x = [], {}, {}
    x = 0.0
    for g in range(columns + 1):
        x += padding[g][False]
        # Each side of the gutter approaches its own lanes. On any row the
        # two opposing sets of terminal stubs therefore cannot touch.
        indices = sorted(
            lanes[g],
            key=lambda i: (
                pins[i][2] and not parts[pins[i][0]].is_source,
                rows[i],
                (source_pin_is_upper(parts[pins[i][0]], pins[i][2], grounds)
                 != (pins[i][0] in source_flips))
                if parts[pins[i][0]].is_source else pins[i][3],
            ),
        )
        split = next(
            (j for j, i in enumerate(indices) if pins[i][2] and not parts[pins[i][0]].is_source),
            len(indices),
        )
        shared = vertical_lanes[g]
        for j, i in enumerate(indices):
            lane_x[i] = x + 0.9 + (j + (len(shared) if j >= split else 0)) * LANE_PITCH
        for j, net in enumerate(shared, start=split):
            vertical_x[net] = x + 0.9 + j * LANE_PITCH
        x += (1.25 if tight else 1.8) + max(
            0, len(indices) + len(shared) - 1
        ) * LANE_PITCH + padding[g][True]
        if g < columns:
            centers.append(x + widths[g] / 2)
            x += widths[g]

    channels = defaultdict(list)
    for i, (part, net, _, _) in enumerate(pins):
        if net == rotated.get(part):
            lane_x[i] = centers[cells[part][1]]
    for net, indices in groups.items():
        if (
            net in grounds or (len(indices) == 1 and net not in boundary) or net in separated
            or net in source_rails or net in vertical
        ):
            continue
        if net in direct and len(indices) == 2:
            continue
        row = median_low(rows[i] for i in indices)
        upper = [max(0, rows[i] - 1) for i in indices if net == rotated.get(pins[i][0])]
        lower = [
            rows[i] for i in indices
            if pins[i][0] in rotated and net != rotated[pins[i][0]]
        ]
        if upper:
            row = min(row, min(upper))
        if lower:
            row = max(row, max(lower))
        channels[row].append(net)

    row_y, track_y = [0.0], {}
    source_rows = {cells[i][0] for i, part in enumerate(parts) if part.is_source}
    package_rows = defaultdict(float)
    for i, _, _, offset in pins:
        if parts[i].kind == "H":
            package_rows[cells[i][0]] = max(package_rows[cells[i][0]], abs(offset) + 0.9)
    for row in range(max(row for row, _ in cells.values()) + 1):
        tracks = sorted(
            channels[row],
            key=lambda n: (
                max(lane_x[i] for i in groups[n] if i not in direct_pins)
                - min(lane_x[i] for i in groups[n] if i not in direct_pins),
                n,
            ),
        )
        clearance = max(2.8, package_rows[row])
        for j, net in enumerate(tracks):
            track_y[net] = row_y[-1] - clearance - TRACK_PITCH * j
        pitch = 4.15 if tight else 4.8 if row in source_rows else 5.0
        if package_rows:
            pitch = max(pitch, clearance + package_rows[row + 1] + 1.2)
        row_y.append(row_y[-1] - pitch - TRACK_PITCH * len(tracks))

    for i, part in enumerate(parts):
        row, col = cells[i]
        symbol = grid_symbol(
            part,
            centers[col] - (PACKAGE_WIDTH if part.kind == "H" else SYMBOL_WIDTH) / 2,
            row_y[row],
            grounds,
            top="-" if i in flipped else "+",
            reverse=i in reversed_parts,
        )
        if part.is_source and source_offsets:
            _, (ax, ay), (bx, by), top = symbol
            offset = source_offsets.get(i, 0)
            symbol = (part, (ax, ay - offset), (bx, by - offset), top)
        if i in source_flips:
            _, a, b, top = symbol
            symbol = (part, b, a, top)
        if i in rotated:
            x, y = centers[col], row_y[row]
            upper, lower = (x, y), (x, y - 2.5)
            a, b = (upper, lower) if part.p == rotated[i] else (lower, upper)
            symbol = (part, a, b, "+")
        drawing.positions[part.name] = symbol[1]
        drawing.symbols.append(symbol)
    terminals = []
    for i, _, left, offset in pins:
        part, a, b, _ = drawing.symbols[i]
        terminals.append(
            (a if (left != (i in reversed_parts)) else b)
            if part.is_source or i in rotated
            else (min(a[0], b[0]) if left else max(a[0], b[0]), a[1] + offset)
        )

    for net, indices in groups.items():
        if net in grounds:
            lower = {pins[i][0] for i in indices if pins[i][2] and pins[i][3] < 0}
            for i in indices:
                part, _, left, offset = pins[i]
                a = terminals[i]
                if part in rotated:
                    ground_at(drawing, a, net)
                    continue
                if parts[part].is_source:
                    lower_terminal = min(
                        point[1] for point in drawing.symbols[part][1:3]
                    )
                    if a[1] == lower_terminal:
                        b = (a[0], a[1] - 0.3)
                        add_wire(drawing, net, b, a)
                    else:
                        b = (a[0] + 0.72, a[1])
                        add_wire(drawing, net, a, b)
                    ground_at(drawing, b, net)
                    continue
                b = (a[0] + (-0.6 if left else 0.6), a[1])
                add_wire(drawing, net, a, b)
                if offset > 0 and part in lower:
                    add_wire(drawing, net, (b[0], b[1] - 0.7), b)
                else:
                    ground_at(drawing, b, net)
            continue
        if (len(indices) == 1 and net not in boundary) or net in separated:
            for i in indices:
                a = terminals[i]
                left = pins[i][2] and not parts[pins[i][0]].is_source
                b = (a[0] + (-0.72 if left else 0.72), a[1])
                add_wire(drawing, net, a, b)
                drawing.connectors.append(
                    (net, b, names[net], left, pins[i][3] < 0)
                )
        elif net in vertical:
            a, b = sorted((terminals[i] for i in vertical[net]), key=lambda p: p[1])
            x = vertical_x[net]
            add_wire(drawing, net, a, (x, a[1]), (x, b[1]), b)
        else:
            route = indices
            branch = None
            rail = source_rails.get(net)
            if net in direct:
                a, b = (terminals[i] for i in direct[net])
                branch = ((a[0] + b[0]) / 2, a[1])
                add_wire(drawing, net, a, b)
                drawing.labels[net] = branch
                route = [i for i in indices if i not in direct_pins]
                if not route:
                    continue
            if rail is not None:
                route = [i for i in indices if i != rail]
                left, ty = terminals[rail]
            else:
                ty = track_y[net]
            xs = [lane_x[i] for i in route]
            if branch:
                # Choose a free lane inside the gap for the side branch.
                gutter = gutters[direct[net][0]]
                track_x = lane_x[lanes[gutter][0]] - 0.46 if lanes[gutter] else branch[0]
                branch = (track_x, branch[1])
                xs.append(track_x)
                low, high = sorted((branch[1], ty))
                add_wire(drawing, net, (track_x, low), (track_x, high))
                drawing.junctions.setdefault(net, []).append(branch)
            if rail is None:
                left = min(xs)
            right = max(xs)
            add_wire(drawing, net, (left, ty), (right, ty))
            if not branch:
                drawing.labels[net] = ((left + right) / 2, ty)
            for i in route:
                x = lane_x[i]
                a = terminals[i]
                if a[0] != x:
                    add_wire(drawing, net, a, (x, a[1]))
                low, high = sorted((a[1], ty))
                add_wire(drawing, net, (x, low), (x, high))
                if left < x < right:
                    drawing.junctions.setdefault(net, []).append((x, ty))

    if boundary:
        attach_boundary(drawing, track_y, boundary, grounds)
    return drawing


def attach_boundary(drawing, track_y, boundary, grounds):
    """Bring each used package port to a short terminal at the edge of its detail."""
    left = min(
        [a[0] for _, a, _, _ in drawing.symbols]
        + [p[0] for wires in drawing.wires.values() for edge in wires for p in edge]
    ) - 1.1
    for net in boundary:
        if net in grounds:
            point = next(
                (p for p in drawing.grounds if drawing.ground_names.get(p, "0") == net),
                None,
            )
        elif net in track_y:
            y = track_y[net]
            start = min(
                p[0] for edge in drawing.wires[net] for p in edge if p[1] == y
            )
            point = left, y
            add_wire(drawing, net, point, (start, y))
        else:
            point = None
        if point is not None:
            drawing.boundary[net] = point


def _mark_crossings(drawing):
    """Mark junctions for the same net and visual gaps for distinct nets."""
    horizontal, vertical, diagonal = [], [], []
    for net, segments in drawing.wires.items():
        for a, b in segments:
            axis = wire_axis((a, b))
            (diagonal if axis is None else (horizontal, vertical)[axis]).append((net, a, b))
    contacts = []
    for net, a, b in vertical:
        x = a[0]
        low, high = sorted((a[1], b[1]))
        for other, c, d in horizontal:
            left, right = sorted((c[0], d[0]))
            y = c[1]
            if left < x < right and low < y < high:
                if net == other:
                    drawing.junctions.setdefault(net, []).append((x, y))
                else:
                    drawing.crossings.setdefault((net, a, b), []).append(y)
                    contacts.append((net, other))

    straight = horizontal + vertical
    for index, (net, a, b) in enumerate(diagonal):
        for other, c, d in straight + diagonal[:index]:
            crossing = intersection((a, b), (c, d))
            if crossing is None:
                continue
            first, second = crossing
            if 0 < first < 1 and 0 < second < 1:
                if net == other:
                    drawing.junctions.setdefault(net, []).append(along(a, b, first))
                else:
                    drawing.crossings.setdefault((net, a, b), []).append(first)
                    contacts.append((net, other))

    # Prefer a clear span of the trunk for its label.
    for net, (center, y) in drawing.labels.items():
        if not drawing.wires.get(net):
            continue
        a, b = drawing.wires[net][0]
        left, right = sorted((a[0], b[0]))
        stops = sorted(
            {
                left,
                right,
                *(
                    c[0]
                    for _, c, d in vertical
                    if left < c[0] < right and c[1] <= y + 0.75 and d[1] >= y
                ),
            }
        )
        candidates = [
            (start + end) / 2
            for start, end in zip(stops, stops[1:])
            if end - start >= len(net) * 0.32 + 0.35
        ]
        if candidates:
            drawing.labels[net] = (min(candidates, key=lambda x: abs(x - center)), y)
        elif len(stops) > 2:
            start, end = max(
                zip(stops, stops[1:]), key=lambda interval: interval[1] - interval[0]
            )
            drawing.labels[net] = ((start + end) / 2, y)

    return contacts


def _separate_nets(contacts, drawing, budget, local):
    remaining = list(contacts)
    separated = set()
    while len(remaining) > budget:
        neighbors = defaultdict(int)
        for a, b in remaining:
            neighbors[a] += 1
            neighbors[b] += 1
        net = max(
            (net for net in neighbors if net not in local and net != "0"),
            key=lambda net: (neighbors[net] / (len(drawing.wires[net]) + 1), net),
            default=None,
        )
        if net is None:
            break
        separated.add(net)
        remaining = [(a, b) for a, b in remaining if net not in (a, b)]
    return separated


def _layout_cost(drawing):
    """Measure the visible cost of a candidate in schematic grid units."""
    segments = [segment for wires in drawing.wires.values() for segment in wires]
    length = sum(math.dist(a, b) for a, b in segments)
    gaps = sum(map(len, drawing.crossings.values()))
    points = [point for segment in segments for point in segment]
    points.extend(point for _, a, b, _ in drawing.symbols for point in (a, b))
    xs, ys = zip(*points)
    span = max(max(xs) - min(xs), 1.25 * (max(ys) - min(ys)))
    return length + 14 * gaps + 16 * len(drawing.connectors) + 0.5 * span


def _placement_trials(circuit, pins, runs, order):
    """Propose swaps and moves between nearby blocks that share a net."""
    parts = circuit.components
    by_member = {i: tuple(run) for run in runs for i in run}
    blocks = list(
        dict.fromkeys(by_member.get(i, (i,)) for i in order if not parts[i].is_source)
    )
    position = {i: j for j, block in enumerate(blocks) for i in block}
    nets = defaultdict(set)
    for i, net, *_ in pins:
        if i in position and net not in circuit.grounds:
            nets[net].add(position[i])

    pairs = set()
    for members in nets.values():
        indices = sorted(members)
        pairs.update(zip(indices, indices[1:]))
    # Start with distant connected blocks, plus neighboring blocks whose
    # order can determine whether a branch fits on a row.
    moves = sorted(pairs, key=lambda pair: (-(pair[1] - pair[0]), pair))
    moves = moves[:10] + [(i, i + 1) for i in range(min(8, len(blocks) - 1))]
    sources = [i for i in order if parts[i].is_source]
    for left, right in dict.fromkeys(moves):
        moved = blocks.copy()
        if right == left + 1:
            moved[left], moved[right] = moved[right], moved[left]
        else:
            moved.insert(left + 1, moved.pop(right))
        yield [*sources, *(i for block in moved for i in block)]


def connected_layout(circuit, labels, *, simplify=False):
    """Search several placements and refine the best with local block moves."""
    parts = circuit.components
    if not parts:
        return Layout()
    runs, reversed_parts = series_runs(circuit)
    feedback = feedback_pairs(parts)
    run_members = {i for run in runs for i in run}
    # Keep the output end of a feedback component on the same side as the
    # amplifier output. Series runs already have a fixed physical orientation.
    reversed_parts.update(
        resistor for amp, resistor, _ in feedback
        if resistor not in run_members and parts[resistor].p == parts[amp].p
    )
    flipped = {
        i
        for i, part in enumerate(parts)
        if part.kind == "O" and part.in_plus == "0" and part.in_minus != "0"
    }
    pins = component_pins(parts, flipped, reversed_parts)
    size = len(parts)
    center = math.ceil(math.sqrt(size * 0.72))
    widths = sorted({max(1, min(size, center + offset)) for offset in (-2, -1, 0, 1, 2)})
    candidates = []
    seen = set()
    budget = max(8, size // 2)
    source_order = sorted(range(size), key=lambda i: (parts[i].order, i))
    local = {
        net
        for run in runs for a, b in zip(run, run[1:])
        for net in {parts[a].p, parts[a].n} & {parts[b].p, parts[b].n}
    }
    supply = {
        net for part in parts if part.is_source for net in (part.p, part.n)
    }
    feedback_nets = {net for amp, _, pin in feedback for net in (parts[amp].p, pin)}
    units = [tuple(run) for run in runs]
    units.extend(
        (i,) for i, part in enumerate(parts) if i not in run_members and not part.is_source
    )

    def evaluate_cells(cells, order=None, columns=None):
        key = tuple(sorted(cells.items()))
        if key in seen:
            return
        seen.add(key)
        penalty = placement_penalty(cells, feedback)
        drawing = _channel_layout(
            circuit, labels, pins, flipped, reversed_parts, cells, set(), local
        )
        contacts = _mark_crossings(drawing)
        score = _layout_cost(drawing) + penalty
        candidates.append((score, order, columns, drawing, cells))
        if simplify and len(contacts) > budget:
            # Keep both versions so a small wire saving cannot force a break
            # into an otherwise readable circuit.
            separated = _separate_nets(
                contacts, drawing, budget, local | supply | feedback_nets
            )
            if separated:
                drawing = _channel_layout(
                    circuit, labels, pins, flipped, reversed_parts, cells, separated, local
                )
                _mark_crossings(drawing)
                simplified_score = _layout_cost(drawing) + penalty
                candidates.append((simplified_score, order, columns, drawing, cells))
                score = min(score, simplified_score)
        return score

    def evaluate(order, columns):
        cells = source_cells(circuit, order, columns, runs, source_rails=True)
        evaluate_cells(cells, order, columns)

    for order in (source_order, *_orders(parts, pins, circuit.grounds)):
        for columns in widths:
            evaluate(order, columns)
    for _ in range(2):
        best = min(candidates, key=lambda candidate: candidate[0])
        _, order, columns, _, _ = best
        for trial in _placement_trials(circuit, pins, runs, order):
            evaluate(trial, columns)
        if min(candidates, key=lambda candidate: candidate[0]) is best:
            break

    seeds = {}
    for score, _, _, _, cells in sorted(candidates, key=lambda candidate: candidate[0]):
        seeds.setdefault(tuple(sorted(cells.items())), (score, cells))
        if len(seeds) == 6:
            break
    evolve_placements(seeds.values(), units, feedback, evaluate_cells)

    for _, _, _, drawing, cells in sorted(candidates, key=lambda candidate: candidate[0]):
        if preserves_nets(circuit, drawing):
            separated = {net for net, *_ in drawing.connectors}
            return _compact_channels(
                circuit, labels, pins, flipped, reversed_parts, cells, separated, local,
                drawing, units, feedback,
            )
    return labeled(circuit, labels)


def _compact_channels(
    circuit, labels, pins, flipped, reversed_parts, cells, separated, local,
    drawing, units, feedback,
):
    """Refine the winning placement with shorter routes and nearby component moves."""
    def cost(layout):
        length = sum(math.dist(a, b) for wires in layout.wires.values() for a, b in wires)
        return length, sum(map(len, layout.crossings.values()))

    def clear_symbols(layout):
        """Reject a shortcut through the interior of another symbol."""
        wires = [edge for segments in layout.wires.values() for edge in segments]
        for part, a, b, _ in layout.symbols:
            x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if part.kind in {"O", "Q"}:
                height = BJT_PIN_OFFSET if part.kind == "Q" else 0.73
                body = ((a[0] + 0.08, y - height), (b[0] - 0.08, y + height))
            elif a[0] == b[0]:
                body = ((x - 0.44, min(a[1], b[1]) + 0.08),
                        (x + 0.44, max(a[1], b[1]) - 0.08))
            else:
                body = ((min(a[0], b[0]) + 0.08, y - 0.44),
                        (max(a[0], b[0]) - 0.08, y + 0.44))
            if any(segments_intersect(body, edge) for edge in wires):
                return False
        return True

    best, best_cells = drawing, cells
    best_length, best_gaps = cost(drawing)
    gap_limit = best_gaps
    stage_limit = placement_penalty(cells, feedback)
    rotations = False
    offsets, flips = {}, set()

    def try_layout(trial, rotate, source_offsets=None, source_flips=()):
        nonlocal best, best_cells, best_length, best_gaps, rotations, offsets, flips
        if placement_penalty(trial, feedback) > stage_limit:
            return
        layout = _channel_layout(
            circuit, labels, pins, flipped, reversed_parts, trial, separated, local,
            tight=True, rotate=rotate, source_offsets=source_offsets,
            source_flips=source_flips,
        )
        _mark_crossings(layout)
        length, gaps = cost(layout)
        if gaps <= gap_limit and (round(length, 7), gaps) < (
            round(best_length, 7), best_gaps
        ) and clear_symbols(layout) and preserves_nets(circuit, layout):
            best, best_cells = layout, trial
            best_length, best_gaps = length, gaps
            rotations = {
                i for i, (part, a, b, _) in enumerate(layout.symbols)
                if not part.is_source and a[0] == b[0]
            }
            offsets = source_offsets or {}
            flips = set(source_flips)

    try_layout(cells, False)
    try_layout(cells, True)
    parts = circuit.components
    anchors = circuit.grounds | {
        net for part in parts if part.is_source for net in (part.p, part.n)
    }
    shunts = [
        i for i, part in enumerate(parts)
        if part.kind in "RCLX" and {part.p, part.n} & anchors
    ]
    for i in shunts[:12]:
        try_layout(best_cells, (rotations or set()) ^ {i})

    if not units:
        return best
    first_row = min(cells[unit[0]][0] for unit in units)
    first_column = min(cells[unit[0]][1] for unit in units)
    last_column = max(col for _, col in cells.values())
    owners = defaultdict(set)
    for i, net, *_ in pins:
        if net not in circuit.grounds:
            owners[net].add(i)
    neighbors = defaultdict(set)
    for members in owners.values():
        for i in members:
            neighbors[i].update(members - {i})

    def distance(unit, placement):
        return sum(
            abs(placement[i][0] - placement[j][0])
            + abs(placement[i][1] - placement[j][1])
            for i in unit for j in sorted(neighbors[i]) if j not in unit
        )

    # Try the most stretched blocks first. A link can move to a free slot
    # near the two terminals it bridges instead of routing back to its old row.
    singles = {u[0] for u in units if len(u) == 1}
    for unit in sorted(units, key=lambda u: (-distance(u, best_cells), u))[:20]:
        occupied = {pos: i for i, pos in best_cells.items() if i not in unit}
        row, col = best_cells[unit[0]]
        adjacent = set().union(*(neighbors[i] for i in unit)) - set(unit)
        preferred_rows = {row - 1, row, row + 1}
        preferred_columns = {col - 1, col, col + 1}
        if len(unit) == 1:
            preferred_rows.update(best_cells[i][0] for i in adjacent)
            preferred_columns.update(best_cells[i][1] for i in adjacent)
        trials = []
        for r in sorted(preferred_rows):
            for c in sorted(preferred_columns):
                if (r, c) == (row, col):
                    continue
                positions = [(r, c + offset) for offset in range(len(unit))]
                if r < first_row or c < first_column or positions[-1][1] > last_column:
                    continue
                blockers = [occupied[pos] for pos in positions if pos in occupied]
                if blockers and (len(unit) != 1 or blockers[0] not in singles):
                    continue
                trial = best_cells | dict(zip(unit, positions))
                if blockers:
                    trial[blockers[0]] = row, col
                trials.append((distance(unit, trial), trial))
        trial_count = 7 if len(parts) <= 24 else 3
        for _, trial in sorted(trials, key=lambda candidate: candidate[0])[:trial_count]:
            try_layout(trial, rotations)
            if len(unit) == 1 and parts[unit[0]].kind in "RCLX":
                try_layout(trial, (rotations or set()) | {unit[0]})
                # Two vertical branches can compete for the same drop. Give
                # the newly rotated part a trial with the other one horizontal.
                if rotations and unit[0] in shunts:
                    try_layout(trial, {unit[0]})

    # Drop an isolated source in its own column toward the branch it feeds.
    # Each trial re-routes the rail from the new terminal height.
    sources = sorted(
        (i for i, part in enumerate(parts) if part.is_source),
        key=lambda i: best_cells[i],
    )
    for i in reversed(sources):
        part = parts[i]
        grounded = bool({part.p, part.n} & circuit.grounds)
        for offset in (0.35, 0.65):
            try_layout(best_cells, rotations, offsets | {i: offset}, flips)
        if part.kind == "V" and not grounded:
            try_layout(best_cells, rotations, offsets, flips ^ {i})
    if len(sources) > 1:
        # Change the vertical order of batteries as a unit, including both
        # terminals. Their rails still start at the source and run right.
        for a, b in zip(sources, sources[1:]):
            if best_cells[a][1] == best_cells[b][1]:
                trial = best_cells | {a: best_cells[b], b: best_cells[a]}
                try_layout(trial, rotations, offsets, flips)

    # Recheck the rail drops after moving sources and branches. A floating
    # return can use the open edge column without crossing a symbol below it.
    floating = {
        net for i in sources for net in (parts[i].p, parts[i].n)
        if parts[i].p not in circuit.grounds and parts[i].n not in circuit.grounds
    }
    for i in sorted(singles):
        part = parts[i]
        if part.kind not in "RCLX" or not {part.p, part.n} & floating:
            continue
        row, col = best_cells[i]
        occupied = {pos: j for j, pos in best_cells.items() if j != i}
        for r in (row, row + 1):
            trial = best_cells | {i: (r, last_column + 1)}
            blocker = occupied.get((r, last_column + 1))
            if blocker is not None:
                if blocker not in singles:
                    continue
                trial[blocker] = row, col
            try_layout(trial, (rotations or set()) | {i}, offsets, flips)
            try_layout(trial, {i}, offsets, flips)
    return _shorten_rail_branches(circuit, best, cost, clear_symbols)


def _shorten_rail_branches(circuit, drawing, cost, clear_symbols):
    """Place a passive branch directly between two source rails when it fits."""
    rails = {}
    for part, a, b, _ in drawing.symbols:
        if part.is_source:
            for net, pin in ((part.p, a), (part.n, b)):
                edges = drawing.wires.get(net)
                if edges and edges[0][0] == pin and edges[0][1][1] == pin[1]:
                    rails[net] = edges[0]

    def branch_edges(edges, pin, rail):
        """Walk an unshared drop from its terminal to the rail."""
        branch = []
        while not segments_intersect((pin, pin), rail):
            touching = [edge for edge in edges[1:] if pin in edge and edge not in branch]
            if len(touching) != 1:
                return []
            edge = touching[0]
            branch.append(edge)
            pin = edge[edge[0] == pin]
        return branch

    def length(edges):
        return sum(math.dist(a, b) for a, b in edges)

    for index, (part, a, b, top) in enumerate(drawing.symbols):
        if part.kind not in "RCLX" or part.p == part.n or not {part.p, part.n} <= rails.keys():
            continue
        p_rail, n_rail = rails[part.p], rails[part.n]
        py, ny = p_rail[0][1], n_rail[0][1]
        left = max(p_rail[0][0], n_rail[0][0]) + 0.3
        right = min(p_rail[1][0], n_rail[1][0]) - 0.3
        if abs(py - ny) < 3.5 or left > right:
            continue
        p_edges = branch_edges(drawing.wires[part.p], a, p_rail)
        n_edges = branch_edges(drawing.wires[part.n], b, n_rail)
        if not p_edges or not n_edges:
            continue
        old_length = length(p_edges) + length(n_edges)
        if abs(py - ny) - 2.5 >= old_length - 1e-7:
            continue

        center = (a[0] + b[0]) / 2
        xs = sorted({left, right, *(x for wires in drawing.wires.values()
                      for edge in wires for x, _ in edge if left < x < right)})
        lanes = [min(right, max(left, center))]
        lanes.extend(sorted(((l + r) / 2 for l, r in zip(xs, xs[1:])),
                            key=lambda x: abs(x - center))[:8])
        old_gaps = cost(drawing)[1]
        for x in lanes:
            y = (py + ny) / 2
            direction = 1 if py > ny else -1
            new_a, new_b = (x, y + direction * 1.25), (x, y - direction * 1.25)
            clearance = ((x - 0.9, y - 1.9), (x + 0.9, y + 1.9))
            if any(
                segments_intersect(clearance, (start, end))
                for j, (_, start, end, _) in enumerate(drawing.symbols)
                if j != index
            ):
                continue
            trial = deepcopy(drawing)
            trial.symbols[index] = part, new_a, new_b, top
            trial.positions[part.name] = new_a
            for net, edges, pin, rail_y in (
                (part.p, p_edges, new_a, py), (part.n, n_edges, new_b, ny),
            ):
                wires = trial.wires[net]
                for edge in edges:
                    wires.remove(edge)
                tap = (x, rail_y)
                add_wire(trial, net, *sorted((pin, tap), key=lambda point: point[1]))
                trial.junctions[net] = [
                    dot for dot in trial.junctions.get(net, ())
                    if not any(segments_intersect((dot, dot), edge) for edge in edges)
                    or sum(segments_intersect((dot, dot), edge) for edge in wires) > 1
                ]
                trial.junctions[net].append(tap)
            trial.crossings.clear()
            _mark_crossings(trial)
            if cost(trial)[1] <= old_gaps and clear_symbols(trial) and preserves_nets(circuit, trial):
                drawing = trial
                break
    return drawing
