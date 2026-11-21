"""Conventional placement for a cross-coupled transistor oscillator."""

from collections import defaultdict
from dataclasses import dataclass

from circuitlab.layout.checks import mark_crossings
from circuitlab.layout.geometry import Layout, add_wire, ground_lead, place_symbol
from circuitlab.model.graph import Component


@dataclass(frozen=True)
class OscillatorHalf:
    transistor: Component
    collector_feed: Component
    base_feed: Component
    coupling: Component
    follower: tuple[Component, Component, Component] | tuple[()]
    monitor: tuple[Component, Component, str] | tuple[()]


def _oscillator(circuit):
    """Recognize the paired drivers and account for all supported attachments."""
    parts, grounds = circuit.components, circuit.grounds
    sources = [part for part in parts if part.is_source]
    if len(sources) != 1:
        return None
    source, = sources
    if source.kind != "V" or (source.p in grounds) == (source.n in grounds):
        return None
    rail = source.n if source.p in grounds else source.p
    transistors = [part for part in parts if part.kind in {"H", "Q"} and part.transistor_type == "npn"]
    core = [part for part in transistors if part.n in grounds]
    if len(core) != 2 or len(transistors) not in {2, 3, 4}:
        return None
    passives = defaultdict(list)
    for part in parts:
        if part.kind in {"R", "C"}:
            passives[part.kind, frozenset((part.p, part.n))].append(part)

    def select(kind, a, b):
        matches = passives[kind, frozenset((a, b))]
        return matches[0] if len(matches) == 1 else None

    def returns(kind, net):
        return [part for ground in grounds if (part := select(kind, net, ground))]

    used = {source, *core}
    halves = []
    for driver, other in (core, core[::-1]):
        collector = select("R", rail, driver.p)
        bias = select("R", rail, driver.base)
        coupling = select("C", driver.p, other.base)
        if not all((collector, bias, coupling)):
            return None
        used.update((collector, bias, coupling))
        followers = []
        for buffer in transistors:
            if buffer.p != rail or buffer.n in grounds:
                continue
            feed = select("R", driver.p, buffer.base)
            load = returns("R", buffer.n)
            if feed and len(load) == 1:
                followers.append((buffer, feed, load[0]))
        if len(followers) > 1 or any(part in used for group in followers for part in group):
            return None
        follower = followers[0] if followers else ()
        used.update(follower)
        monitors = []
        for cap in parts:
            if cap.kind != "C" or (cap.p in grounds) == (cap.n in grounds):
                continue
            monitor = cap.n if cap.p in grounds else cap.p
            feed = select("R", driver.p, monitor)
            if feed and feed not in used:
                monitors.append((feed, cap, monitor))
        if len(monitors) > 1:
            return None
        monitor = monitors[0] if monitors else ()
        used.update(monitor[:2])
        halves.append(OscillatorHalf(driver, collector, bias, coupling, follower, monitor))
    if used != set(parts) or all(half.monitor for half in halves):
        return None
    if halves[1].monitor:
        halves.reverse()
    return source, rail, *halves


