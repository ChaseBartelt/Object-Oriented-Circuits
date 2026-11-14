"""Pick the visible packages and their contents from the compiled graph."""

from collections import Counter, defaultdict
from dataclasses import replace

from .graph import Component

METHOD_PREFIX = "@method:"


def hood_levels(circuit, expanded):
    """Build the top level and the opened packages, in parent-first order."""
    levels = {None: []}
    children = []
    groups = {}
    for index, method in enumerate(circuit.instances):
        ancestor = (
            None if method.parent is None else children[method.parent]
        )
        if ancestor not in levels:
            children.append(ancestor)
            continue
        if ancestor is None and method.kind not in {"chip", "gate"}:
            children.append(ancestor)
            continue
        name = f"{METHOD_PREFIX}{index}"
        children.append(name)
        groups[name] = (ancestor, method, [])
        if name in expanded:
            levels[name] = []

    for part in circuit.components:
        owner = circuit.owners.get(part.name)
        level = children[owner] if owner is not None else None
        if level not in levels:
            groups[level][2].append(part)
            continue
        if part.kind == "Q":
            pins = tuple(zip(("base", "collector", "emitter"), part.terminal_nets))
            levels[level].append(replace(part, kind="H", package_pins=pins))
        else:
            levels[level].append(part)

    usage = defaultdict(Counter)
    totals = Counter()
    for part in circuit.components:
        owner = circuit.owners.get(part.name)
        level = children[owner] if owner is not None else None
        nets = set(part.terminal_nets)
        totals.update(nets)
        usage[level].update(nets)

    for name, (parent, method, members) in reversed(groups.items()):
        inside = usage[name]
        usage[parent].update(inside)
        contents = levels.get(name, members)
        if not contents and not method.body and name not in expanded:
            continue
        pins = tuple((pin, circuit.resolve_wire(net)) for pin, net in method.pins)
        exposed = {net for _, net in pins}
        nets = {net for part in contents for net in part.terminal_nets}
        pins += tuple(
            (net, net) for net in sorted(nets - exposed)
            if net in circuit.grounds or inside[net] < totals[net]
        )
        levels[parent].append(
            Component(
                name, "H", "", "", label=method.name,
                order=min((part.order for part in contents), default=0),
                package_pins=pins,
            )
        )

    for name, parts in levels.items():
        if name is None and len(parts) == len(circuit.components) and all(
            part is original for part, original in zip(parts, circuit.components)
        ):
            yield name, circuit, (), ()
            continue
        parts.sort(key=lambda part: part.order)
        names = {part.name for part in parts if part.kind != "H"}
        method = groups[name][1] if name is not None else None
        boundary = tuple(dict.fromkeys(
            net for part in levels[groups[name][0]] if part.name == name
            for _, net in part.package_pins
        )) if method else ()
        yield name, replace(
            circuit, components=parts, groups=[],
            wire_taps=[tap for tap in circuit.wire_taps if tap.component in names],
        ), method.body if method else (), boundary
