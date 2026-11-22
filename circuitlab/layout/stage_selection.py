"""Find the nearby passive parts that travel with an amplifier stage."""

from collections import defaultdict

from circuitlab.layout.geometry import ChipPackage, along, package_body_corners, rotate_point, routed_pins
from circuitlab.layout.wire_edit import clean_point, physical_wire_islands


PASSIVE_KINDS = {"R", "C", "L", "X", "Z"}
SUPPLY_PINS = {"vcc", "vee", "vdd", "vss", "vplus", "vminus", "v+", "v-", "vs+", "vs-"}
SIDE_REACH = 4.5
SERIES_REACH = 9


def local_stage_selection(circuit, drawing, names):
    """Add the feedback and side passives physically wired to selected amplifiers."""
    stages = [symbol for symbol in drawing.symbols if symbol[0].name in names
              and (isinstance(symbol[0], ChipPackage) or symbol[0].kind == "O")]
    if not stages:
        return frozenset(names)

    power = set(circuit.grounds)
    owners = defaultdict(set)
    passives = {}
    turns = drawing.rotations
    grid = drawing.pin_grid
    for symbol in drawing.symbols:
        part, a, b, _ = symbol
        power.update(net for net in (part.vplus, part.vminus) if net)
        power.update(net for label, net in part.package_pins if label.casefold() in SUPPLY_PINS)
        for net in part.terminal_nets:
            owners[net].add(part.name)
        if part.kind in PASSIVE_KINDS and part.p != part.n:
            pins = dict(routed_pins(symbol, turns.get(part.name, 0), grid))
            passives[part.name] = along(a, b, .5), pins

    islands = {}

    def linked(net, first, second):
        # Matching connector names can connect distant parts electrically.
        # Only draw them into a stage when a physical wire joins their pins.
        if net not in islands:
            _, islands[net] = physical_wire_islands(drawing, net)
        return islands[net][clean_point(first)] == islands[net][clean_point(second)]

    selected = set(names)
    for symbol in stages:
        part, a, b, _ = symbol
        contacts = defaultdict(list)
        rotation = turns.get(part.name, 0)
        for net, pos in routed_pins(symbol, rotation, grid):
            if net not in power:
                contacts[net].append(pos)
        height = a[1] - package_body_corners(part, a, b, grid)[0][1] if part.kind == "H" else .9
        side_leads = []
        left, right = sorted((a[0], b[0]))
        for name, (center, pins) in passives.items():
            x, y = rotate_point(center, a, b, -rotation)
            gap = max(left - x, x - right)
            if gap > SIDE_REACH or abs(y - a[1]) > height + 4:
                continue
            attached = {
                net for net, pin in pins.items()
                if any(linked(net, pin, end) for end in contacts.get(net, ()))
            }
            if len(attached) == 2 and gap <= 2:
                selected.add(name)
            elif len(attached) == 1 and gap > 0 and abs(y - a[1]) <= height + 2:
                other, = pins.keys() - attached
                if other not in power or other in circuit.grounds:
                    selected.add(name)
                    side_leads.append((name, other, -1 if x < left else 1, pins[other]))

        # An exclusive side net can carry one more passive in the same chain.
        for name, net, direction, at in side_leads:
            if net in power or len(owners[net]) != 2:
                continue
            other, = owners[net] - {name}
            if other not in passives:
                continue
            center, pins = passives[other]
            x, y = rotate_point(center, a, b, -rotation)
            side = left if direction < 0 else right
            if (0 < direction * (x - side) <= SERIES_REACH and abs(y - a[1]) <= height + 2
                    and linked(net, at, pins[net])):
                selected.add(other)
    return frozenset(selected)
