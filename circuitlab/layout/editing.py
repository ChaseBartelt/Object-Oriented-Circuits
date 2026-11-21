"""Move, rotate, slide and split geometry while preserving electrical nets."""
# Editing operates on candidate layouts and reconnects moved terminals to fixed attachments.
# Accept a move only after checking connectivity, so dragging a glyph cannot redefine the
# circuit or join nets merely because their coordinates happen to overlap.

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

from circuitlab.layout.checks import mark_crossings, preserves_nets, segment_intersects_box, segments_intersect
from circuitlab.layout.geometry import add_wire, along, routed_pins, symbol_bounds as _symbol_bounds, wire_axis
from circuitlab.layout.wiring import connector_names
from circuitlab.layout.wire_edit import (
    clean_point, graph_edges, physical_wire_islands, replace_route, route_anchors, route_trial,
    terminal_run, wire_graph, wire_slide_candidates,
)


def _shift(point, delta):
    return clean_point(tuple(a + b for a, b in zip(point, delta)))


# Move the body and all of its physical contacts together, then reconnect those contacts
# to the unchanged surrounding circuit before accepting the candidate.
def move_symbol(circuit, original, name, delta, *, diagonal=False, rotate=0, align_axis=None, ground_islands=()):
    """Move or rotate one symbol and reroute the leads to its pins."""
    i, symbol = next(
        (i, symbol) for i, symbol in enumerate(original.symbols)
        if symbol[0].name == name
    )
    part, a, b, top = symbol
    moved = part, _shift(a, delta), _shift(b, delta), top
    previous_turns = original.rotations.get(name, 0)
    turns = (previous_turns + rotate) % 4
    if moved == symbol and not rotate and align_axis is None:
        return original
    stationary = [other for j, other in enumerate(original.symbols) if i != j]
    base = _translate_ground_islands(original, ground_islands, delta, stationary)
    if base is None:
        return None
    old_pins = routed_pins(symbol, previous_turns, original.pin_grid)
    new_pins = routed_pins(moved, turns, original.pin_grid)
    old_pins, new_pins = _external_ground_pins(old_pins, new_pins, ground_islands)
    endpoints = {(net, clean_point(point)) for net, point in new_pins}
    bounds = _symbol_bounds(moved, turns, body_only=True)
    docked_pins, stationary_pins = set(), []
    for other in stationary:
        other_turns = original.rotations.get(other[0].name, 0)
        # Same-net endpoints may touch. Keep the symbol bodies clear of each other.
        if boxes_overlap(bounds, _symbol_bounds(other, other_turns, body_only=True)):
            return None
        other_pins = routed_pins(other, other_turns, original.pin_grid)
        docked_pins.update(endpoints.intersection((net, clean_point(p)) for net, p in other_pins))
        stationary_pins.extend(other_pins)

    # Route candidates copy their own mutable geometry in _shortest_move.
    drawing = replace(base, symbols=list(original.symbols),
                      positions=dict(original.positions), rotations=dict(original.rotations))
    if turns:
        drawing.rotations[name] = turns
    else:
        drawing.rotations.pop(name, None)
    drawing.symbols[i] = moved
    drawing.positions[name] = moved[1]

    if ground_islands and not old_pins:
        mark_crossings(drawing)
        return drawing if preserves_nets(circuit, drawing) else None
    result = _reroute_move(
        circuit, base, drawing, old_pins, new_pins,
        (a[0] + b[0]) / 2,
        stationary_pins=stationary_pins, diagonal=diagonal, rotate=rotate,
        align_axis=align_axis, docked_pins=docked_pins,
    )
    if result is None and part.kind == "A" and not diagonal:
        # Three top/bottom control leads can cross every shared Manhattan
        # bend candidate during a translation. Retain their nets with the
        # same checked diagonal fallback already used for rotations.
        result = _reroute_move(
            circuit, base, drawing, old_pins, new_pins,
            (a[0] + b[0]) / 2, stationary_pins=stationary_pins,
            diagonal=True, rotate=rotate, align_axis=align_axis, docked_pins=docked_pins,
        )
    return result


