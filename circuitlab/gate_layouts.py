"""Shared schematic layouts for built-in gates."""

import json
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache

from .compiler import compile_circuit
from .gates import gate_body
from .hood import METHOD_PREFIX, hood_levels
from .layout import BJT_PIN_OFFSET, PACKAGE_WIDTH, Layout, add_wire, ground_at, place_symbol
from .layout_checks import preserves_nets
from .placement import pack_layout, restore_layout
from .routing import _mark_crossings
from .wire_edit import replace_route


@lru_cache(maxsize=64)
def gate_preview(name, inputs):
    """Compile a representative gate with distinct nets at every port."""
    pins, _ = gate_body(name, inputs)
    circuit = compile_circuit(f"{name}({', '.join(pins)});", render_only=True)
    level = METHOD_PREFIX + "0"
    _, detail, _, boundary = next(
        row for row in hood_levels(circuit, {level}) if row[0] == level
    )
    return replace(detail, instances=[], owners={}, grounds={"0", "gnd"}), boundary


@lru_cache(maxsize=64)
def default_gate_layout(name, inputs):
    """Build the default arrangement for gates with dedicated layouts."""
    if name == "nand" and inputs >= 2:
        build = _nand_layout
    elif name == "and" and inputs >= 3:
        build = _and_layout
    elif name == "or" and inputs >= 2:
        build = _or_layout
    elif name == "nor" and inputs >= 1:
        build = _nor_layout
    else:
        return None
    circuit, _ = gate_preview(name, inputs)
    drawing = build(circuit.components)
    for net, edges in list(drawing.wires.items()):
        replace_route(drawing, net, edges)
    _mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


def _nand_layout(parts):
    pullup, capacitor, *input_rows = parts
    vcc, output, ground = pullup.p, pullup.n, capacitor.n
    drawing = Layout()
    drawing.boundary.update({vcc: (0, 0), output: (0, -1.8)})

    pullup_left, pullup_right = (5.75, 0), (10.05, 0)
    cap_left, cap_right = (15.35, 0), (19.65, 0)
    output_rail = (12.65, 0)
    output_tap = (output_rail[0], drawing.boundary[output][1])
    cap_ground = (cap_right[0], -0.8)
    place_symbol(drawing, pullup, vcc, pullup_left, pullup_right)
    place_symbol(drawing, capacitor, output, cap_left, cap_right)
    add_wire(drawing, vcc, drawing.boundary[vcc], pullup_left)
    add_wire(drawing, output, pullup_right, output_rail, cap_left)
    add_wire(drawing, output, drawing.boundary[output], output_tap, output_rail)
    add_wire(drawing, ground, cap_right, cap_ground)
    ground_at(drawing, cap_ground, ground)

    _place_input_stack(drawing, input_rows, output_tap, resistor_x=0.5, row_left=5, row_y=-4.55)
    return drawing


def _and_layout(parts):
    input_pullup, input_cap, *stages = parts
    output_pullup, output_cap, driver_resistor, driver = stages[-4:]
    input_rows = stages[:-4]
    drawing = Layout()

    def place(part, start, end):
        place_symbol(drawing, part, part.p, start, end)

    def wire(net, *points):
        add_wire(drawing, net, *points)

    vcc, inverted, output, ground = input_pullup.p, input_pullup.n, driver.p, driver.n
    rail_y, inverted_y = -5.25, -10
    pullup_x, driver_input_x, output_x = 14, 15.25, 25
    driver_left, driver_y = 16, -2.25
    driver_right = driver_left + PACKAGE_WIDTH
    collector_y, emitter_y = driver_y + BJT_PIN_OFFSET, driver_y - BJT_PIN_OFFSET
    collector = (output_x, collector_y)
    inverted_junction = (driver_input_x, inverted_y)
    drawing.boundary.update({vcc: (0, 0), output: (0, rail_y)})

    # Two pull-ups share the supply rail. The inverter and its output are on the right.
    place(input_pullup, (pullup_x, -0.25), (pullup_x, -4.55))
    place(output_pullup, (14.4, 0), (18.7, 0))
    place(driver, (driver_left, driver_y), (driver_right, driver_y))
    wire(vcc, drawing.boundary[vcc], (14.4, 0))
    wire(vcc, (pullup_x, 0), (pullup_x, -0.25))
    wire(output, (18.7, 0), (output_x, 0), collector, (output_x, rail_y), drawing.boundary[output])
    wire(output, (driver_right, collector_y), collector)
    wire(ground, (driver_right, emitter_y), (23.5, emitter_y), (23.5, -3.6))
    ground_at(drawing, (23.5, -3.6), ground)

    # Keep both capacitor branches and the driver input above the input rows.
    place(output_cap, (6.75, -7.5), (2.45, -7.5))
    wire(output, (7.25, rail_y), (7.25, -7.5), (6.75, -7.5))
    wire(ground, (2.45, -7.5), (1.25, -7.5))
    ground_at(drawing, (1.25, -7.5), ground)
    drawing.ground_rotations[1.25, -7.5] = 1
    place(input_cap, (16.75, inverted_y), (21.05, inverted_y))
    wire(inverted, (pullup_x, -4.55), (pullup_x, inverted_y), inverted_junction, (16.75, inverted_y))
    wire(ground, (21.05, inverted_y), (22.25, inverted_y), (22.25, -11.25))
    ground_at(drawing, (22.25, -11.25), ground)
    place(driver_resistor, (driver_input_x, -9.65), (driver_input_x, -5.35))
    drawing.label_offsets["component", driver_resistor.name] = (4, 0)
    wire(inverted, (driver_input_x, -9.65), inverted_junction)
    wire(driver.base, (driver_input_x, -5.35), (driver_input_x, driver_y), (driver_left, driver_y))

    _place_input_stack(drawing, input_rows, inverted_junction, resistor_x=2.5, row_left=7.5, row_y=-12.75)
    return drawing


