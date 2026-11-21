"""Place and route arbitrary circuit graphs with orthogonal wire channels."""
# General routing assigns components to cells and connects pins through shared net tracks.
# Candidate cost chooses among electrically valid drawings; optimization can shorten a route
# but cannot justify missing contacts, shorts between nets, or wires through component bodies.

import math
from itertools import islice, product
from collections import defaultdict, deque
from copy import deepcopy
from statistics import median_low

from circuitlab.layout.geometry import (
    source_pin_is_upper,
    BJT_PIN_OFFSET,
    POT_PIN_OFFSET,
    Layout,
    PACKAGE_WIDTH,
    PACKAGE_WIRE_CLEARANCE,
    add_wire,
    component_pins,
    grid_symbol,
    grid_terminal,
    ground_at,
    ground_lead,
    inline_grounded_voltage,
    input_source_indices,
    package_half_height,
    place_symbol,
)
from circuitlab.layout.automatic import compact, series_runs, source_cells
from circuitlab.layout.bjt_stages import multivibrator_layout
from circuitlab.layout.buffered_divider import buffered_divider_layout
from circuitlab.layout.checks import clear_of_bodies, clear_package_wires, preserves_nets, segments_intersect
from circuitlab.layout.checks import mark_crossings as _mark_crossings
from circuitlab.layout.grid import align_layout
from circuitlab.layout.package_stages import package_stage_layout
from circuitlab.layout.route_quality import crossing_count, wire_length
from circuitlab.layout.search import (
    circuit_instance_cells, evolve_placements, feedback_pairs, feedback_stage_cells, placement_penalty,
)
from circuitlab.layout.shorten import compact_spacing, optimize_layout
from circuitlab.layout.wire_edit import count_wire_bends
from circuitlab.layout.wiring import connector_names, labeled, signal_flow
from circuitlab.model.graph import CircuitError

SYMBOL_WIDTH = 4.3
LANE_PITCH = 0.46
TRACK_PITCH = 0.96


# Try readable topology-specific views, then general routing, and validate the chosen
# geometry against the original electrical net identities.
def schematic_layout(circuit, labels, mode):
    """Choose a layout and check that its visible connections preserve the graph."""
    if mode == "labeled":
        return labeled(circuit, labels)
    if mode == "auto":
        for build, args in (
            (buffered_divider_layout, (circuit, labels)),
            (multivibrator_layout, (circuit,)),
            (package_stage_layout, (circuit, labels)),
        ):
            candidate = build(*args)
            if candidate is None or not preserves_nets(circuit, candidate):
                continue
            edges = [edge for segments in candidate.wires.values() for edge in segments]
            if clear_of_bodies(edges, candidate):
                return candidate
    parts = circuit.components
    if len(parts) > 64 or any(part.kind == "H" for part in parts):
        return fast_layout(circuit, labels)
    conventional = compact(circuit, labels) or signal_flow(circuit)
    if conventional and preserves_nets(circuit, conventional):
        layout = conventional
    elif _inline_grounded_parts(circuit, component_pins(parts), (), shunts=False):
        return fast_layout(circuit, labels)
    else:
        layout = connected_layout(circuit, labels)
    # Keep direct series joins and rail geometry; accept only group moves that
    # also survive the viewer's subsequent grid snap. Saved placements bypass
    # this generator; dense packages retain fast_layout's existing margins.
    return compact_spacing(circuit, optimize_layout(circuit, layout), budget=12, grid_safe=True)


def fast_layout(circuit, labels, *, boundary=(), shorten=True):
    """Route a small fixed set of placements and skip iterative refinement."""
    parts = circuit.components
    if not parts:
        # Mathematical chips have no internal symbols. Keep their actual
        # boundary contacts editable/persistable without inventing devices.
        drawing = Layout()
        for row, net in enumerate(dict.fromkeys(boundary)):
            start = 0.0, -2.0 * row
            drawing.boundary[net] = start
            add_wire(drawing, net, start, (1.5, start[1]))
        return drawing
    runs, reversed_parts = series_runs(circuit)
    pins = component_pins(parts, reversed_parts=reversed_parts)
    if not pins:
        return labeled(circuit, labels)
    inline = _inline_grounded_parts(circuit, pins, boundary)
    order = next(_orders(parts, pins, circuit.grounds))
    center = math.ceil(math.sqrt(len(parts) * 0.72))
    widths = (center,) if len(parts) > 32 else range(max(1, center - 1), center + 2)
    sources = [i for i in order if parts[i].is_source]
    others = [i for i in order if not parts[i].is_source]
    candidates = []
    for width in widths:
        cells = source_cells(circuit, order, width, runs)
        candidates.append(cells)
        if len(parts) <= 32:
            candidates.append({
                i: divmod(j, width) for j, i in enumerate(sources + others)
            })
    if len(circuit.instances) > 1:
        units = [tuple(run) for run in runs]
        run_members = {i for unit in units for i in unit}
        units.extend((i,) for i in others if i not in run_members)
        candidates.extend(circuit_instance_cells(circuit, candidates[0], units, feedback_pairs(parts)))

    def route(cells, regular, pack_tracks, attached):
        drawing = _channel_layout(
            circuit, labels, pins, set(), reversed_parts, cells, set(),
            tight=True, regular=regular, boundary=boundary, pack_tracks=pack_tracks, attached=attached,
        )
        _mark_crossings(drawing)
        return drawing

    def usable(drawing, pack_tracks):
        # A package's fixed external contacts can end up too close after
        # grid rounding. Retain the exclusive-lane fallback in that case.
        return clear_package_wires(drawing) and preserves_nets(circuit, drawing) and (
            not boundary or not pack_tracks or align_layout(circuit, drawing, keep_package_clearance=True) is not None
        )

    best = None
    # Keep the original exclusive-lane routing available if the packed
    # candidates cannot preserve a particular set of contacts.
    track_modes = ((True, False), (True, True), (False, False), (False, True))
    sources_only = {net: details for net, details in inline.items() if parts[details[0]].is_source}
    attachments = [inline]
    if sources_only != inline:
        attachments.append(sources_only)
    if sources_only:
        attachments.append({})
    for attached, (pack_tracks, regular) in product(attachments, track_modes):
        for cells in candidates:
            layout = route(cells, regular, pack_tracks, attached)
            cost = _layout_cost(layout)
            if best is not None and cost >= best[0]:
                continue
            if usable(layout, pack_tracks):
                best = cost, layout
        if best is not None:
            # A bounded search keeps initial rendering predictable. Never replace
            # the baseline with longer wiring or a worse congestion score.
            if shorten and len(parts) <= 64:
                best_length = wire_length(best[1])
                for trial in islice(_placement_trials(circuit, pins, runs, order), 4):
                    cells = source_cells(circuit, trial, center, runs)
                    drawing = route(cells, regular, pack_tracks, attached)
                    trial_length, cost = wire_length(drawing), _layout_cost(drawing)
                    if trial_length < best_length - 1e-8 and cost <= best[0] and usable(drawing, pack_tracks):
                        best, best_length = (cost, drawing), trial_length
                optimized = optimize_layout(circuit, best[1])
                if usable(optimized, pack_tracks):
                    return optimized
            return best[1]
    # A single row or column leaves room for exclusive tracks when the
    # compact placements cannot keep nets distinct.
    for width in (len(parts), 1):
        cells = {i: divmod(index, width) for index, i in enumerate(sources + others)}
        drawing = _channel_layout(
            circuit, labels, pins, set(), reversed_parts, cells, set(), regular=True, boundary=boundary,
        )
        _mark_crossings(drawing)
        if usable(drawing, False):
            return drawing
    raise CircuitError("Could not route this schematic with distinct nets and clear package edges.")