def flip_opamp_inputs(circuit, original, name):
    """Swap an op amp's input positions and reconnect their existing nets."""
    for index, symbol in enumerate(original.symbols):
        part, a, b, top = symbol
        if part.name == name and part.kind in {"O", "A"}:
            break
    else:
        return None

    moved = part, a, b, "-" if top == "+" else "+"
    drawing = replace(original, symbols=list(original.symbols))
    drawing.symbols[index] = moved
    # An op amp with both inputs tied together needs only its labels swapped.
    if part.in_plus == part.in_minus:
        return drawing

    turns = original.rotations.get(name, 0)
    old_pins = routed_pins(symbol, turns, original.pin_grid)[:2]
    new_pins = routed_pins(moved, turns, original.pin_grid)[:2]
    stationary = list(original.pins())
    for pin in old_pins:
        stationary.remove(pin)
    center_x = (a[0] + b[0]) / 2
    # Stagger the two leads on perpendicular tracks so any crossover is
    # between visible runs, away from the contacts at their ends.
    for extra_bends in ((("xy", .5), ("yx", .5)), (("diagonal", 0),)):
        result = _reroute_move(
            circuit, original, drawing, old_pins, new_pins, center_x,
            stationary_pins=stationary, extra_bends=extra_bends,
        )
        if result is not None:
            return result
    return None


def selection_ends(layout):
    yield from ((("boundary", net), net, pos) for net, pos in layout.boundary.items())
    yield from ((("ground", pos), layout.ground_names.get(pos, "0"), pos) for pos in layout.grounds)
    yield from ((("connector", i), net, pos) for i, (net, pos, *_) in enumerate(layout.connectors))


def _selection_nets(layout, names, selected, stationary, selected_ends):
    fixed_ends = ({net for key, net, _ in selection_ends(layout) if key not in names}
                  if selected_ends else layout.boundary.keys())
    return {net for net, _ in selected} - ({net for net, _ in stationary} | fixed_ends)


def _first_bend(run):
    return next((
        i for i in range(1, len(run) - 1)
        if not segments_intersect((run[i], run[i]), (run[i - 1], run[i + 1]))
    ), len(run) - 1)


def selection_geometry(layout, names):
    """Return moving endpoints, internal wires, and the leads to fixed attachments."""
    selected, stationary = [], []
    for symbol in layout.symbols:
        name = symbol[0].name
        pins = routed_pins(symbol, layout.rotations.get(name, 0), layout.pin_grid)
        (selected if name in names else stationary).extend(pins)
    ends = list(selection_ends(layout))
    attachments = attached_ground_islands(layout, names)
    carried = {("ground", pos) for island in attachments for pos in island.grounds}
    names = set(names) | carried
    selected_ends = [(net, pos) for key, net, pos in ends if key in names]
    selected.extend(selected_ends)
    internal = _selection_nets(layout, names, selected, stationary, selected_ends) if len(names) > 1 else set()
    endpoints = [(key, pos) for key, net, pos in ends
                 if key in names or (key[0] != "boundary" and net in internal)]
    wires = [edge for net, edges in layout.wires.items() if net in internal for edge in edges]
    wires.extend(
        edge for island in attachments if island.net not in internal for edge in island.edges
    )
    external = defaultdict(list)
    for net, pos in selected:
        if net not in internal:
            external[net].append(clean_point(pos))
    for net, pins in external.items():
        graph = wire_graph(layout.wires.get(net, ()), route_anchors(layout, net))
        fixed = route_anchors(layout, net, stationary) - {
            clean_point(pos) for name, pos in selected_ends if name == net
        }
        for index, pin in enumerate(pins):
            run = terminal_run(graph, pin, fixed | set(pins[:index] + pins[index + 1:]))
            stop = _first_bend(run)
            wires.extend(zip(run[:stop], run[1:stop + 1]))
    return endpoints, wires


