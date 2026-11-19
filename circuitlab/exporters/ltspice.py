"""Write native LTspice symbols, wires, and flags from a schematic layout."""

from collections import defaultdict
from dataclasses import dataclass, replace
from hashlib import sha256
from itertools import combinations

from circuitlab.model.graph import CircuitError
from circuitlab.layout.hierarchy import METHOD_PREFIX, hood_levels
from circuitlab.layout.geometry import BJT_PIN_OFFSET, package_pins, pin_offset, rotate_point, symbol_pins, wire_axis
from circuitlab.layout.checks import segments_intersect
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import fast_layout
from circuitlab.exporters.spice import (
    OPAMP_MODEL, POWERED_OPAMP_MODEL, SpiceNames, opamp_subcircuit, spice_circuit,
)
from circuitlab.layout.wire_edit import route_anchors, wire_graph

# Pin locations relative to the anchors in LTspice's built-in symbol library.
SYMBOLS = {
    "R": ("res", 16, 16, 96),
    "C": ("cap", 16, 0, 64),
    "L": ("ind", 16, 16, 96),
    "V": ("voltage", 0, 16, 96),
    "I": ("current", 0, 0, 80),
}


@dataclass(frozen=True)
class Block:
    name: str
    instance: str
    pins: tuple[tuple[str, str, int, int], ...]


def export_hierarchy(circuit, layout, details=None):
    """Export the top sheet and native LTspice block files for the packages it uses."""
    spice = spice_circuit(circuit)
    parts = {part.name: part for part in circuit.components}
    details = details or {}
    expanded = {f"{METHOD_PREFIX}{index}" for index in range(len(circuit.instances))}
    levels = {name: (view, boundary) for name, view, _, boundary in hood_levels(circuit, expanded)}
    packages = {
        part.name: part for view, _ in levels.values() for part in view.components
        if part.name.startswith(METHOD_PREFIX)
    }
    blocks, files = {}, {}
    instances = SpiceNames(*(part.name for part in spice.parts.values()))

    def native_symbols(drawing):
        # Transistors use native LTspice symbols even when shown as packages in the editor.
        return replace(drawing, symbols=[
            (parts.get(part.name, part), a, b, top) for part, a, b, top in drawing.symbols
        ])

    for name in reversed(levels):
        if name is None:
            continue
        view, boundary = levels[name]
        if not view.components:
            continue
        package = packages[name]
        drawing = details.get(name)
        if drawing is None or {part.name for part, *_ in drawing.symbols} != {
            part.name for part in view.components
        }:
            drawing = fast_layout(view, component_labels(view), boundary=boundary)
        drawing = native_symbols(drawing)
        names = SpiceNames(*spice.nodes.values())
        # Port names belong to this sheet. Reuse a readable pin name when available.
        used_ports = SpiceNames("0", "gnd")
        ports = {}
        aliases = []
        pins = []
        for label, net, left, offset in package_pins(package):
            port = used_ports.claim("ground" if label.casefold() in ("gnd", "0") else label)
            if net in ports:
                aliases.append((ports[net], port))
            else:
                ports[net] = port
            pins.append((net, port, 0 if left else 320, round(-4 * offset) * 16))
        nodes = {
            net: (names.claim(mapped) if mapped.casefold() in used_ports.used else mapped)
            for net, mapped in spice.nodes.items()
        }
        nodes.update(ports)
        local = replace(spice, nodes=nodes)
        sheet, _ = _export_drawing(
            drawing, local, blocks=blocks, ports=ports, aliases=aliases, commands=False
        )
        symbol = _block_symbol(package.label, pins)
        digest = sha256((sheet + symbol).encode("ascii")).hexdigest()[:12]
        label = SpiceNames().claim(package.label)[:32]
        # Keep file references lowercase for cross-platform LTspice projects.
        stem = f"ec_{label.lower()}_{digest}"
        blocks[name] = Block(stem, instances.claim(package.label, "X"), tuple(pins))
        files[f"{stem}.asc"] = sheet
        files[f"{stem}.asy"] = symbol

    sheet, used_labels = _export_drawing(native_symbols(layout), spice, blocks=blocks)
    return sheet, files, used_labels


