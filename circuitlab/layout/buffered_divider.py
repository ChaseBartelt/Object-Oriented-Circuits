"""Place a potentiometer divider and its unity-gain buffer as one signal path."""

from math import ceil

from circuitlab.layout.geometry import (
    Layout, add_wire, ground_lead, inline_grounded_voltage, place_symbol, symbol_pins,
)


def buffered_divider_layout(circuit, labels):
    """Recognize one pot between a grounded source and an unpowered follower."""
    parts, grounds = circuit.components, circuit.grounds
    sources = [part for part in parts if part.kind == "V"]
    pots = [part for part in parts if part.kind == "P"]
    amps = [part for part in parts if part.kind == "O"]
    if len(sources) != 1 or len(pots) != 1 or len(amps) != 1:
        return None
    source, pot, amp = sources[0], pots[0], amps[0]
    supply, tap, output = pot.p, pot.wiper, amp.p
    if (
        len({supply, tap, output}) != 3 or grounds & {supply, tap, output}
        or pot.n not in grounds or amp.n not in grounds
        or amp.in_plus != tap or amp.in_minus != output or amp.vplus or amp.vminus
        or supply not in (source.p, source.n)
    ):
        return None
    source_return = source.n if source.p == supply else source.p
    if source_return not in grounds:
        return None

    loads = []
    for part in parts:
        if part in (source, pot, amp):
            continue
        return_net = part.n if part.p == output else part.p
        if part.kind not in {"R", "C", "L", "X", "Z"} or output not in (part.p, part.n) or return_net not in grounds:
            return None
        loads.append((part, return_net))

    drawing = Layout(pin_grid=.25)
    place_symbol(drawing, pot, supply, (0, 0), (4, 0))
    pot_pins = dict(symbol_pins(*drawing.symbols[-1], grid=drawing.pin_grid))
    inline_grounded_voltage(drawing, source, supply, source_return, pot_pins[supply], left=True)
    ground_lead(drawing, pot_pins[pot.n], pot.n, turns=1, length=.75)

    # Plus is on the wiper row. Bring the feedback to minus below the signal.
    place_symbol(drawing, amp, output, (6.5, -.25), (10.5, -.25))
    (_, plus), (_, minus), (_, drive) = symbol_pins(*drawing.symbols[-1], grid=drawing.pin_grid)
    add_wire(drawing, tap, pot_pins[tap], plus)
    tee = (11.25, drive[1])
    feedback_row = -2.25
    add_wire(drawing, output, drive, tee, (tee[0], feedback_row),
             (minus[0] - .5, feedback_row), (minus[0] - .5, minus[1]), minus)

    # A lone load follows the output; parallel loads each get a vertical drop.
    if len(loads) == 1:
        part, return_net = loads[0]
        contact, end = (tee[0] + .75, tee[1]), (tee[0] + 4.25, tee[1])
        add_wire(drawing, output, tee, contact)
        drawing.junctions.setdefault(output, []).append(tee)
        place_symbol(drawing, part, output, contact, end)
        ground_lead(drawing, end, return_net, turns=3, length=.75)
        return drawing

    longest_caption = max((len(line) for part, _ in loads for line in labels[part.name]), default=0)
    pitch = max(4.5, ceil(.8 * longest_caption) * .25)
    previous = tee
    for index, (part, return_net) in enumerate(loads):
        contact = (14 + index * pitch, drive[1])
        bottom = (contact[0], contact[1] - 2.5)
        add_wire(drawing, output, previous, contact)
        drawing.junctions.setdefault(output, []).append(previous)
        place_symbol(drawing, part, output, contact, bottom)
        ground_lead(drawing, bottom, return_net, length=.25)
        previous = contact
    return drawing