def _nor_layout(parts):
    pullup, capacitor, *input_rows = parts
    vcc, output, ground = pullup.p, pullup.n, capacitor.n
    drawing = Layout()
    drawing.boundary.update({vcc: (0, 1.4), output: (0, 0)})
    collector_bus = (17.3, 0)
    pullup_left, pullup_right = (2.8, 1.4), (7.1, 1.4)
    cap_bottom, cap_top = (8.6, 0.1), (8.6, 4.4)
    cap_ground = (10.1, 3.35)

    place_symbol(drawing, pullup, vcc, pullup_left, pullup_right)
    place_symbol(drawing, capacitor, output, cap_bottom, cap_top)
    drawing.label_offsets["component", capacitor.name] = (0, 2.1)
    add_wire(drawing, vcc, drawing.boundary[vcc], pullup_left)
    add_wire(drawing, output, drawing.boundary[output], collector_bus)
    add_wire(drawing, output, pullup_right, (pullup_right[0], 0))
    add_wire(drawing, output, cap_bottom, (cap_bottom[0], 0))
    add_wire(drawing, ground, cap_top, (cap_ground[0], cap_top[1]), cap_ground)
    ground_at(drawing, cap_ground, ground)

    rows = list(_place_input_rows(
        drawing, input_rows, resistor_x=4.6, row_left=9, row_y=-3, row_pitch=4.2,
    ))
    _connect_parallel_rows(drawing, rows, collector_bus)
    return drawing


def _or_layout(parts):
    input_pullup, input_cap, *stages = parts
    output_pullup, output_cap, driver_resistor, driver = stages[-4:]
    drawing = Layout()

    def place(part, start, end):
        place_symbol(drawing, part, part.p, start, end)

    def wire(net, *points):
        add_wire(drawing, net, *points)

    vcc, inverted, output, ground = input_pullup.p, input_pullup.n, driver.p, driver.n
    drawing.boundary.update({vcc: (2.25, 0), output: (2.25, -1)})
    row_left, driver_y = 5.25, -4.9
    row_right = row_left + PACKAGE_WIDTH
    output_x, input_cap_x, collector_x = 15.9, 13.75, 17.25
    supply_branch = (10.75, 0)
    output_tap = (output_x, -1)
    driver_collector_y = driver_y + BJT_PIN_OFFSET

    # Supply and output rails run above the inverter.
    place(output_pullup, (11.6, 0), (output_x, 0))
    place(output_cap, (16.2, -1), (20.5, -1))
    place(driver, (row_left, driver_y), (row_right, driver_y))
    wire(vcc, drawing.boundary[vcc], supply_branch, (11.6, 0))
    wire(output, (output_x, 0), output_tap, (output_x, driver_collector_y),
         (row_right, driver_collector_y))
    wire(output, drawing.boundary[output], output_tap, (16.2, -1))
    wire(ground, (20.5, -1), (20.5, -1.85))
    ground_at(drawing, (20.5, -1.85), ground)
    driver_emitter_y = driver_y - BJT_PIN_OFFSET
    wire(ground, (row_right, driver_emitter_y), (13, driver_emitter_y), (13, -6.5))
    ground_at(drawing, (13, -6.5), ground)

    # The first stage feeds the inverter through the resistor below it.
    bus_y = -9.6
    place(input_pullup, (10.75, -2.6), (6.45, -2.6))
    wire(vcc, supply_branch, (10.75, -2.6))
    wire(inverted, (6.45, -2.6), (4.25, -2.6), (4.25, bus_y), (collector_x, bus_y))
    place(driver_resistor, (12.4, -8.1), (8.1, -8.1))
    wire(inverted, (12.4, -8.1), (12.4, bus_y))
    wire(driver.base, (8.1, -8.1), (4.65, -8.1), (4.65, driver_y), (row_left, driver_y))
    place(input_cap, (input_cap_x, driver_y), (18.05, driver_y))
    drawing.label_offsets["component", input_cap.name] = (0, -1.95)
    wire(inverted, (input_cap_x, driver_y), (input_cap_x, bus_y))
    wire(ground, (18.05, driver_y), (18.05, -6.1))
    ground_at(drawing, (18.05, -6.1), ground)

    rows = list(_place_input_rows(
        drawing, stages[:-4], resistor_x=0.65, row_left=row_left, row_y=-13, row_pitch=4.6,
    ))
    _connect_parallel_rows(drawing, rows, (collector_x, bus_y), first_tap_x=12.4)
    return drawing