def _block_symbol(label, pins):
    top = min(y for _, _, _, y in pins) - 40
    bottom = max(y for _, _, _, y in pins) + 40
    lines = ["Version 4", "SymbolType BLOCK", f"RECTANGLE Normal 16 {top} 304 {bottom}",
             f"TEXT 160 {top - 16} Center 2 {label.encode('ascii', 'replace').decode('ascii')}"]
    for index, (_, port, x, y) in enumerate(pins, 1):
        side = "LEFT" if x == 0 else "RIGHT"
        inside = 16 if x == 0 else 304
        lines.extend((f"LINE Normal {x} {y} {inside} {y}", f"PIN {x} {y} {side} 24",
                      f"PINATTR PinName {port}", f"PINATTR SpiceOrder {index}"))
    return "\n".join([*lines, ""])


def export_schematic(circuit, layout):
    """Serialize the rendered circuit with the same electrical nets as SPICE."""
    return _export_drawing(layout, spice_circuit(circuit))


def _export_drawing(layout, spice, *, blocks=None, ports=None, aliases=(), commands=True):
    layout = _potentiometer_layout(layout, spice)
    options = dict(blocks=blocks or {}, ports=ports or {}, aliases=aliases, commands=commands)
    for scale in ((64, 96, 128) if blocks else (48, 64, 96, 128)):
        try:
            return _schematic(layout, spice, scale, **options), False
        except GridConflict:
            continue
    # Preserve symbol placement if the native pins and the existing tracks
    # cannot coexist on the grid. Flags on the actual pins carry every net.
    return _export_with_pin_labels(layout, spice, options)


def _potentiometer_layout(layout, spice):
    """Use two native resistors and a wiper junction without external symbols."""
    if not any(part.kind == "P" for part, *_ in layout.symbols):
        return layout
    symbols, wires = [], {net: list(edges) for net, edges in layout.wires.items()}
    rotations = dict(layout.rotations)
    for part, a, b, top in layout.symbols:
        if part.kind != "P":
            symbols.append((part, a, b, top))
            continue
        pins = list(symbol_pins(part, a, b, top, grid=layout.pin_grid))
        # Native pot geometry lists high, wiper, low in electrical pin order.
        high, wiper, low = (point for _, point in pins)
        middle = (high[0], (high[1] + low[1]) / 2)
        turns = rotations.pop(part.name, 0)
        high, middle, low, wiper = (
            rotate_point(point, a, b, turns) for point in (high, middle, low, wiper)
        )
        wires.setdefault(part.wiper, []).append((middle, wiper))
        for leg, start, end in zip(spice.potentiometers[part.name], (high, middle), (middle, low)):
            if leg is None:
                wires.setdefault(part.wiper, []).append((start, end))
            else:
                symbols.append((leg, start, end, top))
    return replace(layout, symbols=symbols, wires=wires, rotations=rotations)


def _export_with_pin_labels(layout, spice, options):
    try:
        return _schematic(layout, spice, 96 if options["blocks"] else 64, pin_flags=True, **options), True
    except GridConflict:
        raise CircuitError(
            "The rendered symbols are too close for the LTspice grid. "
            "Select Labeled connectors and export again."
        ) from None