# Move the selected group as a unit: its internal geometry travels together, while only
# connections to fixed external attachments need rerouting.
def move_symbols(circuit, original, names, delta, *, diagonal=False, ground_islands=()):
    """Translate a selection together and reroute its connections to fixed parts."""
    if not any(delta):
        return original

    def shift(point):
        return _shift(point, delta)

    def turns(symbol):
        return original.rotations.get(symbol[0].name, 0)

    def pins(symbols):
        return [pin for symbol in symbols for pin in routed_pins(symbol, turns(symbol), original.pin_grid)]

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

    end_pins = [(net, pos) for key, net, pos in selection_ends(original) if key in names]
    if not old and not end_pins:
        return original
    fixed_boxes = [_symbol_bounds(symbol, turns(symbol), body_only=True) for symbol in fixed]
    moved_boxes = [_symbol_bounds(symbol, turns(symbol), body_only=True) for symbol in moved]
    if any(boxes_overlap(body, box) for body in moved_boxes for box in fixed_boxes):
        return None

    old_pins, new_pins, stationary_pins = pins(old), pins(moved), pins(fixed)
    old_pins += end_pins
    new_pins += [(net, shift(pos)) for net, pos in end_pins]
    internal = _selection_nets(original, names, old_pins, stationary_pins, end_pins)
    carried = [island for island in ground_islands if island.net not in internal]
    base = _translate_ground_islands(original, carried, delta, fixed)
    if base is None:
        return None
    moved_grounds = {
        pos: shift(pos) if pos == before and (
            base.ground_names.get(pos, "0") in internal or ("ground", pos) in names
        ) else pos
        for before, pos in zip(original.grounds, base.grounds)
    }
    if len(set(moved_grounds.values())) != len(moved_grounds):
        return None
    connectors = {pos: shift(pos) for i, (net, pos, *_) in enumerate(original.connectors)
                  if net in internal or ("connector", i) in names}
    positions = {**original.positions, **{part.name: a for part, a, _, _ in moved}}
    drawing = replace(
        base, symbols=symbols, positions=positions,
        boundary={net: shift(pos) if ("boundary", net) in names else pos for net, pos in original.boundary.items()},
        wires={net: [(shift(a), shift(b)) for a, b in edges] if net in internal else edges
               for net, edges in base.wires.items()},
        junctions={net: list(map(shift, dots)) if net in internal else dots
                   for net, dots in base.junctions.items()},
        labels={net: shift(pos) if net in internal else pos for net, pos in base.labels.items()},
        grounds=list(moved_grounds.values()),
        ground_names={moved_grounds[pos]: net for pos, net in base.ground_names.items()},
        ground_rotations={moved_grounds[pos]: angle for pos, angle in base.ground_rotations.items()},
        connectors=[(net, connectors.get(pos, pos), *label) for net, pos, *label in original.connectors],
        label_offsets=_move_endpoint_labels(base, grounds=moved_grounds, connectors=connectors),
        crossings={},
    )
    external_old = [pin for pin in old_pins if pin[0] not in internal]
    external_new = [pin for pin in new_pins if pin[0] not in internal]
    external_old, external_new = _external_ground_pins(external_old, external_new, carried)
    if external_old:
        return _reroute_move(
            circuit, base, drawing, external_old, external_new,
            sum(pos[0] for _, pos in old_pins) / len(old_pins),
            stationary_pins=stationary_pins, diagonal=diagonal,
            moving_ends=set(end_pins),
        )
    mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


def symbol_terminal_offsets(drawing, name):
    """Find translations that bring like-net pins together."""
    stationary = defaultdict(list)
    moving = ()
    for symbol in drawing.symbols:
        part = symbol[0]
        pins = routed_pins(symbol, drawing.rotations.get(part.name, 0), drawing.pin_grid)
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
    moving_pins = routed_pins(symbol, turns, drawing.pin_grid)
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
    end = _shift(point, delta)
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
    for mapping in (drawing.ground_names, drawing.ground_rotations):
        if point in mapping:
            mapping[end] = mapping.pop(point)
    return _reroute_move(
        circuit, original, drawing, ((net, point),), ((net, end),), point[0],
        moving_ground=point, diagonal=diagonal, align_axis=align_axis,
        direct=end == ground_attachment(original, point),
    )


@dataclass(frozen=True)
class GroundIsland:
    """A local ground-marker connection owned by the selected symbols."""

    net: str
    grounds: tuple
    pins: frozenset
    points: frozenset
    edges: tuple


