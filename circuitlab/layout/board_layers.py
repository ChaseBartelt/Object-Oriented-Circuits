"""Classify and edit board faces while keeping the electrical wire union."""

from dataclasses import replace
from math import dist

from circuitlab.layout.geometry import routed_pins, wire_axis
from circuitlab.layout.wire_edit import (
    clean_point, graph_edges, merge_strokes, route_anchors, wire_graph, wire_slide_candidates,
)


def wire_subset(segments, coverage):
    """Check complete collinear coverage, including several adjoining strokes."""
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = dx * dx + dy * dy
        if length < 1e-16:
            return False
        spans = []
        for c, d in coverage:
            if any(abs((at[0] - a[0]) * dy - (at[1] - a[1]) * dx) > 1e-7 for at in (c, d)):
                continue
            first, last = sorted(((at[0] - a[0]) * dx + (at[1] - a[1]) * dy) / length for at in (c, d))
            if last >= -1e-8 and first <= 1 + 1e-8:
                spans.append((max(0, first), min(1, last)))
        reached = 0.0
        for first, last in sorted(spans):
            if first > reached + 1e-8:
                break
            reached = max(reached, last)
        if reached < 1 - 1e-8:
            return False
    return True


def _partition(edges, underside):
    anchors = {at for edge in underside for at in edge}
    graph = wire_graph(edges, anchors)
    top, back = [], []
    for edge in graph_edges(graph):
        (back if wire_subset((edge,), underside) else top).append(edge)
    return sorted(merge_strokes(top)) if top else [], sorted(merge_strokes(back)) if back else []


def layer_wires(drawing, *, underside=False):
    """Return only the requested face, splitting merged union strokes as needed."""
    if not drawing.underside_wires:
        return {} if underside else drawing.wires
    result = {}
    for net, edges in drawing.wires.items():
        top, back = _partition(edges, drawing.underside_wires.get(net, ()))
        if selected := back if underside else top:
            result[net] = selected
    return result


def set_wire_layer(drawing, net, *, edge=None, pin=None, point=None, underside=True):
    """Move the clicked branch to a face, stopping at terminals and junctions."""
    edges = drawing.wires.get(net, ())
    if not edges:
        return drawing
    anchors = route_anchors(drawing, net)
    cuts = {at for segment in drawing.underside_wires.get(net, ()) for at in segment}
    graph = wire_graph(edges, anchors | cuts | ({clean_point(pin)} if pin is not None else set()))
    if pin is not None:
        at = clean_point(pin)
        seeds = [(at, other) for other in graph.get(at, ())]
    elif edge is not None:
        options = [candidate for candidate in graph_edges(graph) if wire_subset((candidate,), (edge,))]
        if not options:
            return drawing
        clicked = point or tuple((a + b) / 2 for a, b in zip(*edge))

        def proximity(candidate):
            a, b = candidate
            axis = wire_axis(candidate)
            if axis is None:
                return min(dist(clicked, at) for at in candidate)
            low, high = sorted((a[axis], b[axis]))
            return max(low - clicked[axis], clicked[axis] - high, 0)

        seeds = [min(options, key=lambda candidate: (proximity(candidate), candidate))]
    else:
        return drawing
    # A change of face is a fixed physical through-hole attachment too.
    for at in graph:
        faces = {wire_subset(((at, other),), drawing.underside_wires.get(net, ())) for other in graph[at]}
        if len(faces) > 1:
            anchors.add(at)
    selected = set()
    pending = list(seeds)
    while pending:
        a, b = pending.pop()
        current = tuple(sorted((a, b)))
        if current in selected:
            continue
        selected.add(current)
        for at, previous in ((a, b), (b, a)):
            if at not in anchors and len(graph[at]) == 2:
                pending.extend((at, other) for other in graph[at] if other != previous)
    old = drawing.underside_wires.get(net, [])
    if underside:
        if wire_subset(selected, old):
            return drawing
        updated = sorted(merge_strokes([*old, *selected]))
    else:
        updated, removed = _partition(old, selected)
        if not removed:
            return drawing
    layers = dict(drawing.underside_wires)
    if updated:
        layers[net] = updated
    else:
        layers.pop(net, None)
    from circuitlab.layout.checks import mark_crossings
    result = replace(drawing, underside_wires=layers, crossings={}, junctions={})
    mark_crossings(result)
    return result


