"""Readable views of a resolved circuit graph."""
# Choose between labeled connections and readable buffered signal-flow drawings.
# Matching labels connect electrical islands intentionally; label allocation must avoid
# collisions between distinct nets that merely have similar display names.

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import groupby

from circuitlab.layout.geometry import Layout, add_wire, component_pins, grid_symbol, grid_terminal, ground_at, input_source_indices, placement_order
from circuitlab.layout.automatic import series_runs, source_cells


# Allocate readable labels once per electrical net; distinct nets must never receive the
# same connector label just because their shortened names collide.
def connector_names(nets):
    """Keep readable labels without collisions between distinct electrical nets."""
    # Map full electrical net identities to display-only connector names.
    # Reserve every original spelling without case sensitivity before choosing
    # aliases, including names that will themselves be shortened.
    names = {}
    reserved = Counter(net.casefold() for net in nets)
    alias = 0
    for net in nets:
        # Shorten scoped/internal names, long names, and case-folding collisions.
        # Friendly unique names stay readable as written. Electrical net keys
        # remain unchanged even when their displayed connector text is replaced.
        if (
            "." in net
            or net.startswith("@")
            or len(net) > 16
            or reserved[net.casefold()] > 1
        ):
            # Try increasing N numbers until one avoids all reserved original names.
            # The monotonically increasing counter also keeps generated aliases
            # distinct from one another without a separate generated-name set.
            while True:
                alias += 1
                short = f"N{alias}"
                if short.casefold() not in reserved:
                    break
            names[net] = short
        else:
            names[net] = net
    return names