def _inline_grounded_parts(circuit, pins, boundary, *, shunts=True):
    """Find grounded sources and shunts that serve exactly one side pin."""
    members = defaultdict(list)
    for pin in pins:
        members[pin[1]].append(pin)
    ports = set(boundary).union(circuit.connector_pins, circuit.terminals or (), circuit.port or ())
    attached = {}
    for index, branch in enumerate(circuit.components):
        voltage_source = branch.kind == "V" and branch.is_source
        if not voltage_source and not (shunts and branch.kind in {"R", "C", "L", "X", "Z"}):
            continue
        live, ground = (branch.n, branch.p) if branch.p in circuit.grounds else (branch.p, branch.n)
        if (ground not in circuit.grounds or live in circuit.grounds or live in ports
                or len(members[live]) != 2):
            continue
        target, _, _, offset = next(pin for pin in members[live] if pin[0] != index)
        part = circuit.components[target]
        if part.kind in {"H", "P", "Q", "S"} or (part.kind in {"O", "A"} and abs(offset) < 1):
            attached[live] = index, ground
    return attached


def _active_cells(cells, attached):
    """Omit the branch cells already drawn at the attached device's side."""
    omitted = {index for index, _ in attached.values()}
    active = {i: cell for i, cell in cells.items() if i not in omitted}
    rows = {row: i for i, row in enumerate(sorted({r for r, _ in active.values()}))}
    columns = {col: i for i, col in enumerate(sorted({c for _, c in active.values()}))}
    return {i: (rows[row], columns[col]) for i, (row, col) in active.items()}


def _place_grounded_shunt(drawing, shunt, net, ground, terminal, left, lead, *, drop):
    direction = -1 if left else 1
    x, y = terminal
    near = (x + direction * lead, y)
    far = (near[0], y - 2.5) if drop else (near[0] + direction * 2.5, y)
    place_symbol(drawing, shunt, net, near, far)
    add_wire(drawing, net, terminal, near)
    ground_lead(drawing, far, ground, turns=0 if drop else (1 if left else 3), length=.5)


def _orders(parts, pins, grounds):
    pin_nets = [set() for _ in parts]
    owners = defaultdict(set)
    for i, net, *_ in pins:
        if net not in grounds:
            pin_nets[i].add(net)
            owners[net].add(i)
    sources = sorted(input_source_indices(parts), key=lambda i: parts[i].order)
    roots = [*sources, *range(len(parts))]
    for depth_first in (True, False):
        order, seen, used_nets = [], set(), set()
        for root in roots:
            pending = deque([root])
            while pending:
                i = pending.popleft()
                if i in seen:
                    continue
                order.append(i)
                seen.add(i)
                neighbors = []
                for net in sorted(pin_nets[i], key=lambda n: (len(owners[n]), n)):
                    if net not in used_nets:
                        used_nets.add(net)
                        neighbors.extend(sorted(owners[net] - seen))
                if depth_first:
                    pending.extendleft(reversed(neighbors))
                else:
                    pending.extend(neighbors)
        yield order


# Reuse a lane only when the entire run and its neighboring clearance fit.
def _channel_slot(tracks, low, high, clearance):
    for slot, occupied in enumerate(tracks):
        if all(high + clearance <= start or end + clearance <= low for start, end in occupied):
            occupied.append((low, high))
            return slot
    tracks.append([(low, high)])
    return len(tracks) - 1


