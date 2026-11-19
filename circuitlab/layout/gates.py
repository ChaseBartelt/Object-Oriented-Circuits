"""Shared schematic layouts for built-in gates."""
# Gate templates supply reusable geometry, while each live instance keeps its own electrical
# parts and nets. Rebinding maps template identities onto those resolved instance names;
# shared defaults remain separate from per-document placement edits.

import json
import os
import tempfile
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.language.gates import gate_body
from circuitlab.layout.hierarchy import METHOD_PREFIX, hood_levels
from circuitlab.layout.geometry import BJT_PIN_OFFSET, PACKAGE_WIDTH, Layout, add_wire, ground_at, place_symbol
from circuitlab.layout.checks import mark_crossings as _mark_crossings, preserves_nets
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.wire_edit import replace_route


# Distinct preview nets expose every port and make template connectivity checks meaningful.
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
    build, minimum = {
        "nand": (_nand_layout, 2), "and": (_and_layout, 3),
        "or": (_or_layout, 2), "nor": (_nor_layout, 1),
    }.get(name, (None, 0))
    if build is None or not (inputs >= minimum):
        return None
    circuit, _ = gate_preview(name, inputs)
    drawing = build(circuit.components)
    for net, edges in list(drawing.wires.items()):
        replace_route(drawing, net, edges)
    _mark_crossings(drawing)
    return drawing if preserves_nets(circuit, drawing) else None


def _place_part(drawing, part, start, end):
    place_symbol(drawing, part, part.p, start, end)


def _ground_lead(drawing, net, *points):
    add_wire(drawing, net, *points)
    ground_at(drawing, points[-1], net)


def _nand_layout(parts):
    pullup, capacitor, *input_rows = parts
    vcc, output, ground = pullup.p, pullup.n, capacitor.n
    drawing = Layout(boundary={vcc: (0, 0), output: (0, -1.8)})

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
    _ground_lead(drawing, ground, cap_right, cap_ground)

    _place_input_stack(drawing, input_rows, output_tap, resistor_x=0.5, row_left=5, row_y=-4.55)
    return drawing


def _and_layout(parts):
    input_pullup, input_cap, *stages = parts
    output_pullup, output_cap, driver_resistor, driver = stages[-4:]
    input_rows = stages[:-4]
    drawing = Layout()

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
    _place_part(drawing, input_pullup, (pullup_x, -0.25), (pullup_x, -4.55))
    _place_part(drawing, output_pullup, (14.4, 0), (18.7, 0))
    _place_part(drawing, driver, (driver_left, driver_y), (driver_right, driver_y))
    add_wire(drawing, vcc, drawing.boundary[vcc], (14.4, 0))
    add_wire(drawing, vcc, (pullup_x, 0), (pullup_x, -0.25))
    add_wire(drawing, output, (18.7, 0), (output_x, 0), collector, (output_x, rail_y), drawing.boundary[output])
    add_wire(drawing, output, (driver_right, collector_y), collector)
    _ground_lead(drawing, ground, (driver_right, emitter_y), (23.5, emitter_y), (23.5, -3.6))

    # Keep both capacitor branches and the driver input above the input rows.
    _place_part(drawing, output_cap, (6.75, -7.5), (2.45, -7.5))
    add_wire(drawing, output, (7.25, rail_y), (7.25, -7.5), (6.75, -7.5))
    _ground_lead(drawing, ground, (2.45, -7.5), (1.25, -7.5))
    drawing.ground_rotations[1.25, -7.5] = 1
    _place_part(drawing, input_cap, (16.75, inverted_y), (21.05, inverted_y))
    add_wire(drawing, inverted, (pullup_x, -4.55), (pullup_x, inverted_y), inverted_junction, (16.75, inverted_y))
    _ground_lead(drawing, ground, (21.05, inverted_y), (22.25, inverted_y), (22.25, -11.25))
    _place_part(drawing, driver_resistor, (driver_input_x, -9.65), (driver_input_x, -5.35))
    drawing.label_offsets["component", driver_resistor.name] = (4, 0)
    add_wire(drawing, inverted, (driver_input_x, -9.65), inverted_junction)
    add_wire(drawing, driver.base, (driver_input_x, -5.35), (driver_input_x, driver_y), (driver_left, driver_y))

    _place_input_stack(drawing, input_rows, inverted_junction, resistor_x=2.5, row_left=7.5, row_y=-12.75)
    return drawing


def _nor_layout(parts):
    pullup, capacitor, *input_rows = parts
    vcc, output, ground = pullup.p, pullup.n, capacitor.n
    drawing = Layout(boundary={vcc: (0, 1.4), output: (0, 0)})
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
    _ground_lead(drawing, ground, cap_top, (cap_ground[0], cap_top[1]), cap_ground)

    rows = list(_place_input_rows(
        drawing, input_rows, resistor_x=4.6, row_left=9, row_y=-3, row_pitch=4.2,
    ))
    _connect_parallel_rows(drawing, rows, collector_bus)
    return drawing


