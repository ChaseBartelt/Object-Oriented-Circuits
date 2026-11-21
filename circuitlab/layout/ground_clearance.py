"""Keep decorative ground strokes clear of nearby schematic geometry."""

from dataclasses import replace
from math import dist

from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.editing import ground_attachment, move_ground
from circuitlab.layout.geometry import rotate_point, routed_pins, wire_axis
from circuitlab.layout.wire_edit import clean_point, replace_route, route_anchors, route_trial, terminal_run, wire_graph


def ground_strokes(point, turns=0):
    """Return the stem and three bars in the drawing's coordinate system."""
    x, y = point
    bars = tuple(
        ((x - width, y - height), (x + width, y - height))
        for width, height in ((.32, .27), (.23, .39), (.14, .51))
    )
    edges = ((point, (x, y - .27)), *bars)
    return tuple(tuple(rotate_point(p, point, point, turns) for p in edge) for edge in edges)


def _ground_ink(point, turns):
    stem, *bars = ground_strokes(point, turns)
    # A lead is meant to meet the start of the stem.
    a, b = stem
    start = tuple(x + .1 * (y - x) for x, y in zip(a, b))
    return ((start, b), *bars)


def _visible_contacts(drawing):
    contacts = {net: list(drawing.visible_segments(net)) for net in drawing.wires}
    for net, pos in (*drawing.pins(), *drawing.boundary.items()):
        contacts.setdefault(net, []).append((pos, pos))
    for net, pos, *_ in drawing.connectors:
        contacts.setdefault(net, []).append((pos, pos))
    return contacts


def _ground_is_blocked(point, drawing, visible_contacts):
    ink = _ground_ink(point, drawing.ground_rotations.get(point, 0))
    contacts = [edge for edges in visible_contacts.values() for edge in edges]
    for other in drawing.grounds:
        if other != point:
            contacts.extend(ground_strokes(other, drawing.ground_rotations.get(other, 0)))
    return any(segments_intersect(stroke, edge) for stroke in ink for edge in contacts)


def _validated_contacts(drawing, trial, net, point, end, blocked):
    """Check the new route and keep existing clear markers unobstructed."""
    if trial is None:
        return None
    previous = {frozenset(edge) for edge in drawing.wires.get(net, ())}
    added = [edge for edge in trial.wires.get(net, ()) if frozenset(edge) not in previous]
    if not clear_of_bodies(added, trial):
        return None
    contacts = _visible_contacts(trial)
    if _ground_is_blocked(end, trial, contacts) or any(
        _ground_is_blocked(pos, trial, contacts)
        for pos in drawing.grounds if pos != point and pos not in blocked
    ):
        return None
    return contacts


def _exclusive_run(drawing, net, point):
    """Find a ground lead that serves only one actual terminal."""
    anchored = route_anchors(drawing, net, moving_ground=point)
    graph = wire_graph(drawing.wires.get(net, ()), anchored | {point})
    if len(graph.get(point, ())) != 1:
        return ()
    run = terminal_run(graph, point, anchored)
    terminal = run[-1]
    if len(graph.get(terminal, ())) != 1 or sum(
        name == net and clean_point(pos) == terminal for name, pos in drawing.pins()
    ) != 1:
        return ()
    return run


def _face_ground_leads(drawing, contacts):
    """Point a short horizontal ground marker away from its sole terminal."""
    for point in drawing.grounds:
        if point in drawing.boundary.values():
            continue
        net = drawing.ground_names.get(point, "0")
        run = _exclusive_run(drawing, net, point)
        if not run or any(wire_axis(edge) != 0 for edge in zip(run, run[1:])):
            continue
        offset = point[0] - run[-1][0]
        if not 0 < abs(offset) <= 2:
            continue
        turns = 1 if offset < 0 else 3
        if drawing.ground_rotations.get(point, 0) == turns:
            continue
        rotated = replace(drawing, ground_rotations={**drawing.ground_rotations, point: turns})
        if (clear_of_bodies(ground_strokes(point, turns), rotated)
                and not _ground_is_blocked(point, rotated, contacts)):
            drawing = rotated
    return drawing