# Use column gutters to reach shared horizontal tracks, keeping terminal identities
# attached to their own nets even when several routes cross visually.
def _channel_layout(
    circuit, labels, pins, flipped, reversed_parts, cells, local,
    *, tight=False, rotate=False, source_offsets=None, source_flips=(), regular=False,
    boundary=(), pack_tracks=True, attached=None,
):
    """Route from pins through column gutters to shared horizontal net tracks."""
    parts, grounds = circuit.components, circuit.grounds
    attached = attached or {}
    if attached:
        cells = _active_cells(cells, attached)
        pins = [pin for pin in pins if pin[0] in cells]
    # Dense package pinouts need the existing margins to survive grid rounding.
    compact = tight and not any(part.kind == "H" for part in parts)
    reuse_lanes = tight and pack_tracks
    drawing = Layout()
    columns = 1 + max(column for _, column in cells.values())
    rows = [cells[i][0] for i, *_ in pins]
    gutters = [cells[i][1] + (parts[i].is_source or not left) for i, _, left, _ in pins]
    groups, lanes = defaultdict(list), defaultdict(list)
    for i, (_, net, _, _) in enumerate(pins):
        groups[net].append(i)
    names = connector_names(groups)
    inline_leads, package_loops = {}, {}
    shunt_drops, shunt_depth = set(), defaultdict(float)
    if attached:
        # A shunt at the lowest pin can turn downward without masking another
        # terminal. Reserve its side clearance and the space above the tracks.
        side_bottom = {}
        for part, _, left, offset in pins:
            side_bottom[part, left] = min(offset, side_bottom.get((part, left), offset))
        for part, net, left, offset in pins:
            if (net in attached and not parts[attached[net][0]].is_source
                    and parts[part].kind in {"H", "Q"} and offset == side_bottom[part, left]):
                shunt_drops.add(net)
                inline_leads[net] = .75
                row = cells[part][0]
                shunt_depth[row] = max(shunt_depth[row], 4 - offset)
        # Stagger branch glyphs on close pins such as the two op-amp inputs.
        inline_sides = defaultdict(list)
        for part, net, left, offset in pins:
            if net not in attached or net in shunt_drops:
                continue
            side = inline_sides[part, left]
            nearby = {lead for y, lead in side if abs(offset - y) < 1}
            lead = .25
            while lead in nearby:
                lead += 1.75
            side.append((offset, lead))
            inline_leads[net] = lead
        # In a simple package circuit, tied neighbors can join beside their
        # pins without occupying a track across the entire drawing.
        for net, indices in groups.items():
            if net in grounds or net in boundary or len(indices) != 2:
                continue
            first, second = indices
            part, _, left, offset = pins[first]
            other, _, other_left, other_offset = pins[second]
            if (part == other and parts[part].kind == "H" and left == other_left
                    and abs(offset - other_offset) == 1):
                package_loops[net] = first, second
    # Reserve a horizontal track at the source terminal. Pins on the rest
    # of the circuit reach that track from below.
    source_rails = {}
    for net, indices in groups.items():
        sources = [i for i in indices if parts[pins[i][0]].is_source]
        if (
            not regular and net not in grounds and net not in boundary
            and sources and len(indices) > 1
        ):
            source_rails[net] = min(sources, key=lambda i: (rows[i], i))

    rotated = {}
    if rotate:
        for i in cells:
            part = parts[i]
            if part.kind not in "RCLXZK" or part.p == part.n:
                continue
            if rotate is not True and i not in rotate:
                continue
            if (part.p in grounds) != (part.n in grounds):
                live = part.n if part.p in grounds else part.p
            elif (part.p in source_rails) != (part.n in source_rails):
                live = part.p if part.p in source_rails else part.n
            elif rotate is not True:
                # A link between two ordinary signal nets can also turn when
                # one side is above the other.
                neighbor_rows = {
                    net: median_low([
                        rows[j] for j in groups[net] if pins[j][0] != i
                    ] or [cells[i][0]])
                    for net in (part.p, part.n)
                }
                if neighbor_rows[part.p] == neighbor_rows[part.n]:
                    continue
                live = min(neighbor_rows, key=lambda net: neighbor_rows[net])
            else:
                continue
            if {part.p, part.n} & local:
                continue
            neighbors = [rows[j] for j in groups[live] if pins[j][0] != i]
            # A vertical shunt is useful when its connection arrives from
            # above. Keep end-to-end series connections horizontal.
            if neighbors and (live in source_rails or min(neighbors) < cells[i][0]):
                rotated[i] = live

    direct = {}
    vertical = {}
    for net, indices in groups.items():
        if (
            regular or net in grounds or net in boundary or net in source_rails or net in package_loops
            or (len(indices) != 2 and net not in local)
        ):
            continue
        right_pins, left_pins = {}, {}
        for i in indices:
            part, _, left, offset = pins[i]
            if part in rotated:
                continue
            key = rows[i], gutters[i], offset
            if left and not parts[part].is_source:
                left_pins[key] = i
            elif not parts[part].is_source or source_pin_is_upper(parts[part], left, grounds):
                right_pins[key] = i
        if key := next(iter(sorted(right_pins.keys() & left_pins.keys())), None):
            direct[net] = right_pins[key], left_pins[key]
        elif len(indices) == 2:
            a, b = indices
            if (
                gutters[a] == gutters[b] and rows[a] != rows[b]
                and all(pins[i][0] not in rotated for i in indices)
            ):
                vertical[net] = a, b

    padding = [[0.0, 0.0] for _ in range(columns + 1)]
    direct_pins = {i for pair in direct.values() for i in pair}
    vertical_lanes = defaultdict(list)
    for net, (a, _) in vertical.items():
        vertical_lanes[gutters[a]].append(net)
    for i, (part, net, left, _) in enumerate(pins):
        g = gutters[i]
        left = left and not parts[part].is_source
        single = len(groups[net]) == 1 and net not in boundary
        if net in shunt_drops:
            padding[g][left] = max(padding[g][left], 2.25)
        elif net in attached:
            padding[g][left] = max(padding[g][left], 4.25 + inline_leads[net])
        elif net in package_loops:
            padding[g][left] = max(padding[g][left], .75)
        elif net not in grounds and single:
            padding[g][left] = max(padding[g][left], 0.43 * len(names[net]))
        elif (
            net not in grounds and net not in vertical and net != rotated.get(part)
            and i not in direct_pins
            and i != source_rails.get(net)
        ):
            lanes[gutters[i]].append(i)

    widths = [(4.5 if compact else 4.7) if tight else 5.3] * columns
    for i in cells:
        part = parts[i]
        col = cells[i][1]
        name, *values = labels[part.name]
        label_width = max([min(len(name), 24), *map(len, values)])
        if not part.is_source:
            widths[col] = max(widths[col], 0.35 * label_width + (0.55 if tight else 0.8))
        if part.kind == "H":
            widths[col] = max(widths[col], PACKAGE_WIDTH + 0.4)

    channels, target_rows = defaultdict(list), {}
    for net, indices in groups.items():
        if net in source_rails:
            target_rows[net] = rows[source_rails[net]]
            continue
        if (
            net in grounds or net in package_loops or (len(indices) == 1 and net not in boundary)
            or net in vertical or (net in direct and len(indices) == 2)
        ):
            continue
        row = median_low(rows[i] for i in indices)
        upper = [max(0, rows[i] - 1) for i in indices if net == rotated.get(pins[i][0])]
        lower = [
            rows[i] for i in indices
            if pins[i][0] in rotated and net != rotated[pins[i][0]]
        ]
        if upper:
            row = min(row, min(upper))
        if lower:
            row = max(row, max(lower))
        target_rows[net] = row
        channels[row].append(net)

    centers, lane_x, vertical_x = [], {}, {}
    x = 0.0
    for g in range(columns + 1):
        x += padding[g][False]
        # Each side of the gutter approaches its own lanes. On any row the
        # two opposing sets of terminal stubs therefore cannot touch.
        indices = sorted(
            lanes[g],
            key=lambda i: (
                pins[i][2] and not parts[pins[i][0]].is_source,
                rows[i],
                (source_pin_is_upper(parts[pins[i][0]], pins[i][2], grounds)
                 != (pins[i][0] in source_flips))
                if parts[pins[i][0]].is_source else pins[i][3],
            ),
        )
        split = next(
            (j for j, i in enumerate(indices) if pins[i][2] and not parts[pins[i][0]].is_source),
            len(indices),
        )
        shared = vertical_lanes[g]
        lead = 0.75 if compact else 0.9
        # A drop stays between its pin's row and its target rail's row. Keep
        # opposing leads in separate lane groups, and allow pins on different
        # rows to share a lane only if their drops cannot meet.
        lane_count = 0
        for side, group in enumerate((indices[:split], indices[split:])):
            if side:
                right_lanes = lane_count
                # The middle group is reserved for direct vertical net pairs.
                lane_count += len(shared)
            occupied_lanes = []
            for j, i in enumerate(group):
                if reuse_lanes:
                    low, high = sorted((rows[i], target_rows[pins[i][1]]))
                    j = _channel_slot(occupied_lanes, low, high, 1)
                lane_x[i] = x + lead + (lane_count + j) * LANE_PITCH
            lane_count += len(occupied_lanes) if reuse_lanes else len(group)
        for j, net in enumerate(shared, start=right_lanes):
            vertical_x[net] = x + lead + j * LANE_PITCH
        x += ((1.1 if compact else 1.25) if tight else 1.8) + max(
            0, lane_count - 1
        ) * LANE_PITCH + padding[g][True]
        if g < columns:
            centers.append(x + widths[g] / 2)
            x += widths[g]

    for i, (part, net, _, _) in enumerate(pins):
        if net == rotated.get(part):
            lane_x[i] = centers[cells[part][1]]

    row_y, track_y = [0.0], {}
    source_rows = {cells[i][0] for i in cells if parts[i].is_source}
    package_rows = defaultdict(float)
    for i, (row, _) in cells.items():
        if parts[i].kind == "H":
            # Keep room for the visible gap after both parts and tracks snap.
            height = package_half_height(parts[i]) + 2 * PACKAGE_WIRE_CLEARANCE
            package_rows[row] = max(package_rows[row], height)
    for row in range(max(row for row, _ in cells.values()) + 1):
        tracks = sorted(
            channels[row],
            key=lambda n: (
                max(lane_x[i] for i in groups[n] if i not in direct_pins)
                - min(lane_x[i] for i in groups[n] if i not in direct_pins),
                n,
            ),
        )
        clearance = max(2.5 if compact else 2.8, package_rows[row], shunt_depth[row])
        if regular and row in source_rows:
            # Reserve space below the source's grounded return before
            # bringing unrelated horizontal tracks across its column.
            clearance = max(clearance, 3.3)
        occupied_tracks = []
        for net in tracks:
            # Boundary wires run to the edge of the drawing, and a side
            # branch can extend past its assigned pin lanes. Keep these
            # tracks exclusive. Other non-overlapping runs can share a row.
            if reuse_lanes and net not in boundary and net not in direct:
                xs = [lane_x[i] for i in groups[net]]
                left, right = min(xs), max(xs)
                j = _channel_slot(occupied_tracks, left, right, LANE_PITCH)
            else:
                j = len(occupied_tracks)
                occupied_tracks.append([(-math.inf, math.inf)])
            track_y[net] = row_y[-1] - clearance - TRACK_PITCH * j
        pitch = (3.8 if compact else 4.15) if tight else 4.8 if row in source_rows else 5.0
        if package_rows:
            pitch = max(pitch, clearance + package_rows[row + 1] + 1.2)
        row_y.append(row_y[-1] - pitch - TRACK_PITCH * len(occupied_tracks))

    symbols = {}
    for i in sorted(cells):
        part = parts[i]
        row, col = cells[i]
        symbol = grid_symbol(
            part,
            centers[col] - (PACKAGE_WIDTH if part.kind == "H" else SYMBOL_WIDTH) / 2,
            row_y[row],
            grounds,
            top="-" if i in flipped else "+",
            reverse=i in reversed_parts,
        )
        if part.is_source and source_offsets:
            _, (ax, ay), (bx, by), top = symbol
            offset = source_offsets.get(i, 0)
            symbol = (part, (ax, ay - offset), (bx, by - offset), top)
        if i in source_flips:
            _, a, b, top = symbol
            symbol = (part, b, a, top)
        if i in rotated:
            x, y = centers[col], row_y[row]
            upper, lower = (x, y), (x, y - 2.5)
            a, b = (upper, lower) if part.p == rotated[i] else (lower, upper)
            symbol = (part, a, b, "+")
        drawing.positions[part.name] = symbol[1]
        drawing.symbols.append(symbol)
        symbols[i] = symbol
    terminals = []
    for i, _, left, offset in pins:
        part, a, b, _ = symbols[i]
        terminals.append(
            (a if (left != (i in reversed_parts)) else b)
            if part.is_source or i in rotated
            else grid_terminal(part, a, b, left, offset)
        )

    for net, indices in groups.items():
        if net in attached:
            branch_index, ground = attached[net]
            branch = parts[branch_index]
            pin, = indices
            left = pins[pin][2]
            if branch.is_source:
                inline_grounded_voltage(drawing, branch, net, ground, terminals[pin], left=left, lead=inline_leads[net])
            else:
                _place_grounded_shunt(
                    drawing, branch, net, ground, terminals[pin], left, inline_leads[net],
                    drop=net in shunt_drops,
                )
            continue
        if net in package_loops:
            first, second = package_loops[net]
            a, b = terminals[first], terminals[second]
            x = a[0] + (-.5 if pins[first][2] else .5)
            add_wire(drawing, net, a, (x, a[1]), (x, b[1]), b)
            continue
        if net in grounds:
            lower = {pins[i][0] for i in indices if pins[i][2] and pins[i][3] < 0}
            for i in indices:
                part, _, left, offset = pins[i]
                a = terminals[i]
                if part in rotated:
                    ground_at(drawing, a, net)
                    continue
                if parts[part].is_source:
                    lower_terminal = min(
                        point[1] for point in symbols[part][1:3]
                    )
                    if a[1] == lower_terminal:
                        b = (a[0], a[1] - 0.3)
                        add_wire(drawing, net, b, a)
                    else:
                        b = (a[0] + 0.72, a[1])
                        add_wire(drawing, net, a, b)
                    ground_at(drawing, b, net)
                    continue
                # Only the paired amplifier inputs are exactly 0.7 apart.
                # Package pins need their own ground connections.
                if parts[part].kind == "H":
                    ground_lead(drawing, a, net, turns=1 if left else 3, length=.75)
                    continue
                b = (a[0] + (-0.6 if left else 0.6), a[1])
                add_wire(drawing, net, a, b)
                if left and offset > 0 and part in lower and parts[part].kind in {"O", "A"}:
                    add_wire(drawing, net, (b[0], b[1] - 0.7), b)
                else:
                    ground_at(drawing, b, net)
            continue
        if len(indices) == 1 and net not in boundary:
            for i in indices:
                a = terminals[i]
                left = pins[i][2] and not parts[pins[i][0]].is_source
                b = (a[0] + (-0.72 if left else 0.72), a[1])
                add_wire(drawing, net, a, b)
                drawing.connectors.append(
                    (net, b, names[net], left, pins[i][3] < 0)
                )
        elif net in vertical:
            a, b = sorted((terminals[i] for i in vertical[net]), key=lambda p: p[1])
            x = vertical_x[net]
            add_wire(drawing, net, a, (x, a[1]), (x, b[1]), b)
        else:
            route = indices
            branch = None
            rail = source_rails.get(net)
            if net in direct:
                a, b = (terminals[i] for i in direct[net])
                branch = ((a[0] + b[0]) / 2, a[1])
                add_wire(drawing, net, a, b)
                drawing.labels[net] = branch
                route = [i for i in indices if i not in direct_pins]
                if not route:
                    continue
            if rail is not None:
                route = [i for i in indices if i != rail]
                left, ty = terminals[rail]
            else:
                ty = track_y[net]
            xs = [lane_x[i] for i in route]
            if branch:
                # Choose a free lane inside the gap for the side branch.
                gutter = gutters[direct[net][0]]
                track_x = lane_x[lanes[gutter][0]] - 0.46 if lanes[gutter] else branch[0]
                branch = (track_x, branch[1])
                xs.append(track_x)
                low, high = sorted((branch[1], ty))
                add_wire(drawing, net, (track_x, low), (track_x, high))
                drawing.junctions.setdefault(net, []).append(branch)
            if rail is None:
                left = min(xs)
            right = max(xs)
            add_wire(drawing, net, (left, ty), (right, ty))
            if not branch:
                drawing.labels[net] = ((left + right) / 2, ty)
            for i in route:
                x = lane_x[i]
                a = terminals[i]
                if a[0] != x:
                    add_wire(drawing, net, a, (x, a[1]))
                low, high = sorted((a[1], ty))
                add_wire(drawing, net, (x, low), (x, high))
                if left < x < right:
                    drawing.junctions.setdefault(net, []).append((x, ty))

    if boundary:
        attach_boundary(drawing, track_y, boundary, grounds)
    return drawing


