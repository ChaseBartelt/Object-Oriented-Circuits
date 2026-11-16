"""Move, rotate, slide and split geometry while preserving electrical nets."""

import math
from collections import Counter, defaultdict
from dataclasses import replace

from circuitlab.layout.geometry import add_wire, along, rotate_point, symbol_pins, wire_axis
from circuitlab.layout.checks import preserves_nets, segments_intersect
from circuitlab.layout.wiring import connector_names
from circuitlab.layout.wire_edit import (
    clean_point,
    replace_route,
    route_anchors,
    terminal_run,
    wire_graph,
    wire_slide_candidates,
)

from circuitlab.layout.checks import mark_crossings as _mark_crossings


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

    new_pins = _routed_pins(moved, turns, drawing.pin_grid)
    endpoints = {(net, clean_point(point)) for net, point in new_pins}
    docked_pins, stationary_pins = set(), []
    for j, other in enumerate(drawing.symbols):
        if i == j:
            continue
        other_turns = original.rotations.get(other[0].name, 0)
        other_pins = _routed_pins(other, other_turns, drawing.pin_grid)
        shared = endpoints.intersection((net, clean_point(point)) for net, point in other_pins)
        # Same-net endpoints may touch. Keep the symbol bodies clear of each other.
        if boxes_overlap(
            _symbol_bounds(moved, turns, body_only=True),
            _symbol_bounds(other, other_turns, body_only=True),
        ):
            return None
        docked_pins.update(shared)
        stationary_pins.extend(other_pins)
    drawing.symbols[i] = moved
    drawing.positions[name] = moved[1]

    return _reroute_move(
        circuit, original, drawing,
        _routed_pins(symbol, previous_turns, original.pin_grid), new_pins,
        (a[0] + b[0]) / 2,
        stationary_pins=stationary_pins, diagonal=diagonal, rotate=rotate,
        align_axis=align_axis, docked_pins=docked_pins,
    )