def attached_ground_islands(drawing, names):
    """Find grounded wire islands owned entirely by the selected symbols."""
    if not any(isinstance(name, str) for name in names):
        return ()
    markers = defaultdict(list)
    for pos in drawing.grounds:
        markers[drawing.ground_names.get(pos, "0")].append(pos)
    contacts = defaultdict(list)
    for symbol in drawing.symbols:
        part = symbol[0]
        for net, pos in routed_pins(symbol, drawing.rotations.get(part.name, 0), drawing.pin_grid):
            if net in markers:
                contacts[net].append((part.name, clean_point(pos)))

    attached = []
    for net, grounds in markers.items():
        if not any(name in names for name, _ in contacts[net]):
            continue
        extra = list(drawing.junctions.get(net, ()))
        if net in drawing.labels:
            extra.append(drawing.labels[net])
        graph, islands = physical_wire_islands(drawing, net, extra)
        port_islands = {islands[clean_point(pos)] for key, wire, pos in selection_ends(drawing)
                        if wire == net and key[0] != "ground"}
        for island in sorted({islands[clean_point(pos)] for pos in grounds} - port_islands):
            owners = [(name, pos) for name, pos in contacts[net] if islands[pos] == island]
            if not owners or any(name not in names for name, _ in owners):
                continue
            points = frozenset(pos for pos, group in islands.items() if group == island)
            attached.append(GroundIsland(
                net, tuple(pos for pos in grounds if clean_point(pos) in points),
                frozenset(pos for _, pos in owners), points,
                tuple(edge for edge in graph_edges(graph) if edge[0] in points),
            ))
    return tuple(attached)


def _translate_ground_islands(drawing, attachments, delta, stationary):
    if not attachments:
        return drawing
    moved = {(island.net, pos) for island in attachments for pos in island.points}
    grounds = {pos: _shift(pos, delta) for island in attachments for pos in island.grounds}
    positions = [grounds.get(pos, pos) for pos in drawing.grounds]
    if len(set(positions)) != len(positions):
        return None

    translated = [(_shift(a, delta), _shift(b, delta)) for island in attachments for a, b in island.edges]
    translated += [(pos, pos) for pos in grounds.values()]
    for symbol in stationary:
        a, b = _symbol_bounds(symbol, drawing.rotations.get(symbol[0].name, 0), body_only=True)
        if any(segment_intersects_box(edge, a, b) for edge in translated):
            return None

    def translate(net, pos):
        return _shift(pos, delta) if (net, clean_point(pos)) in moved else pos

    return replace(
        drawing,
        wires={net: [(translate(net, a), translate(net, b)) for a, b in edges]
               for net, edges in drawing.wires.items()},
        junctions={net: [translate(net, pos) for pos in dots] for net, dots in drawing.junctions.items()},
        labels={net: translate(net, pos) for net, pos in drawing.labels.items()},
        grounds=positions,
        ground_names={grounds.get(pos, pos): net for pos, net in drawing.ground_names.items()},
        ground_rotations={grounds.get(pos, pos): turns for pos, turns in drawing.ground_rotations.items()},
        label_offsets=_move_endpoint_labels(drawing, grounds=grounds),
        crossings={},
    )


def _external_ground_pins(old_pins, new_pins, attachments):
    if not attachments:
        return old_pins, new_pins
    carried = {(island.net, clean_point(pos))
               for island in attachments for pos in (*island.pins, *island.grounds)}
    pairs = [
        (old, new) for old, new in zip(old_pins, new_pins)
        if (old[0], clean_point(old[1])) not in carried
    ]
    return ([old for old, _ in pairs], [new for _, new in pairs])


def _prune_joined_connectors(circuit, drawing, net):
    """Keep one label per disconnected island, or none once fully wired."""
    _, islands = physical_wire_islands(drawing, net)
    disconnected = len(set(islands.values())) > 1
    retained, removed, seen = [], {}, set()
    for port in drawing.connectors:
        wire, pos, *_ = port
        island = islands.get(clean_point(pos)) if wire == net else None
        if wire != net or (disconnected and island not in seen):
            retained.append(port)
            if wire == net:
                seen.add(island)
        else:
            removed[pos] = None
    # Coincident markers may leave a retained label at the same coordinate.
    for _, pos, *_ in retained:
        removed.pop(pos, None)
    drawing = replace(drawing, connectors=retained,
                      label_offsets=_move_endpoint_labels(drawing, connectors=removed))
    replace_route(drawing, net, drawing.wires.get(net, ()))
    drawing.crossings = {}
    mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