def attach_boundary(drawing, track_y, boundary, grounds):
    """Bring each used package port to a short terminal at the edge of its detail."""
    left = min(
        [a[0] for _, a, _, _ in drawing.symbols]
        + [p[0] for wires in drawing.wires.values() for edge in wires for p in edge]
    ) - 1.1
    for net in boundary:
        if net in grounds:
            point = next(
                (p for p in drawing.grounds if drawing.ground_names.get(p, "0") == net),
                None,
            )
        elif net in track_y:
            y = track_y[net]
            start = min(
                p[0] for edge in drawing.wires[net] for p in edge if p[1] == y
            )
            point = left, y
            add_wire(drawing, net, point, (start, y))
        else:
            point = None
        if point is not None:
            drawing.boundary[net] = point


def _layout_cost(drawing):
    """Measure the visible cost of a candidate in schematic grid units."""
    gaps = crossing_count(drawing)
    points = [point for wires in drawing.wires.values() for edge in wires for point in edge]
    points.extend(point for _, a, b, _ in drawing.symbols for point in (a, b))
    xs, ys = zip(*points)
    span = max(max(xs) - min(xs), 1.25 * (max(ys) - min(ys)))
    return (wire_length(drawing) + 14 * gaps + 16 * len(drawing.connectors)
            + count_wire_bends(drawing) + 0.5 * span)


