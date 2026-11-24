"""Pick the visible packages and their contents from the compiled graph."""
# The compiled graph stays flat for solving, but drawing can show a package or its contents.
# Build visible levels in parent-first order so opened child sheets retain their enclosing
# instance identity and terminal bindings.

from collections import Counter, defaultdict
from dataclasses import replace

from circuitlab.layout.geometry import ChipPackage
from circuitlab.model.graph import Component

METHOD_PREFIX = "@method:"


def hood_levels(circuit, expanded):
    """Build the top level and the opened packages, in parent-first order."""
    levels = {None: []}
    children = []
    groups = {}
    for index, method in enumerate(circuit.instances):
        ancestor = None if method.parent is None else children[method.parent]
        if ancestor not in levels or (ancestor is None and method.kind not in {"chip", "gate"}):
            children.append(ancestor)
            continue
        name = f"{METHOD_PREFIX}{index}"
        children.append(name)
        groups[name] = (ancestor, method, [])
        if name in expanded and method.model_available:
            levels[name] = []

    # An internal math definition may have no drawable electrical parts.
    # Its visible enclosing package must survive both valid behavior and
    # source-located incomplete behavior, even after lowering omits equations.
    behavior_groups = {children[eq.owner] for eq in circuit.behavior if eq.owner is not None}
    issue_groups = {children[issue.owner] for issue in circuit.model_issues if issue.owner is not None}

    usage = defaultdict(Counter)
    totals = Counter()
    for part in circuit.components:
        owner = circuit.owners.get(part.name)
        level = children[owner] if owner is not None else None
        nets = set(part.terminal_nets)
        totals.update(nets)
        usage[level].update(nets)
        if level not in levels:
            groups[level][2].append(part)
        elif part.kind == "Q":
            pins = tuple(zip(("base", "collector", "emitter"), part.terminal_nets))
            levels[level].append(replace(part, kind="H", package_pins=pins))
        else:
            levels[level].append(part)

    for name, (parent, method, members) in reversed(groups.items()):
        inside = usage[name]
        usage[parent].update(inside)
        contents = levels.get(name, members)
        if (not contents and not method.body and method.model_available and name not in expanded
                and name not in behavior_groups and name not in issue_groups):
            continue
        pins = tuple((pin, circuit.resolve_wire(net)) for pin, net in method.pins)
        exposed = {net for _, net in pins}
        nets = {net for part in contents for net in part.terminal_nets}
        pins += tuple(
            (net, net) for net in sorted(nets - exposed)
            if net in circuit.grounds or inside[net] < totals[net]
        )
        package = ChipPackage if method.kind == "chip" else Component
        physical = {"physical_pin_count": len(method.pins)} if package is ChipPackage else {}
        levels[parent].append(package(
            name, "H", "", "", label=method.name,
            order=min((part.order for part in contents), default=0),
            package_pins=pins, **physical,
        ))

    for name, parts in levels.items():
        if name is None and circuit.perfboard is not None:
            parts = [replace(part, pin_pitch=1) if isinstance(part, ChipPackage) else part for part in parts]
        if name is None and len(parts) == len(circuit.components) and all(
            part is original for part, original in zip(parts, circuit.components)
        ):
            yield name, replace(circuit, behavior=[]) if circuit.behavior else circuit, (), ()
            continue
        parts.sort(key=lambda part: part.order)
        names = {part.name for part in parts if part.kind != "H"}
        method = groups[name][1] if name is not None else None
        boundary = tuple(dict.fromkeys(
            net for part in levels[groups[name][0]] if part.name == name
            for _, net in part.package_pins
        )) if method else ()
        yield name, replace(
            circuit, components=parts, groups=[], behavior=[],
            perfboard=circuit.perfboard if name is None else None,
            wire_taps=[tap for tap in circuit.wire_taps if tap.component in names],
        ), method.body if method else (), boundary
