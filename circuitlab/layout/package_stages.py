"""Conventional layouts for packaged amplifiers and their passive networks."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import product
from math import ceil

from circuitlab.layout.geometry import (
    ChipPackage, Layout, add_wire, ground_at, ground_lead,
    package_half_height, package_pins, place_symbol, quad_package,
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
class Branch:
    """Parallel two-terminal parts between consecutive stage junctions."""

    parts: tuple[Component, ...]
    start: str
    end: str

    @property
    def span(self):
        return max(map(_span, self.parts))


@dataclass(frozen=True)
class Stage:
    package: ChipPackage
    source: Component
    input_net: str
    sense: str
    output: str
    path: tuple[Branch, ...]
    feedback: tuple[Component, ...]
    loads: tuple[tuple[Branch, ...], ...]

    def members(self):
        return {self.package, self.source, *self.feedback,
                *(part for branch in self.path for part in branch.parts),
                *(part for path in self.loads for branch in path for part in branch.parts)}


def _grounded_return(part, net, grounds):
    if part.p == net:
        return part.n in grounds
    return part.n == net and part.p in grounds


def _parallel_branch(parts, net):
    """Accept one part or a parallel bank, without crossing a distinct branch."""
    if not parts or any(part.kind not in PASSIVE or part.p == part.n for part in parts):
        return None
    opposite = {part.n if part.p == net else part.p for part in parts}
    if len(opposite) != 1:
        return None
    return Branch(tuple(parts), net, opposite.pop())


def _input_chain(sense, output, package, feedback, owners, grounds):
    """Walk input series sections and parallel banks back to a grounded source."""
    net = sense
    path = []
    seen = {package, *feedback}
    visited = {output, *grounds}
    while net not in visited:
        visited.add(net)
        remaining = [part for part in owners[net] if part not in seen]
        if len(remaining) == 1:
            part = remaining[0]
            if part.is_source and part.kind in {"V", "I"} and _grounded_return(part, net, grounds):
                return part, net, tuple(reversed(path))
        branch = _parallel_branch(remaining, net)
        if branch is None:
            break
        path.append(Branch(branch.parts, branch.end, net))
        seen.update(branch.parts)
        net = branch.end
    return None


def _load_paths(output, package, feedback, owners, grounds):
    """Keep passive output chains together up to ground or a package contact."""
    first = defaultdict(list)
    for part in owners[output]:
        if part not in (package, *feedback):
            first[part.n if part.p == output else part.p].append(part)
    paths, used = [], set()
    for remaining in first.values():
        net, path, visited = output, [], set()
        while net not in grounds:
            branch = _parallel_branch(remaining, net)
            if branch is None or net in visited or used.intersection(branch.parts):
                return None
            path.append(branch)
            visited.add(net)
            used.update(branch.parts)
            net = branch.end
            remaining = [part for part in owners[net] if part not in branch.parts]
            if remaining and all(isinstance(part, ChipPackage) for part in remaining):
                break
        paths.append(tuple(path))
    return tuple(paths)


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
        loads = _load_paths(output, package, feedback, owners, grounds)
        if loads is not None:
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
    """Run series sections and parallel input banks along the sensing pin row."""
    _, sense_y = sense_pin
    widths = [pitch if len(branch.parts) == 1 else FEEDBACK_PITCH * (len(branch.parts) - 1) + 2
              for branch in stage.path]
    source_x = sense_x - sum(widths) - .25
    return_y = _grounded_branch(drawing, stage.source, stage.input_net, source_x, sense_y)
    previous_x = source_x
    slot = source_x
    for branch, width in zip(stage.path, widths):
        if len(branch.parts) == 1:
            start = slot + (width - branch.span) / 2
            end = start + branch.span
            add_wire(drawing, branch.start, (previous_x, sense_y), (start, sense_y))
            place_symbol(drawing, branch.parts[0], branch.start, (start, sense_y), (end, sense_y))
            previous_x = end
        else:
            top, bottom = sense_y + branch.span / 2, sense_y - branch.span / 2
            columns = [slot + .75 + i * FEEDBACK_PITCH for i in range(len(branch.parts))]
            add_wire(drawing, branch.start, (previous_x, sense_y), (slot + .25, sense_y),
                     (slot + .25, top), (columns[-1], top))
            add_wire(drawing, branch.end, (columns[0], bottom), (slot + width - .25, bottom),
                     (slot + width - .25, sense_y))
            for part, x in zip(branch.parts, columns):
                half = _span(part) / 2
                a, b = (x, sense_y + half), (x, sense_y - half)
                add_wire(drawing, branch.start, (x, top), a)
                add_wire(drawing, branch.end, b, (x, bottom))
                place_symbol(drawing, part, branch.start, a, b)
            drawing.junctions.setdefault(branch.start, []).extend((x, top) for x in columns[1:-1])
            drawing.junctions.setdefault(branch.end, []).extend((x, bottom) for x in columns[1:-1])
            previous_x = slot + width - .25
            return_y = min(return_y, bottom - .5)
        slot += width
    add_wire(drawing, stage.sense, (previous_x, sense_y), sense_pin)
    drawing.junctions.setdefault(stage.sense, []).append((sense_x, sense_y))
    return return_y


def _draw_stage(drawing, stage, y, height, grounds, supply_pins, pitch, outputs):
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

    input_return_y = _draw_input_path(drawing, stage, pins[stage.sense], sense_x, pitch)
    bottom_y = min(y - height, input_return_y)

    feedback_start = y + height + FEEDBACK_CLEARANCE
    feedback_end = feedback_start + (max(map(_span, stage.feedback)) if len(stage.feedback) > 1 else 0)
    add_wire(drawing, stage.sense, (sense_x, sense_y), (sense_x, feedback_end))
    add_wire(drawing, stage.output, pins[stage.output], (output_x, output_y), (output_x, feedback_end))
    center = round((sense_x + output_x) / (2 * GRID)) * GRID
    for index, part in enumerate(stage.feedback):
        half = _span(part) / 2
        if len(stage.feedback) == 1:
            a, b = (center - half, feedback_start), (center + half, feedback_start)
            add_wire(drawing, stage.sense, (sense_x, feedback_start), a)
            add_wire(drawing, stage.output, b, (output_x, feedback_start))
        else:
            x = center + (index - (len(stage.feedback) - 1) / 2) * 2
            middle = (feedback_start + feedback_end) / 2
            a, b = (x, middle + half), (x, middle - half)
            add_wire(drawing, stage.sense, (sense_x, feedback_end), (x, feedback_end), a)
            add_wire(drawing, stage.output, b, (x, feedback_start), (output_x, feedback_start))
        place_symbol(drawing, part, stage.sense, a, b)

    load_columns = [max(len(branch.parts) for branch in path) for path in stage.loads]
    if stage.loads:
        add_wire(drawing, stage.output, (output_x, output_y), (output_x, load_y))
        drawing.junctions.setdefault(stage.output, []).append((output_x, output_y))
        load_x = output_x
        for path, columns in zip(stage.loads, load_columns):
            if load_x != output_x:
                add_wire(drawing, stage.output, (output_x, load_y), (load_x, load_y))
            row = load_y
            for branch in path:
                end = row - branch.span
                for offset, part in enumerate(branch.parts):
                    x = load_x + offset * pitch
                    half, center = _span(part) / 2, (row + end) / 2
                    a, b = (x, center + half), (x, center - half)
                    add_wire(drawing, branch.start, (load_x, row), (x, row), a)
                    add_wire(drawing, branch.end, b, (x, end), (load_x, end))
                    place_symbol(drawing, part, branch.start, a, b)
                row = end
            if path[-1].end in grounds:
                ground_at(drawing, (load_x, row), path[-1].end)
            else:
                outputs[path[-1].end] = load_x, row
            bottom_y = min(bottom_y, row)
            load_x += columns * pitch
    # Multiple physical contacts may intentionally share ground. Draw each contact.
    for _, net, on_left, offset in package_contacts:
        direction = -1 if on_left else 1
        start = left if on_left else right, y + offset
        if net in grounds:
            ground_lead(drawing, start, net, turns=1 if on_left else 3, length=.75)
        elif net in supply_pins:
            supply_pins[net].append((start, direction))
    return (sense_x,
            output_x + max(0, sum(load_columns) - 1) * pitch,
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


def _draw_terminal_packages(drawing, packages, outputs, upper, grounds, right):
    """Keep output connectors below the stages, with one external lane per net."""
    for package in packages:
        contacts = list(package_pins(package, GRID))
        connected = [(net, on_left, offset) for _, net, on_left, offset in contacts if net in outputs]
        height = ceil(package_half_height(package, GRID) / GRID) * GRID
        y = upper - height - 1.5 - len(connected) * .75
        place_symbol(drawing, package, package.p, (0, y), (7, y))
        for index, (net, on_left, offset) in enumerate(connected):
            start = outputs.pop(net)
            outer_x = right + 3.25 + index * .75
            approach_x = -1.25 - index * .75 if on_left else 8.25 + index * .75
            track_y = upper - .5 - index * .75
            pin = (0 if on_left else 7, y + offset)
            add_wire(drawing, net, start, (outer_x, start[1]), (outer_x, track_y),
                     (approach_x, track_y), (approach_x, pin[1]), pin)
        for _, net, on_left, offset in contacts:
            pin = (0 if on_left else 7, y + offset)
            if net in grounds:
                ground_lead(drawing, pin, net, turns=1 if on_left else 3, length=.75)
            elif not any(net == linked for linked, _, _ in connected):
                end = (pin[0] + (-.75 if on_left else .75), pin[1])
                add_wire(drawing, net, pin, end)
                drawing.connectors.append((net, end, net, on_left, offset < 0))
        upper = y - height - 1.75


def package_stage_layout(circuit, labels):
    """Stack package feedback stages, their output connectors and supply rails."""
    parts, grounds = circuit.components, circuit.grounds
    packages = [part for part in parts if isinstance(part, ChipPackage)]
    if not packages or len(parts) > 64 or any(map(quad_package, packages)):
        return None
    owners = defaultdict(list)
    for part in parts:
        for net in dict.fromkeys(part.terminal_nets):
            owners[net].append(part)
    stages, terminals = [], []
    used = set()
    for package in packages:
        stage = _stage(package, owners, grounds)
        if stage is None:
            terminals.append(package)
            continue
        if used.intersection(stage.members()):
            return None
        stages.append(stage)
        used.update(stage.members())
    if not stages:
        return None
    outputs = [path[-1].end for stage in stages for path in stage.loads if path[-1].end not in grounds]
    if (len(outputs) != len(set(outputs))
            or any(sum(package.terminal_nets.count(net) for package in terminals) != 1 for net in outputs)
            or any(net not in grounds and net not in outputs and len(owners[net]) > 1
                   for package in terminals for net in package.terminal_nets)):
        return None
    used.update(terminals)
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

    drawing = Layout(pin_grid=GRID)
    pins = {net: [] for net in rails}
    caption_chars = max((min(24, len(labels[part.name][0])) for part in parts), default=0)
    pitch = max(4.5, ceil((0.16 * caption_chars + 0.5) / (2 * GRID)) * 2 * GRID)
    upper = 0
    left = right = 0
    output_pins = {}
    for stage in sorted(stages, key=lambda stage: stage.source.order):
        height = ceil(package_half_height(stage.package, GRID) / GRID) * GRID
        feedback_height = max(map(_span, stage.feedback)) if len(stage.feedback) > 1 else 0
        y = upper - height - FEEDBACK_CLEARANCE - feedback_height
        stage_left, stage_right, bottom_y = _draw_stage(
            drawing, stage, y, height, grounds, pins, pitch, output_pins,
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
    _draw_terminal_packages(drawing, terminals, output_pins, upper, grounds, right)
    mark_crossings(drawing)
    return drawing