def _placement_trials(circuit, pins, runs, order):
    """Propose swaps and moves between nearby blocks that share a net."""
    parts = circuit.components
    by_member = {i: tuple(run) for run in runs for i in run}
    blocks = list(
        dict.fromkeys(by_member.get(i, (i,)) for i in order if not parts[i].is_source)
    )
    position = {i: j for j, block in enumerate(blocks) for i in block}
    nets = defaultdict(set)
    for i, net, *_ in pins:
        if i in position and net not in circuit.grounds:
            nets[net].add(position[i])

    pairs = set()
    for members in nets.values():
        indices = sorted(members)
        pairs.update(zip(indices, indices[1:]))
    # Start with distant connected blocks, plus neighboring blocks whose
    # order can determine whether a branch fits on a row.
    moves = sorted(pairs, key=lambda pair: (-(pair[1] - pair[0]), pair))
    moves = moves[:10] + [(i, i + 1) for i in range(min(8, len(blocks) - 1))]
    sources = [i for i in order if parts[i].is_source]
    for left, right in dict.fromkeys(moves):
        moved = blocks.copy()
        if right == left + 1:
            moved[left], moved[right] = moved[right], moved[left]
        else:
            moved.insert(left + 1, moved.pop(right))
        yield [*sources, *(i for block in moved for i in block)]