def _connect_parallel_rows(drawing, rows, collector_bus, *, first_tap_x=None):
    """Connect the collectors to their shared net and give each emitter a ground."""
    transistor, last_collector, _ = rows[-1]
    net = transistor.p
    bus_x, bus_y = collector_bus
    add_wire(drawing, net, collector_bus, (bus_x, last_collector[1]))
    bus_rows = rows
    if first_tap_x is not None:
        _, first_collector, _ = rows[0]
        add_wire(drawing, net, (first_tap_x, bus_y), (first_tap_x, first_collector[1]), first_collector)
        bus_rows = rows[1:]
    for _, collector, _ in bus_rows:
        add_wire(drawing, net, collector, (bus_x, collector[1]))
    for transistor, _, emitter in rows:
        ground_x = emitter[0] + 0.95
        emitter_ground = (ground_x, emitter[1] - 0.85)
        add_wire(drawing, transistor.n, emitter, (ground_x, emitter[1]), emitter_ground)
        ground_at(drawing, emitter_ground, transistor.n)
    drawing.boundary[transistor.n] = emitter_ground


def _place_input_stack(drawing, input_rows, incoming, *, resistor_x, row_left, row_y):
    """Route the emitter of each transistor to the next transistor's collector."""
    stack_x = row_left + PACKAGE_WIDTH + 0.7
    for transistor, collector, emitter in _place_input_rows(
        drawing, input_rows, resistor_x=resistor_x, row_left=row_left, row_y=row_y,
    ):
        add_wire(drawing, transistor.p, incoming, (incoming[0], collector[1]), collector)
        incoming = (stack_x, emitter[1])
        add_wire(drawing, transistor.n, emitter, incoming)
    ground = input_rows[-1].n
    stack_ground = (stack_x, incoming[1] - 1.5)
    add_wire(drawing, ground, incoming, stack_ground)
    ground_at(drawing, stack_ground, ground)
    drawing.boundary[ground] = stack_ground


def _place_input_rows(drawing, input_rows, *, resistor_x, row_left, row_y, row_pitch=3.5):
    """Place and wire the inputs, then yield the collector and emitter of each row."""
    resistor_end = resistor_x + 4.3
    row_right = row_left + PACKAGE_WIDTH
    for resistor, transistor in zip(input_rows[::2], input_rows[1::2]):
        port, resistor_start = (0, row_y), (resistor_x, row_y)
        base = (row_left, row_y)
        drawing.boundary[resistor.p] = port
        place_symbol(drawing, resistor, resistor.p, resistor_start, (resistor_end, row_y))
        place_symbol(drawing, transistor, transistor.p, base, (row_right, row_y))
        add_wire(drawing, resistor.p, port, resistor_start)
        add_wire(drawing, transistor.base, (resistor_end, row_y), base)
        yield transistor, (row_right, row_y + BJT_PIN_OFFSET), (row_right, row_y - BJT_PIN_OFFSET)
        row_y -= row_pitch