def _display_value(symbol):
    if symbol.kind in {"R", "L", "C"}:
        value = float(symbol.value)
        for multiplier, suffix in ((1e6, "Meg"), (1e3, "k"), (1e-3, "m"),
                                   (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
            scaled = value / multiplier
            if 1 <= scaled < 1000:
                # Use a compact spelling only if it recreates the same value.
                text = repr(scaled).removesuffix(".0")
                if float(text) * multiplier == value:
                    return text + suffix
                break
    return symbol.value


class GridConflict(Exception):
    """The drawing needs more grid space to keep connections distinct."""


def _schematic(layout, spice, scale, *, pin_flags=False, blocks, ports, aliases, commands):
    """Snap the drawing, attach native symbol pins, and serialize the sheet."""

    def snap(value):
        return 16 * round(value * scale / 16)

    def point(position):
        return snap(position[0]), snap(-position[1])

    wires, points = defaultdict(set), defaultdict(set)
    flags = defaultdict(set)
    symbols = []

    def wire(net, a, b):
        if pin_flags:
            return
        points[net].update((a, b))
        if a != b:
            wires[net].add(tuple(sorted((a, b))))

    if not pin_flags:
        for net in layout.wires:
            segments = list(layout.visible_segments(net))
            if any(wire_axis(edge) is None for edge in segments):
                # Insert tees before snapping so both wires round to the same junction.
                graph = wire_graph(segments, route_anchors(layout, net))
                segments = sorted((a, b) for a, neighbors in graph.items() for b in neighbors if a < b)
            for a, b in segments:
                wire(net, point(a), point(b))
        for net, p, _, _, _ in layout.connectors:
            flags[net].add(point(p))
        for p in layout.grounds:
            flags[layout.ground_names.get(p, "0")].add(point(p))

    for part, start, end, top in layout.symbols:
        terminals = []
        turns = layout.rotations.get(part.name, 0)
        x_center, y_center = point(tuple((a + b) / 2 for a, b in zip(start, end)))

        def turn(at):
            if not turns:
                return at
            dx, dy = at[0] - x_center, at[1] - y_center
            dx, dy = ((dx, dy), (-dy, dx), (-dx, -dy), (dy, -dx))[turns]
            return x_center + dx, y_center + dy

        def pin(net, at, *, anchor=None, bends=()):
            native = turn(at)
            terminals.append((net, native))
            if anchor is None:
                flags[net].add(native)
            else:
                route = (point(rotate_point(anchor, start, end, turns)), *map(turn, bends), native)
                for a, b in zip(route, route[1:]):
                    wire(net, a, b)

        block = blocks.get(part.name)
        model = spice.parts.get(part.name)
        a, b = point(start), point(end)
        if block:
            x = snap((start[0] + end[0]) / 2) - 160
            if x < a[0] or x + 320 > b[0]:
                raise GridConflict
            origin = x, a[1]
            symbol, rotation = block.name, "R0"
            for (_, net, left, offset), (_, _, pin_x, pin_y) in zip(package_pins(part, layout.pin_grid), block.pins):
                anchor = ((start if left else end)[0], start[1] + offset)
                terminal = x + pin_x, a[1] + pin_y
                pin(net, terminal, anchor=anchor, bends=((terminal[0], point(anchor)[1]),))
        elif model is None:
            raise CircuitError(f"Cannot export the empty block {part.label}.")
        elif model.kind == "X":
            x, y = a
            if b[0] < x + 112:
                raise GridConflict
            origin = (x + 64, y - 64 if top == "-" else y + 64)
            rotation = "R0" if top == "-" else "M180"
            symbol = r"OpAmps\opamp2"
            for net, sign in ((part.in_plus, "+"), (part.in_minus, "-")):
                upper = sign == top
                offset = pin_offset(0.35 if upper else -0.35, layout.pin_grid)
                anchor = start[0], start[1] + offset
                lane = y - 16 if upper else y + 16
                pin(net, (x + 32, lane), anchor=anchor, bends=((x + 16, point(anchor)[1]), (x + 16, lane)))
            pin(part.p, (x + 96, y), anchor=end)
            for net, direction in ((part.vplus, 1), (part.vminus, -1)):
                supply = (x + 64, y - direction * 32)
                if part.vplus:
                    anchor = ((start[0] + end[0]) / 2,
                              start[1] + direction * pin_offset(1.25, layout.pin_grid))
                    pin(net, supply, anchor=anchor, bends=((supply[0], point(anchor)[1]),))
                else:
                    pin(part.n, supply)
        elif model.kind == "Q":
            x, y = a
            if b[0] < x + 64:
                raise GridConflict
            symbol, rotation = part.transistor_type, "R0"
            origin = (x, y - 48)
            pin(part.base, a, anchor=start)
            # Both native BJT symbols place C and E 64 units right of B.
            for net, direction in ((part.p, 1), (part.n, -1)):
                anchor = end[0], end[1] + direction * pin_offset(BJT_PIN_OFFSET, layout.pin_grid)
                terminal = (x + 64, y - direction * 48)
                pin(net, terminal, anchor=anchor, bends=((terminal[0], point(anchor)[1]),))
        elif model.kind == "S":
            x, y = a[0] + 64, a[1]
            if b[0] < x + 16:
                raise GridConflict
            symbol, rotation = "sw", "R0"
            origin = x, y - 56
            offset = pin_offset(0.6, layout.pin_grid)
            for net, anchor, terminal in (
                (part.in_plus, (start[0], start[1] + offset), (x - 48, y + 24)),
                (part.in_minus, (start[0], start[1] - offset), (x - 48, y - 24)),
                (part.p, (end[0], end[1] + offset), (x, y - 40)),
                (part.n, (end[0], end[1] - offset), (x, y + 40)),
            ):
                pin(net, terminal, anchor=anchor, bends=((terminal[0], point(anchor)[1]),))
        else:
            symbol, pin_x, p_y, n_y = SYMBOLS[model.kind]
            dx, dy = b[0] - a[0], b[1] - a[1]
            if (dx and dy) or abs(dx or dy) < n_y - p_y:
                raise GridConflict
            rotation, transform = _orientation(dx, dy)
            first = transform(pin_x, p_y)
            last = transform(pin_x, n_y)
            length = n_y - p_y
            unit = ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
            along = (x_center - a[0]) * unit[0] + (y_center - a[1]) * unit[1]
            lead = min(max(0, along - 16 * round(length / 32)), abs(dx or dy) - length)
            p = (a[0] + lead * unit[0], a[1] + lead * unit[1])
            origin = p[0] - first[0], p[1] - first[1]
            n = origin[0] + last[0], origin[1] + last[1]
            pin(part.p, p, anchor=start)
            pin(part.n, n, anchor=end)
        if turns:
            origin = turn(origin)
            angle = int(rotation[1:])
            angle = angle + (90 if rotation[0] == "R" else -90) * turns
            rotation = f"{rotation[0]}{angle % 360}"
        for net, at in terminals:
            points[net].add(at)
            if pin_flags:
                flags[net].add(at)
        symbols.extend((
            f"SYMBOL {symbol} {origin[0]} {origin[1]} {rotation}",
            f"SYMATTR InstName {block.instance if block else model.name}",
        ))
        if not block:
            symbols.append(f"SYMATTR Value {_display_value(model)}")
            if model.kind == "L":
                symbols.append(f"SYMATTR SpiceLine {model.extra.strip()}")
            elif model.kind in "VI" and model.extra:
                symbols.append(f"SYMATTR Value2 {model.extra.strip()}")

    io = {}
    for net, port in ports.items():
        if net not in points:
            continue
        locations = points[net] | flags[net]
        boundary = point(layout.boundary[net]) if net in layout.boundary and not pin_flags else None
        at = boundary if boundary is not None else min(locations)
        points[net].add(at)
        flags[net].add(at)
        io[net] = at

    # LTspice wires carry no net ID. Reject any contact between distinct
    # nets after snapping, including pins and flags that sit on a wire.
    occupied = [
        (*wires[net], *((p, p) for p in points[net] | flags[net]))
        for net in points
    ]
    if any(
        segments_intersect(segment, other)
        for first, second in combinations(occupied, 2)
        for segment in first for other in second
    ):
        raise GridConflict

    lines = []
    for net, vertices in points.items():
        edges = set()
        vertices.update(flags[net])
        preferred = point(layout.labels[net]) if net in layout.labels else None
        if preferred is not None and any(
            segments_intersect((preferred, preferred), segment)
            for segment in wires[net]
        ):
            vertices.add(preferred)
        for segment in wires[net]:
            split = sorted(p for p in vertices if segments_intersect((p, p), segment))
            edges.update(zip(split, split[1:]))
        lines.extend(f"WIRE {b[0]} {b[1]} {a[0]} {a[1]}" for a, b in sorted(edges))
        # A drawn crossing gap or labeled connector can split a net into
        # islands. Give each island the same LTspice flag to reconnect it.
        for island in _islands(vertices, edges):
            if not island & flags[net]:
                flags[net].add(preferred if preferred in island else min(island))
        for x, y in sorted(flags[net]):
            lines.append(f"FLAG {x} {y} {spice.nodes[net]}")
            if io.get(net) == (x, y):
                lines.append(f"IOPIN {x} {y} BiDir")

    all_points = [p for vertices in points.values() for p in vertices]
    left = min(x for x, _ in all_points)
    right = max(x for x, _ in all_points)
    bottom = max(y for _, y in all_points)
    # Separate pins can share a net (such as 555 TRIG and THRES). A native
    # jumper makes the two port names aliases without adding a SPICE element.
    port_links = [*aliases, *((port, None) for net, port in ports.items() if net not in points)]
    placed_ports = {ports[net] for net in ports if net in points}
    for i, (port, alias) in enumerate(port_links, 1):
        y = bottom + 96 * i
        if alias is None and port in placed_ports:
            continue
        lines.append(f"FLAG {left} {y} {port}")
        if port not in placed_ports:
            lines.append(f"IOPIN {left} {y} BiDir")
            placed_ports.add(port)
        if alias is not None:
            lines.extend((f"FLAG {left + 64} {y} {alias}", f"IOPIN {left + 64} {y} BiDir"))
            symbols.extend((f"SYMBOL Misc\\jumper {left + 32} {y - 64} R0",
                            f"SYMATTR InstName J_EC_port{i}"))
    bottom += 96 * len(port_links)
    lines.extend(symbols)
    for comment in spice.comments:
        if comment.startswith("* Mechanical switch "):
            bottom += 32
            lines.append(f"TEXT {left} {bottom} Left 2 ;{comment[2:]}")
    if pin_flags:
        lines.append(f"TEXT {left} {bottom + 64} Left 2 ;Matching net labels connect.")
    directives = [spice.directive] if commands else []
    if commands:
        directives.extend(f"{name} {node} 0 DC 0" for name, node in spice.references)
        if any(part.kind == "X" and part.value == OPAMP_MODEL for part in spice.parts.values()):
            # opamp2's pin order is plus, minus, V+, V-, out. The unused V+
            # pin and the return pin both connect to the circuit's return net.
            directives.extend(opamp_subcircuit("plus minus unused return out"))
        if any(part.kind == "X" and part.value == POWERED_OPAMP_MODEL for part in spice.parts.values()):
            # Power pins are retained for wiring while the ideal model remains
            # independent of supplies and returns output current to ground.
            directives.extend(opamp_subcircuit(
                "plus minus vplus vminus out", model=POWERED_OPAMP_MODEL, reference="0",
            ))
        directives.extend(spice.models)
    if directives:
        lines.append(f"TEXT {left} {bottom + 96} Left 2 !" + r"\n".join(directives))
    return "\n".join(
        [
            "Version 4",
            f"SHEET 1 {max(880, right + 160)} {max(680, bottom + 240)}",
            *lines,
            "",
        ]
    )


def _orientation(dx, dy):
    if dx > 0:
        return "R270", lambda x, y: (y, -x)
    if dx < 0:
        return "R90", lambda x, y: (-y, x)
    if dy < 0:
        return "R180", lambda x, y: (-x, -y)
    return "R0", lambda x, y: (x, y)


def _islands(vertices, edges):
    """Find the groups of points connected by physical wire segments."""
    neighbors = defaultdict(set)
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    pending = set(vertices)
    for root in sorted(vertices):
        if root not in pending:
            continue
        pending.remove(root)
        stack = [root]
        for point in stack:
            next_points = neighbors[point] & pending
            pending.difference_update(next_points)
            stack.extend(next_points)
        yield set(stack)