# Use local ground symbols and named connector islands to avoid long continuous wires.
# The labels encode intentional connectivity that the saved-layout checker also understands.
# Provide a general view with short terminal stubs and named connections.
# labels supplies component caption text for spacing; layout.labels below
# stores net-caption positions. Connector records separately carry the
# matching names that intentionally reconnect otherwise separate islands.
def labeled(circuit, labels):
    """Use local ground symbols and matching names to connect distant pins."""
    parts = circuit.components
    # Return an empty drawing for an empty graph. This routine constructs
    # layout metadata and geometry; it does not change circuit connections.
    layout = Layout()
    if not parts:
        return layout
    # Keep recognized series chains together and orient reversible passives
    # consistently with those chains. Pin descriptors include component index,
    # net identity, a side hint, and vertical offset for multi-terminal parts.
    runs, reversed_parts = series_runs(circuit)
    pins = component_pins(parts, reversed_parts=reversed_parts)
    # Choose a roughly square component grid, then use connectivity/source
    # order to place related parts nearby. source_cells() keeps input sources
    # and complete series runs in the appropriate cells.
    columns = math.ceil(math.sqrt(len(parts) * 0.75))
    order = placement_order(
        parts,
        pins,
        circuit.grounds,
        sorted(input_source_indices(parts), key=lambda i: parts[i].order),
    )
    cells = source_cells(circuit, order, columns, runs)
    # Collect all terminal descriptors by exact electrical net. One allocated
    # connector name is reused for every displayed island of that same net.
    nets = defaultdict(list)
    for pin in pins:
        nets[pin[1]].append(pin)
    names = connector_names(nets)

    # Allow space for both component captions and the longest connector text.
    # Vertical pitch also accommodates the largest pin offset. The caller
    # supplies component captions for this nonempty circuit.
    label_width = max(len(line) for lines in labels.values() for line in lines)
    pitch = max(11, 6.1 + max(map(len, names.values()), default=0) * 0.7, label_width * 0.55 + 1)
    row_pitch = max(5.6, max((abs(p[3]) for p in pins), default=0) * 2 + 3)
    # Package symbols have larger bodies/pin labels, so add room in both
    # directions rather than using the ordinary two-terminal grid spacing.
    if any(part.kind == "H" for part in parts):
        pitch += 2.5
        row_pitch += 0.7
    # Convert each cell to drawing coordinates, with later rows below earlier
    # ones because positive y points upward. Keep symbols in component order
    # so a pin's stored component index still selects its placed symbol.
    for i, part in enumerate(parts):
        row, col = cells[i]
        symbol = grid_symbol(
            part, col * pitch, -row * row_pitch, circuit.grounds, reverse=i in reversed_parts
        )
        # Record the symbol's first stored endpoint as its placement anchor.
        # grid_symbol() also handles source orientation and reversed passives.
        layout.positions[part.name] = symbol[1]
        layout.symbols.append(symbol)

    # Translate a pin descriptor to its actual contact coordinate. Sources
    # are vertical and use their stored endpoints; other parts use the shared
    # resolver, including amplifier supply contacts and package pin offsets.
    def terminal(pin):
        i, _, left, offset = pin
        if parts[i].is_source:
            return layout.symbols[i][1 if left else 2]
        _, a, b, _ = layout.symbols[i]
        return grid_terminal(parts[i], a, b, left, offset)

    for net, group in nets.items():
        grounded = net in circuit.grounds
        # A two-pin non-ground net can use a direct link when its components are
        # side-by-side in the same row. All other connectivity uses local stubs
        # with matching connector names or explicit local ground symbols.
        if len(group) == 2 and not grounded:
            first, second = sorted(group, key=lambda p: cells[p[0]])
            r1, c1 = cells[first[0]]
            r2, c2 = cells[second[0]]
            # Require neighboring cells with facing pins at equal offsets. Exclude
            # sources because their vertical geometry does not follow this horizontal
            # pin-side shortcut, and avoid connecting diagonally across rows.
            if (
                r1 == r2
                and c2 == c1 + 1
                and not first[2]
                and second[2]
                and first[3] == second[3]
                and not parts[first[0]].is_source
                and not parts[second[0]].is_source
            ):
                a, b = terminal(first), terminal(second)
                add_wire(layout, net, a, b)
                # Caption a direct link at its midpoint; it needs no connector pair
                # because the visible wire already joins both physical contacts.
                layout.labels[net] = ((a[0] + b[0]) / 2, a[1])
                continue
        for pin in group:
            a = terminal(pin)
            part = parts[pin[0]]
            # Pin side controls stub direction for ordinary parts. Source stubs use
            # their vertical endpoint geometry instead of the horizontal side hint.
            left = pin[2] and not part.is_source
            # Put a lower source return just beneath the source. A grounded upper
            # terminal instead gets a short side lead so the ground symbol does not
            # sit inside the source body.
            if grounded and part.is_source:
                lower = min(point[1] for point in layout.symbols[pin[0]][1:3])
                b = (a[0], a[1] - 0.3) if a[1] == lower else (a[0] + 0.72, a[1])
            else:
                b = (a[0] + (-0.72 if left else 0.72), a[1])
                # Move an upper left ground farther outward to keep its symbol clear
                # of the other input's approach to a multi-terminal component.
                if grounded and left and pin[3] > 0:
                    # Keep the ground symbol outside the other input's approach.
                    b = (b[0] - 0.95, b[1])
            add_wire(layout, net, a, b)
            # Ground records retain the exact return-net name, including named grounds.
            # Other stubs receive the shared connector text; left/below are label
            # placement hints, not changes to electrical identity.
            if grounded:
                ground_at(layout, b, net)
            else:
                # Lower-offset contacts normally put their label below the stub. Package
                # pins use their own regular row arrangement instead of this adjustment.
                below = pin[3] < 0 and part.kind != "H"
                layout.connectors.append((net, b, names[net], left, below))
    return layout


# A recognized stage is a drawing description referencing existing circuit
# components. amp is the unity-gain buffer, feed is its incoming series
# element, shunts holds (return_net, component) branches, and input names
# the upstream driver net rather than the amplifier's local input tap.
@dataclass(frozen=True)
class Stage:
    """One buffered stage with its input component and shunt branches."""

    amp: object
    feed: object
    shunts: tuple
    input: str