class GateLayouts:
    """Store the user's gate layouts separately from saved circuit layouts."""

    def __init__(self, settings):
        self.settings = settings
        self.cache = {}

    @staticmethod
    def path(key, group="v1"):
        gate, inputs = key
        return f"gate-layouts/{group}/{gate}/{inputs}"

    def has_saved(self, key):
        return self.settings.contains(self.path(key))

    def saved(self, key):
        raw = self.settings.value(self.path(key))
        if raw:
            try:
                circuit, boundary = gate_preview(*key)
                return restore_layout(json.loads(raw), circuit, boundary)
            except (TypeError, ValueError):
                pass
        return None

    def get(self, key):
        if key not in self.cache:
            automatic = self.settings.value(self.path(key, "automatic/v1"), False, type=bool)
            self.cache[key] = None if automatic else self.saved(key)
        return self.cache[key]

    def set(self, key, layout):
        path = self.path(key)
        automatic = self.path(key, "automatic/v1")
        if layout is None:
            self.settings.setValue(automatic, True)
        else:
            self.settings.setValue(path, json.dumps(pack_layout(layout), allow_nan=False))
            self.settings.remove(automatic)
        self.settings.sync()
        if self.settings.status() != self.settings.Status.NoError:
            return False
        self.cache[key] = layout
        return True

    def for_instance(self, instance, view, boundary, *, use_default=True):
        if not instance.builtin_gate:
            return None
        key = instance.name, len(instance.pins) - 3
        layout = self.get(key)
        if layout is None and use_default:
            layout = default_gate_layout(*key)
        if layout is None:
            return None
        template, _ = gate_preview(*key)
        return rebind_layout(layout, template, view, instance.pins, boundary)


def rebind_layout(layout, template, view, pins, boundary):
    """Map a reusable layout onto the resolved parts and nets of one instance."""
    if len(template.components) != len(view.components):
        return None
    ports_by_name = {pin: view.resolve_wire(net) for pin, net in pins}
    nets = dict(ports_by_name)
    parts = {}
    for before, after in zip(template.components, view.components):
        if before.kind != after.kind:
            return None
        parts[before.name] = after
        for original, actual in zip(before.terminal_nets, after.terminal_nets):
            if nets.setdefault(original, actual) != actual:
                return None

    def merge(lists):
        merged = defaultdict(list)
        for net, items in lists.items():
            merged[nets[net]].extend(items)
        return dict(merged)

    # Tied pins can share a net while their original leads remain in place.
    # Matching connectors or ground symbols join those leads.
    ports = defaultdict(list)
    for net, pos in layout.boundary.items():
        ports[nets[net]].append(pos)
    labels = {}
    for pin, actual in ports_by_name.items():
        labels.setdefault(actual, pin)
    symbols = [(parts[part.name], a, b, top) for part, a, b, top in layout.symbols]
    connectors = {}
    for net, pos, label, left, below in layout.connectors:
        actual = nets[net]
        connectors[actual, pos] = (labels.setdefault(actual, label), left, below)
    grounds = {}
    for pos in layout.grounds:
        actual = nets[layout.ground_names.get(pos, "0")]
        if actual in view.grounds:
            grounds[pos] = actual
        else:
            connectors.setdefault((actual, pos), (labels[actual], True, False))
    for net, positions in ports.items():
        if net in view.grounds:
            grounds.update(dict.fromkeys(positions, net))
        elif len(positions) > 1:
            for pos in positions:
                connectors.setdefault((net, pos), (labels[net], True, False))
    for pos, net in grounds.items():
        connectors.pop((net, pos), None)

    boundary_positions = {net: positions[0] for net, positions in ports.items() if net in boundary}
    endpoint_kinds = {pos: "connector" for _, pos in connectors} | dict.fromkeys(grounds, "ground")
    label_offsets = {}
    for (kind, *identity), offset in layout.label_offsets.items():
        if kind in {"component", "terminal"} and identity[0] in parts:
            name, *pin = identity
            label_offsets[(kind, parts[name].name, *pin)] = offset
        elif kind in {"net", "probe"} and identity[0] in nets:
            label_offsets[kind, nets[identity[0]]] = offset
        elif kind == "boundary" and identity[0] in layout.boundary:
            pos = layout.boundary[identity[0]]
            net = nets[identity[0]]
            if net not in view.grounds and pos == boundary_positions.get(net):
                label_offsets["boundary", net] = offset
            elif pos in endpoint_kinds:
                label_offsets[(endpoint_kinds[pos], *pos)] = offset
        elif kind in {"ground", "connector"} and tuple(identity) in endpoint_kinds:
            label_offsets[(endpoint_kinds[tuple(identity)], *identity)] = offset

    drawing = replace(
        layout,
        symbols=symbols,
        positions={part.name: a for part, a, _, _ in symbols},
        rotations={parts[name].name: turns for name, turns in layout.rotations.items()},
        wires=merge(layout.wires),
        junctions=merge(layout.junctions),
        labels={nets[net]: pos for net, pos in layout.labels.items()},
        boundary=boundary_positions,
        grounds=list(grounds),
        ground_names=grounds,
        ground_rotations={pos: turns for pos, turns in layout.ground_rotations.items() if pos in grounds},
        connectors=[(net, pos, *caption) for (net, pos), caption in connectors.items()],
        crossings={(nets[net], a, b): cuts for (net, a, b), cuts in layout.crossings.items()},
        label_offsets=label_offsets,
    )
    return drawing if preserves_nets(view, drawing) else None
