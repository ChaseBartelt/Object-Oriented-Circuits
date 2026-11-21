"""Conventional layouts for packaged amplifiers and their passive networks."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import product
from math import ceil

from circuitlab.layout.geometry import (
    ChipPackage, Layout, add_wire, ground_at, ground_lead, inline_grounded_voltage,
    package_half_height, package_pins, place_symbol,
)
from circuitlab.layout.checks import mark_crossings
from circuitlab.layout.lead_spacing import TERMINAL_SPANS
from circuitlab.model.graph import Component


PASSIVE = {"R", "C", "L", "X", "Z"}
GRID = 0.25
FEEDBACK_CLEARANCE = 1.25
FEEDBACK_PITCH = 1.75


def _span(part):
    # Use grid-aligned centers and the shortest complete device leads.
    return ceil(TERMINAL_SPANS[part.kind] / (2 * GRID)) * 2 * GRID


@dataclass(frozen=True)
class Stage:
    package: ChipPackage
    source: Component
    input_net: str
    sense: str
    output: str
    path: tuple[tuple[Component, str], ...]
    feedback: tuple[Component, ...]
    loads: tuple[Component, ...]

    def members(self):
        return {self.package, self.source, *self.feedback, *self.loads,
                *(part for part, _ in self.path)}


def _grounded_return(part, net, grounds):
    if part.p == net:
        return part.n in grounds
    return part.n == net and part.p in grounds


def _input_chain(sense, output, package, feedback, owners, grounds):
    """Walk from the sensing pin to a grounded source without branching."""
    net = sense
    path = []
    seen = {package, *feedback}
    visited = {output, *grounds}
    while net not in visited:
        visited.add(net)
        remaining = [part for part in owners[net] if part not in seen]
        if len(remaining) != 1:
            break
        part = remaining[0]
        if part.is_source and part.kind in {"V", "I"} and _grounded_return(part, net, grounds):
            return part, net, tuple(reversed(path))
        if part.kind not in PASSIVE:
            break
        incoming = part.n if part.p == net else part.p
        path.append((part, incoming))
        seen.add(part)
        net = incoming
    return None


def _stage(package, owners, grounds):
    """Find a unique feedback loop around opposing physical package pins."""
    candidates = []
    left, right = [], []
    for _, net, on_left, _ in package_pins(package):
        if net not in grounds and len(owners[net]) > 1:
            (left if on_left else right).append(net)
    if len(set(left + right)) != len(left) + len(right):
        return None
    for sense, output in product(left, right):
        feedback = tuple(
            part for part in owners[sense]
            if part.kind in PASSIVE and {part.p, part.n} == {sense, output}
        )
        if not feedback:
            continue
        inputs = _input_chain(sense, output, package, feedback, owners, grounds)
        if inputs is None:
            continue
        source, input_net, path = inputs
        loads = tuple(part for part in owners[output] if part not in (package, *feedback))
        if all(part.kind in PASSIVE and _grounded_return(part, output, grounds) for part in loads):
            candidates.append(Stage(package, source, input_net, sense, output, path, feedback, loads))
    return candidates[0] if len(candidates) == 1 else None


def _power_rails(parts, grounds):
    """Partition the remaining grounded sources and shunts by supply net."""
    rails = {}
    for part in parts:
        if not part.is_source:
            continue
        if part.kind not in {"V", "I"} or (part.p in grounds) == (part.n in grounds):
            return None
        net = part.n if part.p in grounds else part.p
        if net in rails:
            return None
        rails[net] = part, []
    for part in parts:
        if part.is_source:
            continue
        net = part.n if part.p in grounds else part.p
        if part.kind not in PASSIVE or net not in rails or not _grounded_return(part, net, grounds):
            return None
        rails[net][1].append(part)
    return rails


def _grounded_branch(drawing, part, net, x, y):
    end = x, y - _span(part)
    place_symbol(drawing, part, net, (x, y), end)
    ground_at(drawing, end, part.n if part.p == net else part.p)
    return end[1]


def _draw_input_path(drawing, stage, sense_pin, sense_x, pitch):
    """Run the source and its series input parts along the sensing pin row."""
    _, sense_y = sense_pin
    source_x = sense_x - pitch * len(stage.path) - 1.75
    return_y = _grounded_branch(drawing, stage.source, stage.input_net, source_x, sense_y)
    previous_x = source_x
    for index, (part, incoming) in enumerate(stage.path):
        span = _span(part)
        start = source_x + (pitch - span) / 2 + pitch * index
        add_wire(drawing, incoming, (previous_x, sense_y), (start, sense_y))
        previous_x = start + span
        place_symbol(drawing, part, incoming, (start, sense_y), (previous_x, sense_y))
    add_wire(drawing, stage.sense, (previous_x, sense_y), sense_pin)
    drawing.junctions.setdefault(stage.sense, []).append((sense_x, sense_y))
    return return_y


def _draw_stage(drawing, stage, y, height, grounds, supply_pins, local_sources, pitch):
    left, right = 0, 7
    place_symbol(drawing, stage.package, stage.package.p, (left, y), (right, y))
    package_contacts = list(package_pins(stage.package, GRID))
    pins = {
        net: (left if on_left else right, y + offset)
        for _, net, on_left, offset in package_contacts
    }
    sense_y, output_y = pins[stage.sense][1], pins[stage.output][1]
    sense_x, output_x = left - 1.5, right + 1.5
    load_y = output_y - .75 if stage.loads else output_y
    local_left = local_right = False
    for _, net, on_left, _ in package_contacts:
        if net not in local_sources:
            continue
        pin_y = pins[net][1]
        if on_left:
            local_left = True
            if pin_y > sense_y:
                sense_x = left - 4.5
        else:
            local_right = True
            if pin_y > load_y:
                output_x = right + 4.5

    input_return_y = _draw_input_path(drawing, stage, pins[stage.sense], sense_x, pitch)
    bottom_y = min(y - height, input_return_y)

    feedback_start = y + height + FEEDBACK_CLEARANCE
    feedback_end = feedback_start + FEEDBACK_PITCH * (len(stage.feedback) - 1)
    add_wire(drawing, stage.sense, (sense_x, sense_y), (sense_x, feedback_end))
    add_wire(drawing, stage.output, pins[stage.output], (output_x, output_y), (output_x, feedback_end))
    center = round((sense_x + output_x) / (2 * GRID)) * GRID
    for index, part in enumerate(stage.feedback):
        row = feedback_start + FEEDBACK_PITCH * index
        half = _span(part) / 2
        a, b = (center - half, row), (center + half, row)
        place_symbol(drawing, part, stage.sense, a, b)
        add_wire(drawing, stage.sense, (sense_x, row), a)
        add_wire(drawing, stage.output, b, (output_x, row))

    if stage.loads:
        add_wire(drawing, stage.output, (output_x, output_y), (output_x, load_y))
        drawing.junctions.setdefault(stage.output, []).append((output_x, output_y))
        for index, part in enumerate(stage.loads):
            load_x = output_x + index * pitch
            if index:
                add_wire(drawing, stage.output, (load_x - pitch, load_y), (load_x, load_y))
            bottom_y = min(bottom_y, _grounded_branch(drawing, part, stage.output, load_x, load_y))
    # Multiple physical contacts may intentionally share ground. Draw each contact.
    for _, net, on_left, offset in package_contacts:
        direction = -1 if on_left else 1
        start = left if on_left else right, y + offset
        if net in grounds:
            ground_lead(drawing, start, net, turns=1 if on_left else 3, length=.75)
        elif net in local_sources:
            source = local_sources[net]
            ground = source.n if source.p == net else source.p
            inline_grounded_voltage(drawing, source, net, ground, start, left=on_left)
        elif net in supply_pins:
            supply_pins[net].append((start, direction))
    return (min(sense_x, left - 4 if local_left else left),
            max(output_x + max(0, len(stage.loads) - 1) * pitch, right + 4 if local_right else right),
            bottom_y)


def _draw_supply_rails(drawing, rails, pins, left, right, pitch, clearance):
    """Keep each supply on one physical trunk beside the stage stack."""
    lanes = defaultdict(int)
    for net, (source, shunts) in rails.items():
        contacts = pins[net]
        directions = {direction for _, direction in contacts}
        if len(directions) != 1:
            return False
        direction = directions.pop()
        lane = lanes[direction]
        lanes[direction] += 1
        spine = (left if direction < 0 else right) + direction * (1.25 + lane)
        rail_y = 4.75 + 5.5 * lane
        bottom = min(at[1] for at, _ in contacts)
        add_wire(drawing, net, (spine, bottom), (spine, rail_y))
        for (x, y), _ in contacts:
            add_wire(drawing, net, (x, y), (spine, y))
            if y > bottom:
                drawing.junctions.setdefault(net, []).append((spine, y))
        source_x = spine + direction * clearance
        add_wire(drawing, net, (spine, rail_y), (source_x, rail_y))
        _grounded_branch(drawing, source, net, source_x, rail_y)
        for index, part in enumerate(shunts, 1):
            end = source_x + direction * index * pitch
            start = end - direction * pitch
            add_wire(drawing, net, (start, rail_y), (end, rail_y))
            drawing.junctions.setdefault(net, []).append((start, rail_y))
            _grounded_branch(drawing, part, net, end, rail_y)
        drawing.labels[net] = ((spine + source_x) / 2, rail_y)
    return True


def package_stage_layout(circuit, labels):
    """Lay out independent package feedback stages with continuous supply rails."""
    parts, grounds = circuit.components, circuit.grounds
    packages = [part for part in parts if isinstance(part, ChipPackage)]
    if not packages or len(parts) > 64:
        return None
    owners = defaultdict(list)
    for part in parts:
        for net in dict.fromkeys(part.terminal_nets):
            owners[net].append(part)
    stages = []
    used = set()
    for package in packages:
        stage = _stage(package, owners, grounds)
        if stage is None or used.intersection(stage.members()):
            return None
        stages.append(stage)
        used.update(stage.members())
    rails = _power_rails([part for part in parts if part not in used], grounds)
    if rails is None:
        return None
    if any(
        net not in grounds and net not in rails and net not in (stage.sense, stage.output)
        and (len(owners[net]) > 1 or stage.package.terminal_nets.count(net) > 1)
        for stage in stages for net in stage.package.terminal_nets
    ):
        return None
    if set(rails) - {net for stage in stages for net in stage.package.terminal_nets}:
        return None

    contacts = Counter(net for package in packages for net in package.terminal_nets)
    ports = set(circuit.connector_pins).union(circuit.terminals or (), circuit.port or ())
    local_sources = {
        net: source for net, (source, shunts) in rails.items()
        if source.kind == "V" and not shunts and contacts[net] == 1 and net not in ports
    }
    rails = {net: rail for net, rail in rails.items() if net not in local_sources}
    drawing = Layout(pin_grid=GRID)
    pins = {net: [] for net in rails}
    caption_chars = max((min(24, len(labels[part.name][0])) for part in parts), default=0)
    pitch = max(4.5, ceil((0.16 * caption_chars + 0.5) / (2 * GRID)) * 2 * GRID)
    upper = 0
    left = right = 0
    for stage in sorted(stages, key=lambda stage: stage.source.order):
        height = ceil(package_half_height(stage.package, GRID) / GRID) * GRID
        y = upper - height - FEEDBACK_CLEARANCE - FEEDBACK_PITCH * (len(stage.feedback) - 1)
        stage_left, stage_right, bottom_y = _draw_stage(
            drawing, stage, y, height, grounds, pins, local_sources, pitch,
        )
        upper = bottom_y - 1.75
        left = min(left, stage_left)
        right = max(right, stage_right)
    supply_caption = max(
        (len(line) for source, _ in rails.values() for line in labels[source.name]), default=0,
    )
    clearance = max(3.25, ceil((0.17 * supply_caption + 1.25) / GRID) * GRID)
    if not _draw_supply_rails(drawing, rails, pins, left, right, pitch, clearance):
        return None
    mark_crossings(drawing)
    return drawing