def remap_wire_layers(original, drawing, *, net=None, underside=None):
    """Keep classifications after routing edits and classify new active-face strokes."""
    if (drawing.underside_wires != original.underside_wires
            and all(wire_subset(edges, drawing.wires.get(name, ())) for name, edges in drawing.underside_wires.items())):
        return drawing
    layers = {}
    for name, edges in drawing.wires.items():
        old = original.underside_wires.get(name, ())
        active = net is None or name == net
        if not old and not (active and underside is True):
            continue
        boundaries = {at for edge in [*original.wires.get(name, ()), *old] for at in edge}
        graph = wire_graph(edges, boundaries)
        selected = []
        for edge in graph_edges(graph):
            previous = wire_subset((edge,), original.wires.get(name, ()))
            if wire_subset((edge,), old) or (active and not previous and (
                    underside is True or underside is None and old and wire_subset(original.wires.get(name, ()), old))):
                selected.append(edge)
        if selected:
            layers[name] = sorted(merge_strokes(selected))
    result = replace(drawing, underside_wires=layers)
    if underside is None and original.underside_wires:
        from circuitlab.layout.checks import segments_intersect

        old_top = layer_wires(original)

        def carried(name, before, after):
            nonlocal result
            if net is not None and name != net:
                return
            attached = lambda edges: any(segments_intersect((before, before), edge) for edge in edges)
            if not attached(original.underside_wires.get(name, ())) or attached(old_top.get(name, ())):
                return
            graph = wire_graph(result.wires.get(name, ()), route_anchors(result, name))
            # A moved leaf remains one branch. Never recolor a new tee's
            # unrelated outgoing branches merely because one pin was on back.
            if len(graph.get(clean_point(after), ())) == 1:
                result = set_wire_layer(result, name, pin=after, underside=True)

        previous = {symbol[0].name: symbol for symbol in original.symbols}
        for symbol in drawing.symbols:
            name = symbol[0].name
            old = previous.get(name)
            if old is None:
                continue
            old_pins = routed_pins(old, original.rotations.get(name, 0), original.pin_grid)
            new_pins = routed_pins(symbol, drawing.rotations.get(name, 0), drawing.pin_grid)
            for (wire, before), (_, after) in zip(old_pins, new_pins):
                carried(wire, before, after)
        for wire, after in drawing.boundary.items():
            if wire in original.boundary:
                carried(wire, original.boundary[wire], after)
        for index, (wire, after, *_) in enumerate(drawing.connectors):
            if index < len(original.connectors):
                carried(wire, original.connectors[index][1], after)
        for at in drawing.grounds:
            if at in original.grounds:
                carried(drawing.ground_names.get(at, "0"), at, at)
    return result


def move_board_wire(circuit, drawing, net, edge, distance, *, underside=False,
                    snap=0, pointer=None, accept_offset=None, repair=False):
    """Slide one face independently, retaining opposite-face attachment holes."""
    from circuitlab.layout.checks import clear_of_bodies, mark_crossings, preserves_nets

    active = layer_wires(drawing, underside=underside)
    opposite = layer_wires(drawing, underside=not underside)
    if not wire_subset((edge,), active.get(net, ())):
        return None, None
    fixed = {at for segment in opposite.get(net, ()) for at in segment}
    temporary = [*drawing.connectors, *((net, at, "_board_attachment", True, False) for at in fixed)]
    face = replace(drawing, wires={name: list(edges) for name, edges in active.items()},
                   underside_wires=active if underside else {}, connectors=temporary)
    for candidate, offset in wire_slide_candidates(face, net, edge, distance, snap=snap, pointer=pointer):
        if accept_offset is not None and not accept_offset(distance if offset is None else offset):
            continue
        if candidate is face:
            return drawing, None
        wires = {name: list(edges) for name, edges in opposite.items()}
        for name, edges in candidate.wires.items():
            wires.setdefault(name, []).extend(edges)
        result = replace(drawing, wires=wires, crossings={}, junctions=dict(candidate.junctions),
                         labels=dict(candidate.labels), underside_wires=candidate.wires if underside else opposite)
        mark_crossings(result)
        if preserves_nets(circuit, result, nets={net} if repair else None) and (
                underside or clear_of_bodies(candidate.wires.get(net, ()), result, underside=False)):
            return result, offset
    return None, None