# Search multiple placements instead of committing the first valid route; refine only
# within bounded work so large designs remain usable.
def connected_layout(circuit, labels):
    """Search several placements and refine the best with local block moves."""
    parts = circuit.components
    if not parts:
        return Layout()
    runs, reversed_parts = series_runs(circuit)
    feedback = feedback_pairs(parts)
    run_members = {i for run in runs for i in run}
    # Keep the output end of a feedback component on the same side as the
    # amplifier output. Series runs already have a fixed physical orientation.
    reversed_parts.update(
        resistor for amp, resistor, _ in feedback
        if resistor not in run_members and parts[resistor].p == parts[amp].p
    )
    flipped = {
        i
        for i, part in enumerate(parts)
        if part.kind == "O" and part.in_plus == "0" and part.in_minus != "0"
    }
    pins = component_pins(parts, flipped, reversed_parts)
    size = len(parts)
    center = math.ceil(math.sqrt(size * 0.72))
    widths = sorted({max(1, min(size, center + offset)) for offset in (-2, -1, 0, 1, 2)})
    candidates = []
    seen = set()
    source_order = sorted(range(size), key=lambda i: (parts[i].order, i))
    local = {
        net
        for run in runs for a, b in zip(run, run[1:])
        for net in {parts[a].p, parts[a].n} & {parts[b].p, parts[b].n}
    }
    # A stage output can continue straight into the next block while other
    # consumers and feedback branches tap the same run.
    if len({amp for amp, _, _ in feedback}) > 1:
        local.update(parts[amp].p for amp, _, _ in feedback)
    units = [tuple(run) for run in runs]
    units.extend(
        (i,) for i, part in enumerate(parts) if i not in run_members and not part.is_source
    )

    def evaluate_cells(cells, order=None, columns=None, *, tight=False):
        key = tight, tuple(sorted(cells.items()))
        if key in seen:
            return
        seen.add(key)
        penalty = placement_penalty(cells, feedback)
        drawing = _channel_layout(
            circuit, labels, pins, flipped, reversed_parts, cells, local, tight=tight,
        )
        _mark_crossings(drawing)
        score = _layout_cost(drawing) + penalty
        candidates.append((score, order, columns, drawing, cells))
        return score

    def evaluate(order, columns):
        cells = source_cells(circuit, order, columns, runs, source_rails=True)
        evaluate_cells(cells, order, columns)

    for order in (source_order, *_orders(parts, pins, circuit.grounds)):
        for columns in widths:
            evaluate(order, columns)
    for _ in range(2):
        best = min(candidates, key=lambda candidate: candidate[0])
        _, order, columns, _, _ = best
        for trial in _placement_trials(circuit, pins, runs, order):
            evaluate(trial, columns)
        if min(candidates, key=lambda candidate: candidate[0]) is best:
            break

    initial = min(candidates, key=lambda candidate: candidate[0])[-1]
    for cells, tight in feedback_stage_cells(circuit, initial, units, feedback):
        evaluate_cells(cells, tight=tight)
    for cells in circuit_instance_cells(circuit, initial, units, feedback):
        evaluate_cells(cells, tight=True)

    seeds = {}
    for score, _, _, _, cells in sorted(candidates, key=lambda candidate: candidate[0]):
        seeds.setdefault(tuple(sorted(cells.items())), (score, cells))
        if len(seeds) == 6:
            break
    evolve_placements(seeds.values(), units, feedback, evaluate_cells)

    for _, _, _, drawing, cells in sorted(candidates, key=lambda candidate: candidate[0]):
        if preserves_nets(circuit, drawing):
            return _compact_channels(
                circuit, labels, pins, flipped, reversed_parts, cells, local,
                drawing, units, feedback,
            )
    return fast_layout(circuit, labels)