def multivibrator_layout(circuit):
    """Place an astable pair, its local supply rail, and optional output filters."""
    if not (oscillator := _oscillator(circuit)):
        return None
    source, rail, left, right = oscillator
    first, second = left.transistor, right.transistor
    c1, c2 = first.p, second.p
    b1, b2 = first.base, second.base
    drawing = Layout(pin_grid=.25)
    supply_taps = []
    rail_y = 2.75

    def wire(net, *points):
        add_wire(drawing, net, *points)

    def passive(part, net, a, b):
        place_symbol(drawing, part, net, a, b)

    def transistor(part, x, y):
        # Package pin locations are base left and collector/emitter right.
        place_symbol(drawing, part, part.p, (x, y), (x + 6.5, y))
        return (x + 6.5, y + .75), (x + 6.5, y - .75)

    def junction(net, *points):
        drawing.junctions.setdefault(net, []).extend(points)

    def supply(point):
        x, _ = point
        wire(rail, point, (x, rail_y))
        supply_taps.append(x)

    def return_branch(part, net, start, end, *, turns=0):
        passive(part, net, start, end)
        ground_lead(drawing, end, part.n if part.p == net else part.p, turns=turns)

    def emitter_load(part, transistor, emitter):
        x, y = emitter
        start = x, y - .5
        wire(transistor.n, emitter, start)
        return_branch(part, transistor.n, start, (x, y - 2.5))

    collector1, emitter1 = transistor(first, 0, 0)
    top_y = 5.5
    collector2, emitter2 = transistor(second, 2, top_y)
    ground_lead(drawing, emitter1, first.n, turns=3, length=.25)
    ground_lead(drawing, emitter2, second.n, turns=3, length=.25)

    # Keep the shared supply between the two stages and the timing capacitors
    # near the receiving bases. Collector taps below and beside the drivers
    # also serve the adjacent output stages without following the rail.
    passive(source, rail, (-3, 5.25), (-3, 7.25))
    supply((-3, 5.25))
    ground_lead(drawing, (-3, 7.25), source.n if source.p == rail else source.p, turns=2)
    passive(left.collector_feed, rail, (-3, 1.75), (-3, -.25))
    supply((-3, 1.75))
    passive(left.base_feed, rail, (-1, 2.5), (-1, .5))
    supply((-1, 2.5))
    collector_y = collector2[1]
    passive(right.collector_feed, c2, (9.75, collector_y), (11.75, collector_y))
    supply((11.75, collector_y))
    passive(right.base_feed, b2, (1.25, top_y), (1.25, 3.5))
    supply((1.25, 3.5))

    passive(left.coupling, c1, (-2, top_y), (0, top_y))
    passive(right.coupling, c2, (-4.75, 1.5), (-4.75, 0))
    wire(b1, (-4.75, 0), (-4.75, -1), (-1, -1), (-1, 0), (0, 0))
    wire(b1, (-1, .5), (-1, 0))
    junction(b1, (-1, 0))
    wire(b2, (0, top_y), (2, top_y))
    junction(b2, (1.25, top_y))
    wire(c1, collector1, (7.5, .75), (7.5, -2), (-3, -2), (-3, -.25))
    wire(c1, (-2, -2), (-2, top_y))
    junction(c1, (-3, -2), (-2, -2))
    wire(c2, collector2, (9.75, collector_y))
    collector_tap = 9.5
    wire(c2, (collector_tap, collector_y), (collector_tap, 3.25), (-4.75, 3.25), (-4.75, 1.5))
    junction(c2, (collector_tap, collector_y))

    if left.follower:
        buffer, feed, load = left.follower
        collector, emitter = transistor(buffer, 0, -4)
        passive(feed, c1, (-3, -4), (-1, -4))
        wire(c1, (-3, -2), (-3, -4))
        wire(buffer.base, (-1, -4), (0, -4))
        wire(rail, collector, (8.5, collector[1]))
        supply((8.5, collector[1]))
        emitter_load(load, buffer, emitter)
    if right.follower:
        buffer, feed, load = right.follower
        collector, emitter = transistor(buffer, 12.5, 0)
        passive(feed, c2, (9.75, 0), (11.75, 0))
        wire(c2, (collector_tap, 3.25), (collector_tap, 0), (9.75, 0))
        junction(c2, (collector_tap, 3.25))
        wire(buffer.base, (11.75, 0), (12.5, 0))
        supply(collector)
        emitter_load(load, buffer, emitter)

    if left.monitor:
        feed, cap, net = left.monitor
        wire(c1, (-3, -2), (-5.5, -2), (-5.5, -5.5))
        passive(feed, c1, (-5.5, -5.5), (-3.5, -5.5))
        return_branch(cap, net, (-3.5, -5.5), (-2, -5.5), turns=3)
    wire(rail, (min(supply_taps), rail_y), (max(supply_taps), rail_y))
    junction(rail, *((x, rail_y) for x in sorted(set(supply_taps))[1:-1]))
    mark_crossings(drawing)
    return drawing