# A label can join another island only when both belong to the exact same net.
# Similar text or a nearby ground symbol is not permission to short two nodes.
def move_connector(circuit, original, index, end, *, join=None, join_wire=False, diagonal=False):
    """Move a label; join a matching label or a different island of its exact net."""
    net, start, name, left, below = original.connectors[index]
    end = clean_point(end)
    if join is None and not join_wire and end == clean_point(start):
        return original
    if join_wire:
        if join is not None or not any(
            segments_intersect((end, end), edge) for edge in original.wires.get(net, ())
        ):
            return None
        _, islands = physical_wire_islands(original, net, (start, end))
        if islands[clean_point(start)] == islands[end]:
            return None
    if join is not None:
        other_net, other_end, other_name, *_ = original.connectors[join]
        if index == join or net != other_net or name != other_name or end != clean_point(other_end):
            return None
    ports = list(original.connectors)
    ports[index] = net, end, name, left, below
    drawing = _reroute_move(
        circuit, original, replace(
            original, connectors=ports, label_offsets=_move_endpoint_labels(original, connectors={start: end})
        ),
        ((net, start),), ((net, end),), start[0], moving_connector=index, diagonal=diagonal,
    )
    if drawing is not None and (join is not None or join_wire):
        drawing = _prune_joined_connectors(circuit, drawing, net)
    return drawing


def move_boundary(circuit, original, net, delta, *, diagonal=False):
    """Move a package port and trim its old lead back to the connected circuit."""
    start = original.boundary[net]
    end = _shift(start, delta)
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