def _or_layout(parts):
    input_pullup, input_cap, *stages = parts
    output_pullup, output_cap, driver_resistor, driver = stages[-4:]
    vcc, inverted, output, ground = input_pullup.p, input_pullup.n, driver.p, driver.n
    drawing = Layout(boundary={vcc: (2.25, 0), output: (2.25, -1)})
    row_left, driver_y = 5.25, -4.9
    row_right = row_left + PACKAGE_WIDTH
    output_x, input_cap_x, collector_x = 15.9, 13.75, 17.25
    supply_branch = (10.75, 0)
    output_tap = (output_x, -1)
    driver_collector_y = driver_y + BJT_PIN_OFFSET

    # Supply and output rails run above the inverter.
    _place_part(drawing, output_pullup, (11.6, 0), (output_x, 0))
    _place_part(drawing, output_cap, (16.2, -1), (20.5, -1))
    _place_part(drawing, driver, (row_left, driver_y), (row_right, driver_y))
    add_wire(drawing, vcc, drawing.boundary[vcc], supply_branch, (11.6, 0))
    add_wire(drawing, output, (output_x, 0), output_tap, (output_x, driver_collector_y),
         (row_right, driver_collector_y))
    add_wire(drawing, output, drawing.boundary[output], output_tap, (16.2, -1))
    _ground_lead(drawing, ground, (20.5, -1), (20.5, -1.85))
    driver_emitter_y = driver_y - BJT_PIN_OFFSET
    _ground_lead(drawing, ground, (row_right, driver_emitter_y), (13, driver_emitter_y), (13, -6.5))

    # The first stage feeds the inverter through the resistor below it.
    bus_y = -9.6
    _place_part(drawing, input_pullup, (10.75, -2.6), (6.45, -2.6))
    add_wire(drawing, vcc, supply_branch, (10.75, -2.6))
    add_wire(drawing, inverted, (6.45, -2.6), (4.25, -2.6), (4.25, bus_y), (collector_x, bus_y))
    _place_part(drawing, driver_resistor, (12.4, -8.1), (8.1, -8.1))
    add_wire(drawing, inverted, (12.4, -8.1), (12.4, bus_y))
    add_wire(drawing, driver.base, (8.1, -8.1), (4.65, -8.1), (4.65, driver_y), (row_left, driver_y))
    _place_part(drawing, input_cap, (input_cap_x, driver_y), (18.05, driver_y))
    drawing.label_offsets["component", input_cap.name] = (0, -1.95)
    add_wire(drawing, inverted, (input_cap_x, driver_y), (input_cap_x, bus_y))
    _ground_lead(drawing, ground, (18.05, driver_y), (18.05, -6.1))

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
        _ground_lead(drawing, transistor.n, emitter, (ground_x, emitter[1]), emitter_ground)
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
    _ground_lead(drawing, ground, incoming, stack_ground)
    drawing.boundary[ground] = stack_ground


def _place_input_rows(drawing, input_rows, *, resistor_x, row_left, row_y, row_pitch=3.5):
    """Place and wire the inputs, then yield the collector and emitter of each row."""
    resistor_end = resistor_x + 4.3
    row_right = row_left + PACKAGE_WIDTH
    for resistor, transistor in zip(input_rows[::2], input_rows[1::2]):
        port, resistor_start = (0, row_y), (resistor_x, row_y)
        base = (row_left, row_y)
        drawing.boundary[resistor.p] = port
        _place_part(drawing, resistor, resistor_start, (resistor_end, row_y))
        _place_part(drawing, transistor, base, (row_right, row_y))
        add_wire(drawing, resistor.p, port, resistor_start)
        add_wire(drawing, transistor.base, (resistor_end, row_y), base)
        yield transistor, (row_right, row_y + BJT_PIN_OFFSET), (row_right, row_y - BJT_PIN_OFFSET)
        row_y -= row_pitch


BUNDLED_LAYOUTS = Path(__file__).resolve().parents[1] / "libraries" / "layouts" / "defaults.json"


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
        return bool(self.raw(key))

    def bundled(self):
        try:
            data = json.loads(BUNDLED_LAYOUTS.read_text(encoding="utf-8"))
            return data["layouts"] if isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("layouts"), dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def raw(self, key):
        path = self.path(key)
        raw = self.settings.value(path) or self.bundled().get(path)
        return json.dumps(raw) if isinstance(raw, dict) else raw

    def publish(self, path=BUNDLED_LAYOUTS):
        """Write active shared defaults atomically for source control/distribution."""
        records = self.bundled()
        records.update({name: self.settings.value(name) for name in self.settings.allKeys()
                        if name.startswith(("gate-layouts/v1/", "device-layouts/v1/"))})
        # Decode before writing, so malformed personal settings cannot replace
        # a valid file. Structured JSON keeps source-control diffs readable.
        records = {name: json.loads(record) if isinstance(record, str) else record
                   for name, record in records.items()
                   if not self.settings.value(name.replace("/v1/", "/automatic/v1/", 1), False, type=bool)}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                tmp = stream.name
                json.dump({"version": 1, "layouts": records}, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(tmp, path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        return len(records)

    def saved(self, key):
        raw = self.raw(key)
        if not raw:
            return None
        try:
            circuit, boundary = gate_preview(*key)
            return restore_layout(json.loads(raw), circuit, boundary)
        except (TypeError, ValueError):
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


# Translate saved template parts and nets to one live instance without reusing another
# instance's scoped names or changing its compiled connections.
def rebind_layout(layout, template, view, pins, boundary, *, template_pins=None):
    """Map a reusable layout onto the resolved parts and nets of one instance."""
    if len(template.components) != len(view.components):
        return None
    ports_by_name = {pin: view.resolve_wire(net) for pin, net in pins}
    nets = {}
    for pin, net in template_pins or ((pin, pin) for pin in ports_by_name):
        original = template.resolve_wire(net)
        actual = ports_by_name[pin]
        if nets.setdefault(original, actual) != actual:
            return None
    parts = {}
    for before, after in zip(template.components, view.components):
        if before.kind != after.kind or len(before.terminal_nets) != len(after.terminal_nets):
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
    ports = merge({net: [pos] for net, pos in layout.boundary.items()})
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