# Recognize buffered stages only when all required terminals can be represented; otherwise
# return None and leave the drawing to a more general layout strategy.
def signal_flow(circuit):
    """Draw trees of buffered stages with one feed and parallel shunts."""
    parts = circuit.components
    # This specialized view has no supply-rail routing. If an amplifier declares
    # a positive supply contact, decline so a general strategy can retain the
    # explicit power pins. None means unsupported topology, not an empty graph.
    if any(part.kind == "A" or (part.kind == "O" and part.vplus) for part in parts):
        return None
    # The supported tree begins at exactly one voltage source with its return
    # in the declared ground set, and contains at least one ideal amplifier.
    # Any other components must also be accounted for by the checks below.
    sources = [p for p in parts if p.kind == "V" and p.n in circuit.grounds]
    amps = [p for p in parts if p.kind == "O"]
    if len(sources) != 1 or not amps:
        return None
    source = sources[0]
    # Honor available nonzero statement-order metadata: a later source should
    # not be promoted to the input of an earlier stage just for this drawing.
    if any(
        part.order and source.order > part.order for part in parts if part is not source
    ):
        return None
    # Drivers are the source output and buffered outputs; taps are distinct
    # non-inverting amplifier inputs. Each stage will connect one driver to
    # one tap through a feed element, with shunts from that tap to ground.
    ground = source.n
    drivers = {source.p, *(p.p for p in amps)}
    taps = {p.in_plus for p in amps}
    # Require unique outputs/taps, separate them from grounds and one another,
    # and accept only unity followers whose inverting input is their output.
    # Amplifier output returns must already resolve to declared ground nets.
    if (
        len(drivers) != len(amps) + 1
        or len(taps) != len(amps)
        or circuit.grounds & (drivers | taps)
        or drivers & taps
        or any(p.in_minus != p.p or p.n not in circuit.grounds for p in amps)
    ):
        return None

    # Build undirected adjacency for supported two-terminal branch elements.
    # K is the native manual switch. Ignore self-loops here; any unaccounted
    # device or branch will make the later full-coverage test fail.
    by_node = defaultdict(list)
    for part in parts:
        if part.kind in {"R", "C", "L", "X", "K"} and part.p != part.n:
            by_node[part.p].append((part.n, part))
            by_node[part.n].append((part.p, part))

    # children maps each upstream driver to its outgoing buffered stages.
    # Track consumed component objects so recognizing one stage cannot hide
    # an extra load or an unsupported device elsewhere in the circuit.
    children = defaultdict(list)
    used = {source, *amps}
    for amp in amps:
        # Inspect all supported branches touching this input tap. Every accepted
        # tap has exactly one feed from a driver plus one or more shunts whose
        # far ends are declared grounds; other tap connections need general routing.
        connections = by_node[amp.in_plus]
        shunts = tuple(
            (node, part) for node, part in connections if node in circuit.grounds
        )
        feeds = [(node, part) for node, part in connections if node in drivers]
        if not shunts or len(feeds) != 1 or len(connections) != len(shunts) + 1:
            return None
        # Record the stage under its upstream driver and mark its series/shunt
        # parts consumed. Preserve each shunt's own return net so different named
        # grounds are never combined just because their symbols look alike.
        incoming, feed = feeds[0]
        used.add(feed)
        used.update(part for _, part in shunts)
        children[incoming].append(Stage(amp, feed, shunts, incoming))

    # Each load is a shunt or a series element followed by a shunt to ground.
    # Remaining branches may form one terminal load per driver: either a
    # direct shunt or a single series element followed by a grounded shunt.
    # Store (optional_feed, shunt, load_node, return_net) for final drawing.
    loads = {}
    for node in [source.p, *(amp.p for amp in amps)]:
        # Exclude components already used by buffered stages. More than one
        # remaining branch at this driver is outside the supported load shape.
        remaining = [(other, part) for other, part in by_node[node] if part not in used]
        if not remaining:
            continue
        if len(remaining) != 1:
            return None
        other, part = remaining[0]
        # A direct grounded load uses the driver itself as its load node and
        # requires no additional horizontal series component.
        if other in circuit.grounds:
            loads[node] = (None, part, node, other)
            used.add(part)
        # For a two-element load, the intermediate node must be private to this
        # load rather than another driver/tap, and its only other branch must
        # reach a declared ground. Keep that return identity in the load record.
        elif other not in drivers | taps:
            tail = [(net, p) for net, p in by_node[other] if p != part]
            if len(tail) != 1 or tail[0][0] not in circuit.grounds:
                return None
            return_net, shunt = tail[0]
            loads[node] = (part, shunt, other, return_net)
            used.update((part, shunt))
    # Accept only complete coverage of the original component set. A loaded
    # driver must also be a leaf in this specialized view; drawing a load
    # and downstream stages on the same driver needs a more general strategy.
    if used != set(parts) or any(children[node] for node in loads):
        return None

    # Assign horizontal depth and vertical subtree position only after the
    # topology is recognized. Each leaf consumes one row; parent drivers
    # are centered over the span of leaves in their descendant subtree.
    coordinates = {}
    leaf_rows = 0

    def place(node, depth):
        nonlocal leaf_rows
        start = leaf_rows
        for stage in children[node]:
            place(stage.amp.p, depth + 1)
        # If recursive children consumed no rows, this driver is a leaf.
        # The coordinate formula uses 3.4 as half the leaf pitch: consecutive
        # leaf centers are 6.8 units apart, with internal nodes at their midpoint.
        if leaf_rows == start:
            leaf_rows += 1
        coordinates[node] = (depth, -(start + leaf_rows - 1) * 3.4)

    # A cycle cannot be reachable from this root because every amp has one feed.
    place(source.p, 0)
    # Every recognized driver must be reachable from the source. This also
    # rejects disconnected stage islands/cycles that were not visited by place().
    if set(coordinates) != drivers:
        return None

    # Begin drawing only after recognition succeeds. Use one global stage
    # pitch sized for the widest shunt bank so corresponding depth columns
    # line up across different branches of the tree.
    layout = Layout()
    shunt_spacing = 3.5
    stage_pitch = 11.25 + shunt_spacing * (
        max(len(stage.shunts) for group in children.values() for stage in group) - 1
    )

    # Depth one begins just right of the input source; each additional
    # buffered stage advances by the same pitch.
    def feed_x(depth):
        return 1.75 + (depth - 1) * stage_pitch

    # Preserve a reversed two-terminal part's p/n orientation when drawing it
    # from the chosen incoming net. Store the first endpoint as its anchor;
    # top also controls amplifier input-sign arrangement in symbol geometry.
    def symbol(part, positive, a, b, top="+"):
        if part.p != positive:
            a, b = b, a
        layout.symbols.append((part, a, b, top))
        layout.positions[part.name] = a

    # Place a vertical component with its named signal node at the top and
    # attach a local ground record at the lower contact, retaining the actual
    # return net. The same helper also draws the input voltage source.
    def shunt(part, node, x, y, return_net):
        symbol(part, node, (x, y), (x, y - 2.75))
        ground_at(layout, (x, y - 2.75), return_net)

    # Show friendly external net names, leaving scoped/internal names quiet.
    # These are caption anchors, not the matching connectors used by labeled().
    def caption(net, anchor):
        if "." not in net and not net.startswith("@"):
            layout.labels[net] = anchor

    source_y = coordinates[source.p][1]
    # For source fan-out, raise the source above its highest immediate child
    # instead of placing it at the subtree midpoint, leaving a clear rail
    # to feed the separate branches.
    if len(children[source.p]) > 1:
        source_y = max(coordinates[stage.amp.p][1] for stage in children[source.p]) + 2.75
    shunt(source, source.p, 0, source_y, ground)
    caption(source.p, (0, source_y))
    # Remember the physical outgoing contact of every driver. Stage bodies
    # are placed first; a separate pass will connect those contacts to child
    # feeds, terminal loads, or short labeled output extensions.
    outputs = {source.p: (0, source_y)}
    # Draw each stage from its assigned depth/row: horizontal feed, a bank
    # of vertical shunts, then the unity follower and its feedback loop.
    for group in children.values():
        for stage in group:
            amp = stage.amp
            depth, y = coordinates[amp.p]
            x = feed_x(depth)
            tap = amp.in_plus
            first = x + 3.5
            # Orient the feed from upstream driver to local tap, independent of the
            # component's stored p/n order. The tap begins at the first shunt column.
            symbol(stage.feed, stage.input, (x, y), (first, y))
            ground_y = y - 2.75
            # Space shunt branches along the tap rail. Their lower contacts share
            # a y coordinate, but only branches with the same return identity may
            # have their lower contacts joined by a visible ground rail.
            positions = [first + i * shunt_spacing for i in range(len(stage.shunts))]
            for (_, part), xx in zip(stage.shunts, positions):
                symbol(part, tap, (xx, y), (xx, ground_y))
            # groupby joins consecutive runs of equal return nets, not all branches
            # indiscriminately. Nonadjacent runs may use separate ground symbols
            # with the same name; different named grounds stay on separate rails.
            for return_net, branches in groupby(
                zip(stage.shunts, positions), key=lambda branch: branch[0][0]
            ):
                rail = [(xx, ground_y) for _, xx in branches]
                if len(rail) > 1:
                    add_wire(layout, return_net, *rail)
                # One ground symbol terminates each return run. Interior lower-rail
                # contacts get junction markers; the ground helper initializes the
                # return net's junction list before it is extended.
                ground_at(layout, rail[0], return_net)
                layout.junctions[return_net].extend(rail[1:-1])
            # Join all upper shunt contacts because they share the same tap. Mark
            # their branch points explicitly so the renderer can show connectivity.
            if len(positions) > 1:
                add_wire(layout, tap, *((xx, y) for xx in positions))
            layout.junctions.setdefault(tap, []).extend((xx, y) for xx in positions)
            # Place the amplifier immediately after the last shunt. Approach its
            # non-inverting input below the centerline, keeping the upper input
            # available for the output-feedback return.
            last = positions[-1]
            caption(tap, (first, y))
            add_wire(
                layout,
                tap,
                (last, y),
                (last + 0.5, y),
                (last + 0.5, y - 0.35),
                (last + 1.25, y - 0.35),
            )
            # The minus sign belongs on the upper input here: route output feedback
            # above the triangle to that input, while the tap reaches the lower plus
            # input. This depicts the follower topology already checked above.
            symbol(amp, amp.p, (last + 1.25, y), (last + 5.55, y), "-")
            output = (last + 6.2, y)
            add_wire(
                layout,
                amp.p,
                (last + 5.55, y),
                output,
                (output[0], y + 1.8),
                (last + 0.75, y + 1.8),
                (last + 0.75, y + 0.35),
                (last + 1.25, y + 0.35),
            )
            # Mark the output split between feedback and onward signal wiring, and
            # retain its endpoint for the later driver-connection pass.
            layout.junctions.setdefault(amp.p, []).append(output)
            outputs[amp.p] = output

    # Complete each driver's outgoing wiring after every stage position is
    # known. Its continuation is either child stages, an accepted terminal
    # load, or a short open-ended output lead with a caption.
    for node, (x, y) in outputs.items():
        next_stages = children[node]
        # Use a short horizontal lead to a vertical fan-out rail. Include the
        # driver's own row as well as child rows, removing duplicate y positions
        # so aligned stages do not create redundant rail subdivisions.
        if next_stages:
            rail = x + 0.85
            ends = sorted({y, *(coordinates[s.amp.p][1] for s in next_stages)})
            add_wire(layout, node, (x, y), (rail, y))
            add_wire(layout, node, *((rail, yy) for yy in ends))
            # Branch horizontally from that rail to each child feed's left contact.
            # The feed component itself was already placed in the previous pass.
            for stage in next_stages:
                depth, yy = coordinates[stage.amp.p]
                add_wire(layout, node, (rail, yy), (feed_x(depth), yy))
            # An interior intersection is a T. At a rail end the wires bend.
            layout.junctions.setdefault(node, []).extend(
                (rail, yy) for yy in ends[1:-1]
            )
            # There is also a T at a rail endpoint when the source/output arrives
            # from the left and an aligned child continues right while the rail
            # continues vertically. Mark that endpoint explicitly as well.
            if (
                len(next_stages) > 1
                and any(coordinates[s.amp.p][1] == y for s in next_stages)
                and y in {ends[0], ends[-1]}
            ):
                layout.junctions.setdefault(node, []).append((rail, y))
            if node != source.p:
                caption(node, (rail, y))
        # Draw a recognized leaf load, preserving the upstream net across any
        # series element and using the recorded intermediate/load node for the
        # vertical shunt and its output caption.
        elif node in loads:
            feed, load, output, return_net = loads[node]
            # An optional series load element extends the driver before its grounded
            # shunt. Move the working x coordinate to that component's far contact.
            if feed:
                add_wire(layout, node, (x, y), (x + 1.25, y))
                caption(node, (x + 0.45, y))
                symbol(feed, node, (x + 1.25, y), (x + 4.75, y))
                x += 4.75
            # The shunt meets the signal lead at a marked junction. A short rightward
            # stub gives the load node a readable caption without another connector.
            shunt(load, output, x, y, return_net)
            add_wire(layout, output, (x, y), (x + 1, y))
            layout.junctions.setdefault(output, []).append((x, y))
            caption(output, (x + 1, y))
        # A leaf with no load still gets a visible output extension and caption.
        # It remains the same driver net; this adds no electrical component.
        else:
            add_wire(layout, node, (x, y), (x + 1.5, y))
            caption(node, (x + 1.5, y))
    return layout