# Propose shorter tracks and nearby component moves, accepting only complete valid
# layouts rather than optimizing wire length at the expense of connectivity.
def _compact_channels(
    circuit, labels, pins, flipped, reversed_parts, cells, local,
    drawing, units, feedback,
):
    """Refine the winning placement with shorter routes and nearby component moves."""
    def cost(layout):
        return wire_length(layout), crossing_count(layout)

    def clear_symbols(layout):
        """Reject a shortcut through the interior of another symbol."""
        wires = [edge for segments in layout.wires.values() for edge in segments]
        for part, a, b, _ in layout.symbols:
            x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            half_height = 0.6 if part.kind == "K" else 0.44
            if part.kind in {"O", "Q", "P", "A"}:
                height = {"Q": BJT_PIN_OFFSET, "P": POT_PIN_OFFSET}.get(
                    part.kind, 1.9 if part.kind == "A" else 1.4 if part.vplus else 0.73
                )
                body = ((a[0] + 0.08, y - height), (b[0] - 0.08, y + height))
            elif a[0] == b[0]:
                body = ((x - half_height, min(a[1], b[1]) + 0.08),
                        (x + half_height, max(a[1], b[1]) - 0.08))
            else:
                body = ((min(a[0], b[0]) + 0.08, y - half_height),
                        (max(a[0], b[0]) - 0.08, y + half_height))
            if any(segments_intersect(body, edge) for edge in wires):
                return False
        return True

    best, best_cells = drawing, cells
    best_length, best_gaps = cost(drawing)
    gap_limit = best_gaps
    stage_limit = placement_penalty(cells, feedback)
    rotations = False
    offsets, flips = {}, set()

    def try_layout(trial, rotate, source_offsets=None, source_flips=()):
        nonlocal best, best_cells, best_length, best_gaps, rotations, offsets, flips
        if placement_penalty(trial, feedback) > stage_limit:
            return
        layout = _channel_layout(
            circuit, labels, pins, flipped, reversed_parts, trial, local,
            tight=True, rotate=rotate, source_offsets=source_offsets,
            source_flips=source_flips,
        )
        _mark_crossings(layout)
        length, gaps = cost(layout)
        if gaps <= gap_limit and (round(length, 7), gaps) < (
            round(best_length, 7), best_gaps
        ) and clear_symbols(layout) and preserves_nets(circuit, layout):
            best, best_cells = layout, trial
            best_length, best_gaps = length, gaps
            rotations = {
                i for i, (part, a, b, _) in enumerate(layout.symbols)
                if not part.is_source and a[0] == b[0]
            }
            offsets = source_offsets or {}
            flips = set(source_flips)

    try_layout(cells, False)
    try_layout(cells, True)
    parts = circuit.components
    anchors = circuit.grounds | {
        net for part in parts if part.is_source for net in (part.p, part.n)
    }
    shunts = [
        i for i, part in enumerate(parts)
        if part.kind in "RCLXZK" and {part.p, part.n} & anchors
    ]
    for i in shunts[:12]:
        try_layout(best_cells, (rotations or set()) ^ {i})

    if not units:
        return best
    first_row = min(cells[unit[0]][0] for unit in units)
    first_column = min(cells[unit[0]][1] for unit in units)
    last_column = max(col for _, col in cells.values())
    owners = defaultdict(set)
    for i, net, *_ in pins:
        if net not in circuit.grounds:
            owners[net].add(i)
    neighbors = defaultdict(set)
    for members in owners.values():
        for i in members:
            neighbors[i].update(members - {i})
    neighbors = {i: sorted(neighbors[i]) for i in range(len(parts))}

    def distance(unit, placement):
        return sum(
            abs(placement[i][0] - placement[j][0])
            + abs(placement[i][1] - placement[j][1])
            for i in unit for j in neighbors[i] if j not in unit
        )

    # Try the most stretched blocks first. A link can move to a free slot
    # near the two terminals it bridges instead of routing back to its old row.
    singles = {u[0] for u in units if len(u) == 1}
    for unit in sorted(units, key=lambda u: (-distance(u, best_cells), u))[:20]:
        occupied = {pos: i for i, pos in best_cells.items() if i not in unit}
        row, col = best_cells[unit[0]]
        adjacent = set().union(*(neighbors[i] for i in unit)) - set(unit)
        preferred_rows = {row - 1, row, row + 1}
        preferred_columns = {col - 1, col, col + 1}
        if len(unit) == 1:
            preferred_rows.update(best_cells[i][0] for i in adjacent)
            preferred_columns.update(best_cells[i][1] for i in adjacent)
        trials = []
        for r in sorted(preferred_rows):
            for c in sorted(preferred_columns):
                if (r, c) == (row, col):
                    continue
                positions = [(r, c + offset) for offset in range(len(unit))]
                if r < first_row or c < first_column or positions[-1][1] > last_column:
                    continue
                blockers = [occupied[pos] for pos in positions if pos in occupied]
                if blockers and (len(unit) != 1 or blockers[0] not in singles):
                    continue
                trial = best_cells | dict(zip(unit, positions))
                if blockers:
                    trial[blockers[0]] = row, col
                trials.append((distance(unit, trial), trial))
        trial_count = 7 if len(parts) <= 24 else 3
        for _, trial in sorted(trials, key=lambda candidate: candidate[0])[:trial_count]:
            try_layout(trial, rotations)
            if len(unit) == 1 and parts[unit[0]].kind in "RCLXZK":
                try_layout(trial, (rotations or set()) | {unit[0]})
                # Two vertical branches can compete for the same drop. Give
                # the newly rotated part a trial with the other one horizontal.
                if rotations and unit[0] in shunts:
                    try_layout(trial, {unit[0]})

    # Drop an isolated source in its own column toward the branch it feeds.
    # Each trial re-routes the rail from the new terminal height.
    sources = sorted(
        (i for i, part in enumerate(parts) if part.is_source),
        key=lambda i: best_cells[i],
    )
    for i in reversed(sources):
        part = parts[i]
        grounded = bool({part.p, part.n} & circuit.grounds)
        for offset in (0.35, 0.65):
            try_layout(best_cells, rotations, offsets | {i: offset}, flips)
        if part.kind == "V" and not grounded:
            try_layout(best_cells, rotations, offsets, flips ^ {i})
    # Change the vertical order of batteries as a unit, including both
    # terminals. Their rails still start at the source and run right.
    for a, b in zip(sources, sources[1:]):
        if best_cells[a][1] == best_cells[b][1]:
            trial = best_cells | {a: best_cells[b], b: best_cells[a]}
            try_layout(trial, rotations, offsets, flips)

    # Recheck the rail drops after moving sources and branches. A floating
    # return can use the open edge column without crossing a symbol below it.
    floating = {
        net for i in sources for net in (parts[i].p, parts[i].n)
        if parts[i].p not in circuit.grounds and parts[i].n not in circuit.grounds
    }
    for i in sorted(singles):
        part = parts[i]
        if part.kind not in "RCLXZK" or not {part.p, part.n} & floating:
            continue
        row, col = best_cells[i]
        occupied = {pos: j for j, pos in best_cells.items() if j != i}
        for r in (row, row + 1):
            trial = best_cells | {i: (r, last_column + 1)}
            blocker = occupied.get((r, last_column + 1))
            if blocker is not None:
                if blocker not in singles:
                    continue
                trial[blocker] = row, col
            try_layout(trial, (rotations or set()) | {i}, offsets, flips)
            try_layout(trial, {i}, offsets, flips)
    return _shorten_rail_branches(circuit, best, cost, clear_symbols)