# Replace the cut connection with matching labels or appropriate local ground symbols,
# keeping the electrical net connected even though the visible wire is separated.
def split_wire(circuit, original, net, edge, at, *, grid=0, origin=(0, 0)):
    """Cut a wire into matching labeled ends, or local ground symbols."""
    a, b = sorted(map(clean_point, edge))
    if a == b or net not in original.wires:
        return None
    anchors = route_anchors(original, net) | {a, b}
    graph = wire_graph(original.wires[net], anchors)
    runs = [
        (p, q) for p, q in graph_edges(graph) if math.dist(p, q) >= 0.25
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
    wide_ground = net in circuit.grounds and length >= 2.5 + grid
    if wide_ground:
        # Two inward-facing ground glyphs and captions need substantially more
        # space than connector dots. Include a grid cell for rounding clearance.
        margin = 2.0 + grid
        padding = margin / 2 + min(.25, (length - margin) / 2)
    else:
        padding = margin
    center = max(padding, min(length - padding, along_run))
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
    drawing = route_trial(original, connectors=list(original.connectors))
    if net in circuit.grounds:
        drawing.grounds = list(dict.fromkeys([*original.grounds, low, high]))
        drawing.ground_names = {**original.ground_names, low: net, high: net}
        if wide_ground and math.dist(low, high) >= 2.0 - 1e-8:
            rotations = (3, 1) if axis == 0 else (2, 0)
        else:
            # Short wires cannot fit two facing symbols: point their grounds
            # toward opposite sides of the wire instead of overlapping them.
            rotations = (0, 2) if axis == 0 else (1, 3)
        drawing.ground_rotations = {**original.ground_rotations, low: rotations[0], high: rotations[1]}
    else:
        drawing.connectors.extend([(net, low, name, True, False), (net, high, name, False, True)])
    replace_route(drawing, net, [*graph_edges(graph), (a, low), (high, b)])
    if net in circuit.grounds:
        _prune_split_grounds(drawing, net, (low, high))
    mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


def _prune_split_grounds(drawing, net, added):
    """Discard ground-only cutoffs and old dangling ground leads now replaced."""
    graph, islands = physical_wire_islands(drawing, net)
    pins = {clean_point(pos) for wire, pos in drawing.pins() if wire == net}
    pins.update(clean_point(pos) for wire, pos in drawing.boundary.items() if wire == net)
    useful = {islands[pos] for pos in pins}
    changed = {islands[point] for point in added if point in islands}
    removed = {
        pos: None for pos in drawing.grounds
        if drawing.ground_names.get(pos, "0") == net and (
            islands[clean_point(pos)] not in useful
            or (pos not in added and pos not in pins
                and islands[clean_point(pos)] in changed and len(graph.get(clean_point(pos), ())) == 1)
        )
    }
    drawing.grounds = [pos for pos in drawing.grounds if pos not in removed]
    drawing.ground_names = {pos: wire for pos, wire in drawing.ground_names.items() if pos not in removed}
    drawing.ground_rotations = {pos: turns for pos, turns in drawing.ground_rotations.items() if pos not in removed}
    drawing.label_offsets = _move_endpoint_labels(drawing, grounds=removed)
    edges = [edge for edge in drawing.wires.get(net, ()) if islands[clean_point(edge[0])] in useful]
    replace_route(drawing, net, edges)


def _reroute_move(
    circuit, original, drawing, old_pins, new_pins, center_x, *,
    stationary_pins=None, moving_ground=None, moving_connector=None, moving_boundary=None,
    diagonal=False, rotate=0, align_axis=None, direct=False, docked_pins=(), moving_ends=(), extra_bends=(),
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
        stop = _first_bend(run)
        docked = (net, end) in docked_pins
        if direct or docked:
            stop = len(run) - 1
        elif align_axis is not None:
            stop = next((
                i for i in range(stop, len(run)) if run[i][align_axis] == end[align_axis]
            ), stop)
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
    fixed_edges = {net: list(graph_edges(graph)) for net, graph in graphs.items()}
    bends = (
        ("x", 0.5), ("y", 0.5), ("x", 0), ("y", 0),
        ("x", 1), ("y", 1), ("x", 0.25), ("y", 0.25),
        ("outside", 0.35), ("outside", 0.6),
    )
    if diagonal:
        bends = (("diagonal", 0), *bends)
    if rotate:
        bends += (("diagonal", 0), ("around", 0.7), ("around", 1.3))
    bends += extra_bends
    best = _shortest_move(drawing, circuit, affected, connections, fixed_edges, bends, center_x)
    if best is not None:
        return best
    if docked_pins and not direct:
        return _reroute_move(
            circuit, original, drawing, old_pins, new_pins, center_x,
            stationary_pins=stationary_pins, diagonal=diagonal, rotate=rotate,
            direct=True, docked_pins=docked_pins, extra_bends=extra_bends,
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


def _connection_points(start, end, strategy, ratio, center_x, index):
    """Build a lead according to one of the candidate routing strategies."""
    x, y = start
    u, v = end
    if strategy in {"xy", "yx"}:
        strategy = strategy[index % 2]
    if strategy == "diagonal":
        return start, end
    if strategy == "around":
        approach = min(x, u) - 0.6 if x <= center_x else max(x, u) + 0.6
        exit_lane = min(x, u) - 0.4 if u <= center_x else max(x, u) + 0.4
        across = min(y, v) - ratio if index % 2 == 0 else max(y, v) + ratio
        return start, (approach, y), (approach, across), (exit_lane, across), (exit_lane, v), end
    if strategy == "y":
        lane = y + (v - y) * ratio
        return start, (x, lane), (u, lane), end
    lane = x + (u - x) * ratio
    if strategy == "outside":
        spacing = 0.5 + index * ratio
        lane = min(x, u) - spacing if x <= center_x else max(x, u) + spacing
    return start, (lane, y), (lane, v), end


def _shortest_move(drawing, circuit, affected, connections, fixed_edges, bends, center_x):
    best = None
    for strategy, ratio in bends:
        routed = route_trial(drawing)
        for net in affected:
            routed.wires[net] = list(fixed_edges.get(net, ()))
        for index, (net, start, end) in enumerate(connections):
            points = _connection_points(start, end, strategy, ratio, center_x, index)
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
        mark_crossings(routed)
        if preserves_nets(circuit, routed):
            best = length, routed
    return best[1] if best else None


# Try snapped candidates in order and return the first one that preserves every contact.
# An invalid pointer position should leave the last valid geometry available to the viewer.
def move_wire(circuit, original, net, edge, distance, *, snap=0, pointer=None, accept_offset=None):
    """Return the first electrically valid slide and its snapped offset, if any."""
    for drawing, offset in wire_slide_candidates(original, net, edge, distance, snap=snap, pointer=pointer):
        if accept_offset is not None and not accept_offset(distance if offset is None else offset):
            continue
        if drawing is original:
            return drawing, None
        mark_crossings(drawing)
        if preserves_nets(circuit, drawing):
            return drawing, offset
    return None, None
