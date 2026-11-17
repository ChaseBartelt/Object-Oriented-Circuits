"""Historical MATLAB wire placement retained for reference comparisons."""

import math
from collections import defaultdict
from statistics import median

from circuitlab.layout.geometry import Layout, component_pins, placement_order


def route(circuit, labels):
    """Reproduce the geometry used by the captured MATLAB parity tests."""
    parts = circuit.components
    layout = Layout()
    if not parts:
        return layout
    pins = component_pins(parts)
    columns = min(len(parts), max(1, math.ceil(math.sqrt(len(parts) * 0.75))))
    order = placement_order(parts, pins)
    cells = {i: divmod(j, columns) for j, i in enumerate(order)}
    pin_rows = [cells[i][0] for i, *_ in pins]
    gutters = [cells[i][1] + (not left) for i, _, left, _ in pins]
    gutter_pins = [[] for _ in range(columns + 1)]
    for i, gutter in enumerate(gutters):
        gutter_pins[gutter].append(i)
    lanes = {}
    widths = []
    for members in gutter_pins:
        indices = sorted(
            members,
            key=lambda i: (pins[i][2], pin_rows[i], pins[i][3]),
        )
        for j, i in enumerate(indices, 1):
            lanes[i] = 0.6 + j * 0.38
        widths.append(1.2 + len(indices) * 0.38)
    col_widths = [6.3] * columns
    for i, (_, column) in cells.items():
        col_widths[column] = max(
            col_widths[column], 0.66 * max(map(len, labels[parts[i].name])) + 0.8,
        )
    lefts, centers = [0], []
    for g in range(columns):
        centers.append(lefts[-1] + widths[g] + col_widths[g] / 2)
        lefts.append(lefts[-1] + widths[g] + col_widths[g])
    lane_x = [lefts[g] + lanes[i] for i, g in enumerate(gutters)]
    part_x = [centers[cells[i][1]] - 2.15 for i in range(len(parts))]
    pin_x = [part_x[i] + 4.3 * (not left) for i, _, left, _ in pins]
    nets = {}
    for i, (_, net, _, _) in enumerate(pins):
        nets.setdefault(net, []).append(i)
    direct = set()
    channels = defaultdict(list)
    for net, indices in nets.items():
        if net != "0" and len(indices) == 2:
            a, b = indices
            if (
                gutters[a] == gutters[b]
                and pin_rows[a] == pin_rows[b]
                and pins[a][3] == pins[b][3]
            ):
                direct.add(net)
        if net not in direct:
            channels[math.ceil(median(pin_rows[i] for i in indices))].append(net)
    y, track_y = [0], {}
    for row in range(max(pin_rows) + 1):
        tracks = sorted(
            channels[row],
            key=lambda net: (
                max(lane_x[i] for i in nets[net]) - min(lane_x[i] for i in nets[net])
            ),
        )
        for j, net in enumerate(tracks):
            track_y[net] = y[-1] - 1.55 - 0.88 * j
        y.append(y[-1] - 4.8 - 0.88 * len(tracks))
    pin_y = [y[pin_rows[i]] + pin[3] for i, pin in enumerate(pins)]
    for i, part in enumerate(parts):
        x, vertical = part_x[i], y[cells[i][0]]
        layout.positions[part.name] = (x, vertical)
        layout.symbols.append((part, (x, vertical), (x + 4.3, vertical), "+"))
    horizontal = []
    for net, indices in nets.items():
        if net in direct:
            xs = [pin_x[i] for i in indices]
            left, right, ty = min(xs), max(xs), pin_y[indices[0]]
            horizontal.append((net, left, right, ty))
            layout.labels[net] = ((left + right) / 2, ty)
            continue
        xs = [lane_x[i] for i in indices]
        left, right, ty = min(xs), max(xs), track_y[net]
        if net == "0":
            left = label = -0.85
        elif len(indices) == 1:
            right += 0.35
            label = right
        else:
            midpoint = (left + right) / 2
            label = min(
                (x for x in centers if left + 0.35 < x < right - 0.35),
                key=lambda x: abs(x - midpoint),
                default=midpoint,
            )
        layout.labels[net] = (label, ty)
        if net == "0":
            layout.grounds.append((label, ty))
        horizontal.append((net, left, right, ty))
        layout.junctions[net] = [(x, ty) for x in xs if left < x < right]
        for i in indices:
            horizontal.append(
                (net, min(pin_x[i], lane_x[i]), max(pin_x[i], lane_x[i]), pin_y[i])
            )
    for net, x1, x2, ty in horizontal:
        layout.wires.setdefault(net, []).append(((x1, ty), (x2, ty)))
    for net, indices in nets.items():
        if net in direct:
            continue
        for i in indices:
            x = lane_x[i]
            start, end = sorted((pin_y[i], track_y[net]))
            gaps = sorted(
                ty
                for _, left, right, ty in horizontal
                if left < x < right and start < ty < end
            )
            for crossing in gaps:
                low, high = max(start, crossing - 0.12), min(end, crossing + 0.12)
                if low > start:
                    layout.wires[net].append(((x, start), (x, low)))
                start = max(start, high)
            if end > start:
                layout.wires[net].append(((x, start), (x, end)))
    return layout