def _shorten_rail_branches(circuit, drawing, cost, clear_symbols):
    """Place a passive branch directly between two source rails when it fits."""
    rails = {}
    for part, a, b, _ in drawing.symbols:
        if part.is_source:
            for net, pin in ((part.p, a), (part.n, b)):
                edges = drawing.wires.get(net)
                if edges and edges[0][0] == pin and edges[0][1][1] == pin[1]:
                    rails[net] = edges[0]

    def branch_edges(edges, pin, rail):
        """Walk an unshared drop from its terminal to the rail."""
        branch = []
        edges = edges[1:]
        while not segments_intersect((pin, pin), rail):
            touching = [edge for edge in edges if pin in edge and edge not in branch]
            if len(touching) != 1:
                return []
            edge = touching[0]
            branch.append(edge)
            pin = edge[edge[0] == pin]
        return branch

    def length(edges):
        return sum(math.dist(a, b) for a, b in edges)

    for index, (part, a, b, top) in enumerate(drawing.symbols):
        if part.kind not in "RCLXZK" or part.p == part.n or not {part.p, part.n} <= rails.keys():
            continue
        p_rail, n_rail = rails[part.p], rails[part.n]
        py, ny = p_rail[0][1], n_rail[0][1]
        left = max(p_rail[0][0], n_rail[0][0]) + 0.3
        right = min(p_rail[1][0], n_rail[1][0]) - 0.3
        if abs(py - ny) < 3.5 or left > right:
            continue
        p_edges = branch_edges(drawing.wires[part.p], a, p_rail)
        n_edges = branch_edges(drawing.wires[part.n], b, n_rail)
        if not p_edges or not n_edges:
            continue
        old_length = length(p_edges) + length(n_edges)
        if abs(py - ny) - 2.5 >= old_length - 1e-7:
            continue

        center = (a[0] + b[0]) / 2
        xs = sorted({left, right, *(x for wires in drawing.wires.values()
                      for edge in wires for x, _ in edge if left < x < right)})
        lanes = [min(right, max(left, center))]
        lanes.extend(sorted(((l + r) / 2 for l, r in zip(xs, xs[1:])),
                            key=lambda x: abs(x - center))[:8])
        old_gaps = cost(drawing)[1]
        y = (py + ny) / 2
        direction = 1 if py > ny else -1
        for x in lanes:
            new_a, new_b = (x, y + direction * 1.25), (x, y - direction * 1.25)
            clearance = ((x - 0.9, y - 1.9), (x + 0.9, y + 1.9))
            if any(
                segments_intersect(clearance, (start, end))
                for j, (_, start, end, _) in enumerate(drawing.symbols)
                if j != index
            ):
                continue
            trial = deepcopy(drawing)
            trial.symbols[index] = part, new_a, new_b, top
            trial.positions[part.name] = new_a
            for net, edges, pin, rail_y in (
                (part.p, p_edges, new_a, py), (part.n, n_edges, new_b, ny),
            ):
                wires = trial.wires[net]
                for edge in edges:
                    wires.remove(edge)
                tap = (x, rail_y)
                add_wire(trial, net, *sorted((pin, tap), key=lambda point: point[1]))
                trial.junctions[net] = [
                    dot for dot in trial.junctions.get(net, ())
                    if not any(segments_intersect((dot, dot), edge) for edge in edges)
                    or sum(segments_intersect((dot, dot), edge) for edge in wires) > 1
                ]
                trial.junctions[net].append(tap)
            trial.crossings.clear()
            _mark_crossings(trial)
            if cost(trial)[1] <= old_gaps and clear_symbols(trial) and preserves_nets(circuit, trial):
                drawing = trial
                break
    return drawing