def _symbol_bounds(symbol, turns, *, body_only=False):
    part, a, b, _ = symbol
    height = (
        max(0.8, (len(part.package_pins) + 1) // 2 / 2 + 0.2)
        if part.kind == "H" else 0.9 if part.kind in {"Q", "O", "S"} else 0.45
    )
    if body_only and (axis := wire_axis((a, b))) is not None:
        inset = {"H": 0.55, "O": 1.2, "Q": 0.8, "S": 0.8}.get(
            part.kind, max(0, abs(a[axis] - b[axis]) / 2 - 0.75)
        )
        x_pad, y_pad = (-inset, height) if axis == 0 else (0.45, -inset)
    else:
        x_pad, y_pad = 0.45, height
    lo = min(a[0], b[0]) - x_pad, min(a[1], b[1]) - y_pad
    hi = max(a[0], b[0]) + x_pad, max(a[1], b[1]) + y_pad
    return rotate_point(lo, a, b, turns), rotate_point(hi, a, b, turns)


def selection_ends(layout):
    yield from ((("boundary", net), net, pos) for net, pos in layout.boundary.items())
    yield from ((("ground", pos), layout.ground_names.get(pos, "0"), pos) for pos in layout.grounds)
    yield from ((("connector", i), net, pos) for i, (net, pos, *_) in enumerate(layout.connectors))


def move_symbols(circuit, original, names, delta, *, diagonal=False):
    """Translate a selection together and reroute its connections to fixed parts."""
    if not any(delta):
        return original

    def shift(point):
        return clean_point(tuple(a + b for a, b in zip(point, delta)))

    def turns(symbol):
        return original.rotations.get(symbol[0].name, 0)

    def pins(symbols):
        return [pin for symbol in symbols for pin in _routed_pins(symbol, turns(symbol), original.pin_grid)]

    moved, old, fixed, symbols = [], [], [], []
    for symbol in original.symbols:
        part, a, b, top = symbol
        if part.name in names:
            old.append(symbol)
            symbol = part, shift(a), shift(b), top
            moved.append(symbol)
        else:
            fixed.append(symbol)
        symbols.append(symbol)

    selected_ends = [(key, net, pos) for key, net, pos in selection_ends(original) if key in names]
    if not old and not selected_ends:
        return original
    fixed_boxes = [_symbol_bounds(symbol, turns(symbol), body_only=True) for symbol in fixed]
    moved_boxes = [_symbol_bounds(symbol, turns(symbol), body_only=True) for symbol in moved]
    if any(
        boxes_overlap(body, box) for body in moved_boxes for box in fixed_boxes
    ):
        return None

    old_pins, new_pins, stationary_pins = pins(old), pins(moved), pins(fixed)
    end_pins = [(net, pos) for _, net, pos in selected_ends]
    old_pins += end_pins
    new_pins += [(net, shift(pos)) for net, pos in end_pins]
    stationary_ends = [(net, pos) for key, net, pos in selection_ends(original) if key not in names]
    internal = {net for net, _ in old_pins} - ({net for net, _ in stationary_pins} | ({net for net, _ in stationary_ends} if selected_ends else original.boundary.keys()))
    grounds = {
        pos: shift(pos) if original.ground_names.get(pos, "0") in internal or ("ground", pos) in names else pos
        for pos in original.grounds
    }
    if len(set(grounds.values())) != len(grounds):
        return None
    connectors = {pos: shift(pos) for i, (net, pos, *_) in enumerate(original.connectors)
                  if net in internal or ("connector", i) in names}
    positions = {**original.positions, **{part.name: a for part, a, _, _ in moved}}
    drawing = replace(
        original, symbols=symbols, positions=positions,
        boundary={net: shift(pos) if ("boundary", net) in names else pos for net, pos in original.boundary.items()},
        wires={net: [(shift(a), shift(b)) for a, b in edges] if net in internal else edges
               for net, edges in original.wires.items()},
        junctions={net: list(map(shift, dots)) if net in internal else dots
                   for net, dots in original.junctions.items()},
        labels={net: shift(pos) if net in internal else pos for net, pos in original.labels.items()},
        grounds=list(grounds.values()),
        ground_names={grounds[pos]: net for pos, net in original.ground_names.items()},
        ground_rotations={grounds[pos]: angle for pos, angle in original.ground_rotations.items()},
        connectors=[(net, connectors.get(pos, pos), *label) for net, pos, *label in original.connectors],
        label_offsets=_move_endpoint_labels(original, grounds=grounds, connectors=connectors),
        crossings={},
    )
    external_old = [pin for pin in old_pins if pin[0] not in internal]
    external_new = [pin for pin in new_pins if pin[0] not in internal]
    if external_old:
        return _reroute_move(
            circuit, original, drawing, external_old, external_new,
            sum(pos[0] for _, pos in old_pins) / len(old_pins),
            stationary_pins=stationary_pins, diagonal=diagonal,
            moving_ends=set(end_pins),
        )
    _mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


def _routed_pins(symbol, turns, grid=0):
    _, a, b, _ = symbol
    return tuple(
        (net, rotate_point(pin, a, b, turns)) for net, pin in symbol_pins(*symbol, grid=grid)
    )


def symbol_terminal_offsets(drawing, name):
    """Find translations that bring like-net pins together."""
    stationary = defaultdict(list)
    moving = ()
    for symbol in drawing.symbols:
        part = symbol[0]
        pins = _routed_pins(symbol, drawing.rotations.get(part.name, 0), drawing.pin_grid)
        if part.name == name:
            moving = pins
        else:
            for net, pos in pins:
                stationary[net].append(pos)
    return {
        clean_point((other[0] - pos[0], other[1] - pos[1]))
        for net, pos in moving for other in stationary[net]
    }


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
    moving_pins = _routed_pins(symbol, turns, drawing.pin_grid)
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
    boundary = original.boundary
    if boundary.get(net) == point:
        boundary = {**boundary, net: end}
    drawing = replace(
        original,
        grounds=[end if pos == point else pos for pos in original.grounds],
        ground_names=dict(original.ground_names),
        ground_rotations=dict(original.ground_rotations),
        boundary=boundary,
        label_offsets=_move_endpoint_labels(original, grounds={point: end}),
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
        endpoints = {start: end}
    else:
        ports = [port for i, port in enumerate(original.connectors) if i not in (index, join)]
        endpoints = {start: None, end: None}
    drawing = _reroute_move(
        circuit, original, replace(
            original, connectors=ports, label_offsets=_move_endpoint_labels(original, connectors=endpoints)
        ),
        ((net, start),), ((net, end),), start[0], moving_connector=index, diagonal=diagonal,
    )
    if drawing is None and join is not None and any(port[0] == net for port in ports):
        # A third disconnected end still needs a label on the newly joined run.
        drawing = _reroute_move(
            circuit, original, replace(
                original, connectors=[*ports, original.connectors[join]],
                label_offsets=_move_endpoint_labels(original, connectors={start: None}),
            ),
            ((net, start),), ((net, end),), start[0], moving_connector=index, diagonal=diagonal,
        )
    return drawing


def move_boundary(circuit, original, net, delta, *, diagonal=False):
    """Move a package port and trim its old lead back to the connected circuit."""
    start = original.boundary[net]
    end = clean_point(tuple(a + b for a, b in zip(start, delta)))
    if end == clean_point(start):
        return original
    connectors = list(original.connectors)
    attached = next(
        (i for i, (wire, pos, *_) in enumerate(connectors) if (wire, pos) == (net, start)),
        None,
    )
    if attached is not None:
        wire, _, *label = connectors[attached]
        connectors[attached] = wire, end, *label
    drawing = replace(
        original, boundary={**original.boundary, net: end}, connectors=connectors,
        label_offsets=_move_endpoint_labels(original, connectors={start: end} if attached is not None else None),
    )
    return _reroute_move(
        circuit, original, drawing, ((net, start),), ((net, end),), start[0],
        moving_boundary=net, moving_connector=attached, direct=True, diagonal=diagonal,
    )


def _move_endpoint_labels(layout, *, grounds=None, connectors=None):
    """Keep a label offset with the ground symbol or connector it belongs to."""
    moves = {"ground": grounds or {}, "connector": connectors or {}}
    offsets = {}
    for (kind, *identity), offset in layout.label_offsets.items():
        position = tuple(identity)
        position = moves.get(kind, {}).get(position, position)
        if position is not None:
            offsets[(kind, *position)] = offset
    return offsets


def split_wire(circuit, original, net, edge, at, *, grid=0, origin=(0, 0)):
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
    if grid:
        low, high = (
            clean_point(tuple(math.floor((v + o) / grid + 0.5) * grid - o for v, o in zip(p, origin)))
            for p in (low, high)
        )
        if low == high:
            return None
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
    stationary_pins=None, moving_ground=None, moving_connector=None, moving_boundary=None,
    diagonal=False, rotate=0, align_axis=None, direct=False, docked_pins=(), moving_ends=(),
):
    affected = {net for net, _ in old_pins}
    coincident = {net for net, end in docked_pins if route_anchors(drawing, net) == {end}}
    # Attach at the first fixed corner, branch, or terminal. The old lead
    # is then shortened or pruned instead of leaving a stub at the old pin.
    fixed_anchors = {
        net: route_anchors(
            original, net, stationary_pins,
            moving_ground=moving_ground, moving_connector=moving_connector, moving_boundary=moving_boundary,
        ) - {clean_point(pos) for wire, pos in moving_ends if wire == net}
        for net in affected
    }
    graphs = {
        net: {} if net in coincident else wire_graph(original.wires.get(net, ()), route_anchors(original, net))
        for net in affected
    }
    connections = []
    for index, ((net, start), (_, end)) in enumerate(zip(old_pins, new_pins)):
        if net in coincident:
            continue
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
        docked = (net, end) in docked_pins
        if direct or docked:
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
        if not docked:
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
    if docked_pins and not direct:
        return _reroute_move(
            circuit, original, drawing, old_pins, new_pins, center_x,
            stationary_pins=stationary_pins, diagonal=diagonal, rotate=rotate,
            direct=True, docked_pins=docked_pins,
        )
    # Preserve the old junction geometry if trimming leaves no valid route.
    connections = [
        (net, clean_point(start), clean_point(end))
        for (net, start), (_, end) in zip(old_pins, new_pins)
        if net not in coincident
    ]
    previous_edges = {net: edges for net, edges in original.wires.items() if net not in coincident}
    return _shortest_move(
        drawing, circuit, affected, connections, previous_edges, bends, center_x
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


def move_wire(circuit, original, net, edge, distance, *, snap=0, pointer=None, accept_offset=None):
    """Return the first electrically valid slide and its snapped offset, if any."""
    for drawing, offset in wire_slide_candidates(original, net, edge, distance, snap=snap, pointer=pointer):
        if accept_offset is not None and not accept_offset(distance if offset is None else offset):
            continue
        if drawing is original:
            return drawing, None
        _mark_crossings(drawing)
        if preserves_nets(circuit, drawing):
            return drawing, offset
    return None, None
