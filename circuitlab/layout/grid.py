"""Align a whole existing drawing without altering its electrical graph."""
# Grid alignment changes geometry as one explicit operation, not the electrical model.
# Rounded terminals and wires must still preserve all original connections; otherwise the
# candidate is rejected instead of forcing nearby distinct nets onto one grid point.

import math
from collections import defaultdict
from dataclasses import replace

from circuitlab.layout.checks import clear_package_wires, mark_crossings, preserves_nets, segments_intersect
from circuitlab.layout.editing import move_ground
from circuitlab.layout.wire_edit import clean_point, replace_route, route_anchors, wire_graph


# Build and validate a complete aligned candidate before changing the displayed layout.
# This lets the viewer keep one undo step for an operation affecting many terminals.
def align_layout(circuit, original, step=0.25, *, prune_cycles=False, keep_package_clearance=False):
    """Return a connected grid layout, or None if rounding would join distinct nets.

    Pin spacing belongs to the drawing, not the component/model. Legacy layouts
    keep their original geometry until this explicit operation is requested.
    Fresh layouts may also require the original clearance around package bodies.
    """
    if not math.isfinite(step) or not 0 < step <= 16:
        raise ValueError("Grid spacing must be positive and at most 16")
    # Small rounding biases can resolve a congested half-cell without relocating
    # the circuit. Never accept a partial result or change the netlist to fit it.
    for bias in (0, -0.2, 0.2):
        drawing = _aligned(original, step, bias, prune_cycles=prune_cycles)
        if keep_package_clearance and not clear_package_wires(drawing):
            continue
        mark_crossings(drawing)
        if preserves_nets(circuit, drawing):
            return drawing
        repaired = _clear_ground_contact(circuit, drawing, step)
        if repaired is not None and (not keep_package_clearance or clear_package_wires(repaired)):
            return repaired
    return None


def _clear_ground_contact(circuit, drawing, step):
    """Short ground leads may land on a neighboring trunk after rounding."""
    pins = list(drawing.pins())
    blocked = []
    for pos in drawing.grounds:
        net = drawing.ground_names.get(pos, "0")
        other_edges = [edge for other in drawing.wires if other != net
                       for edge in drawing.visible_segments(other)]
        other_edges.extend((pin, pin) for other, pin in pins if other != net)
        if not any(segments_intersect((pos, pos), edge) for edge in other_edges):
            continue
        blocked.append((pos, net))
        if len(blocked) > 1:
            # This repair moves ONE ground. Other obstructed ground endpoints
            # would remain invalid in every routing candidate. Do not perform
            # hundreds of full-graph route checks for a repair that cannot work.
            return None
    for pos, net in blocked:
        targets = [pin for other, pin in pins if other == net]
        directions = ((step, 0), (-step, 0), (0, step), (0, -step))
        for delta in sorted(directions, key=lambda d: min(
            (math.dist((pos[0] + d[0], pos[1] + d[1]), pin) for pin in targets), default=0
        )):
            moved = move_ground(circuit, drawing, pos, delta)
            if moved is not None and all(
                abs(value / step - round(value / step)) < 1e-6
                for edges in moved.wires.values() for edge in edges for p in edge for value in p
            ):
                return moved
    return None


def _aligned(original, step, bias, *, prune_cycles=False):
    def snap(pos):
        return tuple(round(math.floor(value / step + 0.5 + bias) * step, 8) for value in pos)

    symbols = []
    for part, a, b, top in original.symbols:
        # Grid-sized half-lengths keep all four rotations on the same grid.
        center = snap(tuple((x + y) / 2 for x, y in zip(a, b)))
        half = tuple(round(math.copysign(math.floor(abs(y - x) / (2 * step) + 1e-8) * step, y - x), 8)
                     for x, y in zip(a, b))
        if not any(half):
            axis = max(range(2), key=lambda i: abs(b[i] - a[i]))
            half = tuple(math.copysign(step, b[i] - a[i]) if i == axis else 0 for i in range(2))
        a = clean_point(tuple(c - h for c, h in zip(center, half)))
        b = clean_point(tuple(c + h for c, h in zip(center, half)))
        symbols.append((part, a, b, top))

    drawing = replace(
        original, pin_grid=step, symbols=symbols,
        positions={part.name: a for part, a, _, _ in symbols},
        wires={}, junctions={}, crossings={},
        grounds=list(dict.fromkeys(map(snap, original.grounds))),
        ground_names={snap(pos): net for pos, net in original.ground_names.items()},
        ground_rotations={snap(pos): turns for pos, turns in original.ground_rotations.items()},
        connectors=[(net, snap(pos), *caption) for net, pos, *caption in original.connectors],
        boundary={net: snap(pos) for net, pos in original.boundary.items()},
        labels={net: snap(pos) for net, pos in original.labels.items()},
        label_offsets={
            (key[0], *snap(key[1:])) if key[0] in {"ground", "connector"} else key: offset
            for key, offset in original.label_offsets.items()
        },
    )
    # Split at terminals/tees BEFORE rounding: a terminal on the middle of a
    # diagonal must remain attached after its two neighboring vertices move.
    routes = defaultdict(list)
    for net, edges in original.wires.items():
        graph = wire_graph(edges, route_anchors(original, net))
        snapped = {pos: snap(pos) for pos in graph}
        for a, neighbors in graph.items():
            for b in neighbors:
                if a < b and snapped[a] != snapped[b]:
                    routes[net].append((snapped[a], snapped[b]))

    # Updated pin spacing may differ slightly from independently rounded wire
    # ends. Join them on grid rows/columns and let compact_wire remove old stubs.
    for (net, old), (_, new) in zip(original.pins(), drawing.pins()):
        old, new = snap(old), clean_point(new)
        bend = new[0], old[1]
        for a, b in ((old, bend), (bend, new)):
            if a != b:
                routes[net].append((a, b))
    for net, edges in routes.items():
        replace_route(drawing, net, edges, prune_cycles=prune_cycles)
    return drawing