def _direct_ground_run(circuit, drawing, net, run):
    """Trim only this leaf of its net, retaining every other wire edge."""
    removed = {frozenset(edge) for edge in zip(run, run[1:])}
    graph = wire_graph(drawing.wires[net], route_anchors(drawing, net))
    edges = [(a, b) for a, neighbors in graph.items() for b in neighbors
             if a < b and frozenset((a, b)) not in removed]
    trial = route_trial(drawing)
    replace_route(trial, net, [*edges, (run[0], run[-1])])
    mark_crossings(trial)
    return trial if preserves_nets(circuit, trial) else None


def _ground_endpoint(pin, gap, turns):
    return clean_point(rotate_point((pin[0], pin[1] - gap), pin, pin, turns))


def _move_aligned_lead(circuit, drawing, net, run, end, axis, *, at_terminal=False):
    """Move the marker, then remove any elbows left on its dedicated lead."""
    point, pin = run[0], run[-1]
    trial = drawing
    if end != point:
        delta = tuple(b - a for a, b in zip(point, end))
        trial = move_ground(circuit, drawing, point, delta, align_axis=1 - axis)
    if trial is None or at_terminal:
        return trial
    moved_run = _exclusive_run(trial, net, end)
    if not moved_run or moved_run[-1] != pin:
        return None
    if any(wire_axis(edge) != axis for edge in zip(moved_run, moved_run[1:])):
        return _direct_ground_run(circuit, trial, net, moved_run)
    return trial


def _opamp_input_directions(drawing):
    inputs = {}
    for symbol in drawing.symbols:
        part = symbol[0]
        if part.kind != "O":
            continue
        rotation = drawing.rotations.get(part.name, 0)
        turns = (1 + rotation) % 4
        if turns in (1, 3):
            for net, pin in routed_pins(symbol, rotation, drawing.pin_grid)[:2]:
                inputs[net, clean_point(pin)] = turns
    return inputs


def _near_parallel_stroke(strokes, wires):
    """Avoid glyphs that would visually merge with a nearby parallel wire."""
    for stroke in strokes:
        axis = wire_axis(stroke)
        across = 1 - axis
        low, high = sorted(pos[axis] for pos in stroke)
        for edge in wires:
            if wire_axis(edge) == axis and edge[0] != edge[1]:
                start, end = sorted(pos[axis] for pos in edge)
                if max(low, start) <= min(high, end) and abs(stroke[0][across] - edge[0][across]) < .08:
                    return True
    return False


def _place_opamp_grounds(circuit, drawing, step, blocked, budget):
    """Replace jogged op-amp ground leads with short, clear local returns."""
    inputs = _opamp_input_directions(drawing)
    for point in drawing.grounds:
        if point in drawing.boundary.values():
            continue
        net = drawing.ground_names.get(point, "0")
        run = _exclusive_run(drawing, net, point)
        if not run or (net, run[-1]) not in inputs:
            continue
        axes = [wire_axis(edge) for edge in zip(run, run[1:])]
        if None in axes or len(set(axes)) < 2:
            continue
        pin = run[-1]
        outward = inputs[net, pin]
        old_length = sum(dist(a, b) for a, b in zip(run, run[1:]))
        foreign = [edge for name, wires in drawing.wires.items() if name != net for edge in wires]
        for turns, gap in (
            (outward, step), (outward, 0), (0, step), (0, 2 * step), (0, 3 * step),
        ):
            end = _ground_endpoint(pin, gap, turns)
            if (gap >= old_length or dist(point, end) > 3
                    or end in drawing.boundary.values() or end in drawing.grounds):
                continue
            strokes = ground_strokes(end, turns)
            if (not clear_of_bodies(strokes, drawing)
                    or any(segments_intersect((pin, end), edge) for edge in foreign)
                    or _near_parallel_stroke(strokes, foreign)):
                continue
            if budget <= 0:
                return drawing, budget
            budget -= 1
            axis = 1 - turns % 2
            trial = _move_aligned_lead(circuit, drawing, net, run, end, axis, at_terminal=not gap)
            if trial is None:
                continue
            rotations = dict(trial.ground_rotations)
            if turns:
                rotations[end] = turns
            else:
                rotations.pop(end, None)
            trial = replace(trial, ground_rotations=rotations)
            if sum(map(len, trial.crossings.values())) > sum(map(len, drawing.crossings.values())):
                continue
            contacts = _validated_contacts(drawing, trial, net, point, end, blocked)
            if contacts is not None:
                drawing = trial
                blocked = {pos for pos in blocked - {point} if _ground_is_blocked(pos, drawing, contacts)}
                break
    return drawing, budget


