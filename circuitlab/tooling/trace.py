"""Trace a representative electrical path through a hovered component pin."""
# A hover trace is a representative path through existing net identities, not a new circuit.
# Preserve component terminal order when choosing a branch so the highlighted path starts at
# the pin actually selected in the editor.

import re
from collections import defaultdict, deque


# Walk the selected electrical net using component identities; the preview can highlight
# its existing geometry without rewriting source or constructing a replacement circuit.
def wire_trace(circuit, location, path=None):
    line, name, terminal, argument = location
    if re.fullmatch(r"trans[\w]+", name, re.I):
        name = name[5:]
    key = name.casefold()
    matches = []
    for part in circuit.components:
        local = part.name.split(".", 1)[-1] if part.name.startswith("@file") else part.name
        if (
            circuit.component_lines.get(part.name) == line
            and circuit.component_files.get(part.name, path) == path
            and (local.casefold() == key or local.casefold().startswith(key + "_"))
        ):
            matches.append(part)
    traced = set()
    highlighted = set()
    for part in matches:
        nets = part.terminal_nets
        if terminal < len(nets):
            highlighted.add(nets[terminal])
            others = tuple(net for i, net in enumerate(nets) if i != terminal)
            traced.update(trace_component(circuit, part, nets[terminal], others))
    if not matches:
        call_nets = {
            circuit.resolve_wire(instance.arguments[terminal][1])
            for instance in circuit.instances
            if instance.line == line and instance.name.casefold() == key
            and instance.path in (None, path) and terminal < len(instance.arguments)
            and instance.arguments[terminal][0] == argument
        }
        # A wire on a method call can map to several parts declared in its body.
        for net in circuit.nodes:
            if net in call_nets or (not call_nets and net == argument and net not in circuit.grounds):
                highlighted.add(net)
                traced.update(part.name for part in circuit.components if net in part.terminal_nets)
    return traced, highlighted


def trace_component(circuit, selected, incoming, outgoing):
    links = defaultdict(list)
    for part in circuit.components:
        if part is selected:
            continue
        for net in dict.fromkeys(part.terminal_nets):
            links[net].append(part)

    grounds = circuit.grounds
    sources = {part.p for part in circuit.components if part.is_source} - grounds
    endpoints = {net for net, parts in links.items() if len(parts) == 1} - {
        incoming, *outgoing, *grounds,
    }

    def path(starts, targets, blocked):
        pending = deque((net, ()) for net in starts if net not in blocked)
        visited = set(starts) | set(blocked)
        while pending:
            net, branch = pending.popleft()
            if net in targets:
                return branch
            if net in grounds:
                continue
            for part in links[net]:
                for other in part.terminal_nets:
                    if other not in visited:
                        visited.add(other)
                        pending.append((other, (*branch, part.name)))
        return ()

    before = path((incoming,), sources, outgoing)
    if not before and incoming not in sources:
        before = path((incoming,), endpoints, outgoing)
    after = path(outgoing, grounds or endpoints, (incoming,))
    if not after and not any(net in grounds for net in outgoing):
        after = path(outgoing, endpoints, (incoming,))
    return {selected.name, *before, *after}
