"""Measure routing changes and validate placement candidates."""

import math
from dataclasses import replace

from circuitlab.layout.checks import clear_of_bodies, preserves_nets
from circuitlab.layout.geometry import symbol_bounds, wire_axis
from circuitlab.layout.wire_edit import count_wire_bends


def wire_length(layout):
    return sum(math.dist(a, b) for edges in layout.wires.values() for a, b in edges)


def crossing_count(layout):
    return sum(map(len, layout.crossings.values()))


def wiring_cost(layout):
    """Price crossings and extra segments alongside the total wire length."""
    return (wire_length(layout) + 10 * crossing_count(layout)
            + count_wire_bends(layout) + .4 * sum(map(len, layout.wires.values())))


def layout_span(layout):
    points = [point for symbol in layout.symbols
              for point in symbol_bounds(symbol, layout.rotations.get(symbol[0].name, 0))]
    points += [point for edges in layout.wires.values() for edge in edges for point in edge]
    if not points:
        return 0
    return sum(max(axis) - min(axis) for axis in zip(*points))


def _wires_and_contacts(layout):
    edges = [edge for route in layout.wires.values() for edge in route]
    contacts = [pin for _, pin in layout.pins()]
    contacts.extend(layout.grounds)
    contacts.extend(layout.boundary.values())
    contacts.extend(point for _, point, *_ in layout.connectors)
    return edges + [(point, point) for point in contacts]


def improves_placement(circuit, before, after, *, moved=None, grid=0, compact=False, straightens_input=False):
    """Accept a cleaner placement after checking connections and body clearance."""
    length_change = wire_length(after) - wire_length(before)
    if length_change > 1e-8:
        return False
    improvement = wiring_cost(before) - wiring_cost(after)
    old_crossings, new_crossings = crossing_count(before), crossing_count(after)
    cheaper = improvement > 1e-8
    shorter = cheaper and length_change < -1e-8
    cleaner = cheaper and new_crossings < old_crossings
    simpler = cheaper and new_crossings <= old_crossings and count_wire_bends(after) < count_wire_bends(before)
    tighter = compact and improvement >= -1e-8 and layout_span(after) < layout_span(before) - 1e-8
    aligned = straightens_input and improvement >= -1e-8
    if not (shorter or cleaner or simpler or tighter or aligned):
        return False

    old_edges = {frozenset(edge) for route in before.wires.values() for edge in route}
    added = [edge for route in after.wires.values() for edge in route if frozenset(edge) not in old_edges]
    if any(wire_axis(edge) is None for edge in added):
        return False
    grid = grid or before.pin_grid
    if grid and any(abs(value / grid - round(value / grid)) > 1e-6
                    for edge in added for point in edge for value in point):
        return False

    # A moved body can cover a wire or an unattached contact without moving the wire itself.
    if not clear_of_bodies(_wires_and_contacts(after) if compact else added, after):
        return False
    if moved is not None and not clear_of_bodies(_wires_and_contacts(after), replace(after, symbols=[moved])):
        return False
    return preserves_nets(circuit, after)