def _straighten_ground_leads(circuit, drawing, step, blocked, budget):
    for point in drawing.grounds:
        if point in blocked or point in drawing.boundary.values():
            continue
        net = drawing.ground_names.get(point, "0")
        run = _exclusive_run(drawing, net, point)
        axes = [wire_axis(edge) for edge in zip(run, run[1:])]
        if None in axes or sum(a != b for a, b in zip(axes, axes[1:])) < 2:
            continue
        pin = run[-1]
        turns = drawing.ground_rotations.get(point, 0)
        axis = 1 - turns % 2
        along = abs(point[axis] - pin[axis])
        length = sum(dist(a, b) for a, b in zip(run, run[1:]))
        gaps = {step, 2 * step, 3 * step, round(along / step) * step}
        ends = [_ground_endpoint(pin, gap, turns) for gap in gaps if 0 < gap < length]
        for end in sorted(ends, key=lambda position: (dist(point, position), position)):
            if (end != point and end in drawing.grounds
                    or end in drawing.boundary.values() or dist(point, end) > 1.25):
                continue
            if not clear_of_bodies(ground_strokes(end, turns), drawing):
                continue
            if budget <= 0:
                return drawing
            budget -= 1
            trial = _move_aligned_lead(circuit, drawing, net, run, end, axis)
            if _validated_contacts(drawing, trial, net, point, end, blocked) is not None:
                drawing = trial
                break
    return drawing


def _move_blocked_markers(circuit, drawing, step, contacts, blocked, budget):
    """Try nearby positions, favoring moves along the ground lead's axis."""
    for point in drawing.grounds:
        if point not in blocked or point in drawing.boundary.values():
            continue
        net = drawing.ground_names.get(point, "0")
        turns = drawing.ground_rotations.get(point, 0)
        attachment = ground_attachment(drawing, point)
        axis = wire_axis((point, attachment)) if attachment != point else None
        if axis is None:
            axis = 1 - turns % 2
        candidates = sorted(
            (distance + (.35 if direction != axis else 0), distance, direction, sign)
            for i in range(1, 5) for distance in (i * step,)
            for direction in (0, 1) for sign in (-1, 1)
        )
        obstacles = [edge for wire, edges in contacts.items() if wire != net for edge in edges]
        for _, distance, direction, sign in candidates:
            delta = (sign * distance, 0) if direction == 0 else (0, sign * distance)
            end = clean_point(tuple(x + offset for x, offset in zip(point, delta)))
            ink = ground_strokes(end, turns)
            if (end in drawing.boundary.values() or end in drawing.grounds
                    or any(segments_intersect(stroke, edge) for stroke in ink for edge in obstacles)
                    or not clear_of_bodies(ink, drawing)):
                continue
            if budget <= 0:
                return drawing, blocked, budget
            budget -= 1
            trial = move_ground(circuit, drawing, point, delta)
            trial_contacts = _validated_contacts(drawing, trial, net, point, end, blocked)
            if trial_contacts is None:
                continue
            drawing, contacts = trial, trial_contacts
            blocked = {pos for pos in blocked - {point} if _ground_is_blocked(pos, drawing, contacts)}
            break
    return drawing, blocked, budget


def clear_ground_marks(circuit, drawing, *, grid=.25, budget=12, face_leads=False, opamp_sideways=False):
    """Clear ground glyphs and straighten short leads to single grounded pins."""
    if not drawing.grounds:
        return drawing
    step = drawing.pin_grid or grid
    contacts = _visible_contacts(drawing)
    if face_leads:
        drawing = _face_ground_leads(drawing, contacts)
    before_repair = drawing
    blocked = {pos for pos in drawing.grounds if _ground_is_blocked(pos, drawing, contacts)}
    if face_leads or opamp_sideways:
        drawing, budget = _place_opamp_grounds(circuit, drawing, step, blocked, budget)
        if drawing is not before_repair:
            contacts = _visible_contacts(drawing)
            blocked = {pos for pos in drawing.grounds if _ground_is_blocked(pos, drawing, contacts)}
    drawing, blocked, budget = _move_blocked_markers(circuit, drawing, step, contacts, blocked, budget)
    drawing = _straighten_ground_leads(circuit, drawing, step, blocked, budget)
    if face_leads and drawing is not before_repair:
        # A repaired marker may have crossed its pin or gained a straight lead.
        drawing = _face_ground_leads(drawing, _visible_contacts(drawing))
    return drawing
