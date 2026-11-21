"""Conventional layouts for recognizable circuit topologies."""
# Recognizable topologies get conventional arrangements before the general router is tried.
# A specialized drawing is useful only when it represents every terminal; unsupported shapes
# fall back to general routing rather than dropping branches or optional power connections.

from dataclasses import dataclass, replace
from statistics import median

from circuitlab.layout.checks import mark_crossings
from circuitlab.layout.geometry import (
    Layout, add_wire, component_pins, ground_at, ground_lead, input_source_indices,
    label_node, pin_offset, place_symbol,
)


def _label_pitch(parts, labels, minimum, margin, scale):
    """Leave room for captions, limiting the space reserved for part names."""
    # Without caption data, keep the caller's baseline spacing. With labels,
    # the dictionary is expected to contain a caption sequence for each part.
    if not labels:
        return minimum
    # Cap only the first caption line (the part name) at 24 characters for
    # spacing. Value/model lines retain their full length; this does not
    # truncate any displayed text. Convert that length to drawing-unit pitch.
    longest = max(
        min(len(line), 24) if i == 0 else len(line)
        for part in parts for i, line in enumerate(labels[part.name])
    )
    return max(minimum, margin + scale * longest)


def _passive_edges(parts, source):
    """Index distinct passive branches; reject loops and parallel duplicates."""
    # Use unordered endpoint pairs so orientation does not change branch
    # identity. Exclude the specific source object; all remaining parts must
    # be supported two-terminal passives or native manual switches (kind K).
    edges = {}
    for part in parts:
        if part is source:
            continue
        edge = frozenset((part.p, part.n))
        # Reject self-loops and duplicate endpoint pairs. These specialized views
        # need one unambiguous component for each required connection; a general
        # series/parallel drawing or router can handle other arrangements.
        if part.kind not in {"R", "C", "L", "X", "Z", "K"} or len(edge) != 2 or edge in edges:
            return None
        edges[edge] = part
    return edges


def _grounded_source(layout, source, supply, ground, start, end):
    # Put the supply at start and the return at end, allowing place_symbol()
    # to preserve p/n polarity even when the stored source faces the other way.
    # Keep the actual ground identity rather than assuming every return is 0.
    place_symbol(layout, source, supply, start, end)
    ground_at(layout, end, ground)
    layout.labels[supply] = start


def _connector(layout, net, start, end):
    # Add an outward stub and a connector record. Keep the full electrical net
    # key while replacing underscores with spaces only in the displayed text;
    # the two False flags request the normal rightward/above label style.
    add_wire(layout, net, start, end)
    layout.connectors.append((net, end, net.replace("_", " "), False, False))


# Keep a declared unbranched chain together without treating devices with extra terminals
# as reversible two-terminal passives.
def series_runs(circuit):
    """Keep declared series groups and unbranched graph paths together."""
    parts = circuit.components
    # Index physical terminal ownership, including extra pins on active parts.
    # This prevents a junction with a third contact from being inferred as an
    # unbranched series connection solely from p/n endpoints.
    incident = {}
    for i, net, *_ in component_pins(parts):
        incident.setdefault(net, []).append(i)
    # Only two-terminal parts can be reversed along a series run. Device
    # pin sides must agree with symbol_pins().
    passive = {
        i for i, part in enumerate(parts)
        if not part.is_source and part.kind not in {"O", "Q", "H", "S", "P", "A"} and part.p != part.n
    }
    # For each reversible part, map a joined terminal net to the next part.
    # Keep component indexes so these runs can later guide grid placement
    # without replacing or renaming any electrical components.
    joins = {i: {} for i in passive}

    # Record a symmetric neighbor relationship only if neither side already
    # has a neighbor at that terminal. A part can therefore continue through
    # each endpoint without branching within the proposed run.
    def join(a, b, net):
        if net not in joins[a] and net not in joins[b]:
            joins[a][net] = b
            joins[b][net] = a

    # A named tap can have a third connection and still belong to the
    # original series run. Use the compiled terminals to verify each join.
    # Match group entries to resolved components by name, falling back to
    # source order when compilation generated the final component identity.
    # Only members admitted to passive can become reversible run elements.
    by_name = {parts[i].name: i for i in passive}
    by_order = {parts[i].order: i for i in passive}
    sequences = {}
    # Visit nested groups before their enclosing groups so a parent series
    # sequence can reuse an already collected child series sequence.
    for index in reversed(range(len(circuit.groups))):
        group = circuit.groups[index]
        if group.mode != "series":
            continue
        sequence = []
        for position, (kind, name, value) in enumerate(group.entries):
            # Inline known child series sequences. None marks an unsupported entry
            # or a child of another shape, preventing a guessed join across it.
            # A potentiometer entry also stays out of the reversible two-terminal set.
            if kind == "G":
                sequence.extend(sequences.get(value, [None]))
            elif kind in "RCLXZPK":
                sequence.append(by_name.get(name, by_order.get(group.entry_order.get(position))))
            else:
                sequence.append(None)
        # Declared order is only a hint: adjacent members must actually share
        # exactly one non-ground terminal. This permits a named tap to branch
        # without losing the intended consecutive placement of its series members.
        for a, b in zip(sequence, sequence[1:]):
            if a is not None and b is not None and a != b:
                shared = {parts[a].p, parts[a].n} & {parts[b].p, parts[b].n}
                if len(shared) == 1 and not shared & circuit.grounds:
                    join(a, b, shared.pop())
        sequences[index] = sequence

    # Also infer runs from plain graph connectivity when a non-ground net
    # has exactly two contacts and both belong to reversible parts.
    for net, owners in incident.items():
        if net not in circuit.grounds and len(owners) == 2:
            a, b = owners
            if a in passive and b in passive:
                join(a, b, net)

    # A run begins at an open end. Leave closed rings to the general router.
    # Prefer runs starting away from ground and near a source/amp output.
    # Sort candidates by those preferences, then source order and index,
    # so otherwise equivalent placements remain deterministic.
    drivers = {
        net for part in parts if part.is_source or part.kind == "O"
        for net in ((part.p, part.n) if part.is_source else (part.p,))
    } - circuit.grounds
    ends = []
    for i, neighbors in joins.items():
        # A chain end has one joined terminal; its opposite terminal is the
        # incoming end for traversal. Isolated parts and closed rings do not
        # produce open-end candidates here.
        if len(neighbors) == 1:
            part = parts[i]
            net = part.n if part.p in neighbors else part.p
            ends.append((net in circuit.grounds, net not in drivers, part.order, i, net))

    runs, visited, reversed_parts = [], set(), set()
    # Walk each chain once. The opposite open end may also be in ends, but
    # visited prevents it from producing a second copy of the same run.
    for _, _, _, i, incoming in sorted(ends):
        run = []
        while i not in visited:
            run.append(i)
            visited.add(i)
            part = parts[i]
            # Record a display reversal when traversal enters through n. Leave the
            # actual component endpoints unchanged; callers use this set when placing
            # symbols and constructing their physical pin descriptors.
            if part.p != incoming:
                reversed_parts.add(i)
            outgoing = part.n if part.p == incoming else part.p
            if outgoing not in joins[i]:
                break
            i, incoming = joins[i][outgoing], outgoing
        if run:
            runs.append(run)
    return runs, reversed_parts


def source_cells(circuit, order, columns, runs=(), *, source_rails=False):
    """Stack the input sources in a left column, and place later sources by their nets."""
    parts = circuit.components
    # Work in component indexes and return grid cells, not physical coordinates.
    # Separate sources from other parts while preserving the supplied order.
    # A source-only circuit simply becomes one vertical stack of cells.
    sources = [i for i in order if parts[i].is_source]
    other = [i for i in order if not parts[i].is_source]
    if not other:
        return {i: (row, 0) for row, i in enumerate(sources)}

    # Classify sources using statement order: early inputs anchor the left
    # edge, while later bias/output sources should stay near their own stage.
    inputs = set(input_source_indices(parts))
    early = [i for i in sources if i in inputs]
    late = [i for i in sources if i not in inputs]
    # Rail-oriented placement reserves source rows before other components.
    # When both classes exist, late sources use the next source column;
    # clearing late prevents them from being placed a second time below.
    if source_rails:
        cells = {i: (row, int(bool(early) and i in late))
                 for row, i in enumerate(early + late)}
        start_column = 1 + max((col for _, col in cells.values()), default=-1)
        first_row = len(cells)
        late = []
    else:
        # The normal arrangement reserves only the input-source column. Other
        # parts may start in the first row to its right; late sources are deferred.
        cells = {i: (row, 0) for row, i in enumerate(early)}
        start_column = int(bool(early))
        first_row = 0
    # Widen enough to keep the longest run on one row, then limit width to
    # the number of nonsource/deferred-source cells that can be used.
    columns = min(max(columns, max(map(len, runs), default=0)), len(other) + len(late))
    # Encountering any member places its entire stored run consecutively.
    # Later encounters skip already assigned members, retaining chain order
    # even if a routing trial supplied a different general component order.
    by_member = {i: run for run in runs for i in run}
    row, column = first_row, 0
    for i in other:
        if i in cells:
            continue
        run = by_member.get(i, (i,))
        # Wrap before the whole run rather than splitting a series chain across
        # rows. start_column keeps ordinary components outside the source area.
        if column + len(run) > columns:
            row += 1
            column = 0
        for member in run:
            cells[member] = row, column + start_column
            column += 1
    occupied = set(cells.values())

    # Place each deferred source near nonground neighbors, including any
    # meaningful terminal on a multi-terminal neighbor. If none is connected,
    # use the nearest preceding component by statement order as a hint.
    for i in late:
        nets = {parts[i].p, parts[i].n} - circuit.grounds
        neighbors = [cells[j] for j in other if nets.intersection(parts[j].terminal_nets)]
        if not neighbors:
            preceding = [j for j in other if 0 < parts[j].order < parts[i].order]
            nearest = max(preceding, key=lambda j: parts[j].order) if preceding else other[0]
            neighbors = [cells[nearest]]
        # Use the median neighbor row/column as the target. Candidate cells are
        # unoccupied positions in the ordinary grid, with an extra row available
        # so a crowded layout still has somewhere to put the source.
        row = round(median(r for r, _ in neighbors))
        column = round(median(c for _, c in neighbors))
        candidates = (
            (r, c)
            for r in range(max(row, max(r for r, _ in occupied)) + 2)
            for c in range(start_column, columns + start_column)
            if (r, c) not in occupied
        )
        # Prefer column alignment first, then vertical proximity, then cell order.
        # This is deliberately not a single Manhattan-distance score. Reserve
        # the chosen cell before considering the next deferred source.
        cell = min(candidates, key=lambda cell: (abs(cell[1] - column), abs(cell[0] - row), cell))
        cells[i] = cell
        occupied.add(cell)
    return cells


# A Block is a layout decomposition retaining original component objects,
# not an equivalent electrical replacement. p/n describe its boundary;
# width/depth summarize horizontal span and downward extent for drawing.
@dataclass(frozen=True)
class Block:
    """A two-terminal component or series/parallel network for compact layout."""

    p: str
    n: str
    kind: str
    part: object = None
    children: tuple = ()
    width: float = 3.5
    depth: float = 0

    def oriented(self, start):
        # Callers supply one of the block endpoints as start. Reversing a series
        # block reverses child order as well as each child direction; a parallel
        # block keeps branch order and reverses each branch direction only.
        if self.p == start:
            return self
        children = reversed(self.children) if self.kind == "series" else self.children
        return replace(
            self, p=self.n, n=self.p,
            children=tuple(child.oriented(child.n) for child in children),
        )


def _combine(kind, children, p, n):
    # Flatten adjacent blocks of the same kind into one level. Orient parallel
    # children to the same p endpoint; series children have already been
    # oriented consecutively by the reduction step.
    flattened = []
    for child in children:
        if kind == "parallel":
            child = child.oriented(p)
        flattened.extend(child.children if child.kind == kind else (child,))
    # Parallel branches stack vertically, with a little horizontal rail margin.
    # Their depths plus interbranch gaps determine the total depth; series
    # children instead add widths and share the largest downward extent.
    if kind == "parallel":
        width = max(b.width for b in flattened) + 1.4
        depth = sum(b.depth for b in flattened) + 2 * (len(flattened) - 1)
    else:
        width = sum(b.width for b in flattened)
        depth = max(b.depth for b in flattened)
    return Block(p, n, kind, children=tuple(flattened), width=width, depth=depth)


# Recognize reducible connectivity first, then use the resulting tree to choose drawing
# structure; this is layout decomposition, not a change to the electrical graph.
def series_parallel(parts, terminals):
    """Reduce shared endpoints and degree-two junctions into a drawing tree."""
    # Empty networks and self-loops cannot form this two-terminal drawing tree.
    # The caller selects the component kinds appropriate to its topology.
    if not parts or any(e.p == e.n for e in parts):
        return None
    # Begin with one leaf per actual component. Repeatedly merge drawing blocks
    # while retaining every original leaf; no impedance or value is calculated.
    blocks = [Block(e.p, e.n, "part", part=e) for e in parts]
    while len(blocks) > 1:
        # Rebuild endpoint-pair and node-incidence indexes after each reduction.
        # Use unordered endpoint pairs so reversed branches still count as parallel.
        parallel, incident = {}, {}
        for i, block in enumerate(blocks):
            parallel.setdefault(frozenset((block.p, block.n)), []).append(i)
            for node in (block.p, block.n):
                incident.setdefault(node, []).append(i)
        # Prefer a parallel merge when available. Replace its first member and
        # delete later indexes in reverse order so list positions remain valid,
        # then restart with the reduced collection.
        for indices in parallel.values():
            if len(indices) > 1:
                i = indices[0]
                block = blocks[i]
                blocks[i] = _combine("parallel", [blocks[j] for j in indices], block.p, block.n)
                for j in reversed(indices[1:]):
                    del blocks[j]
                break
        else:
            # If no parallel pair exists, try a degree-two internal junction. Preserve
            # the requested boundary terminals even when they also have degree two.
            # Orient one block into the junction and the other out of it.
            for node, indices in incident.items():
                if node in terminals or len(indices) != 2:
                    continue
                i, j = indices
                first, second = blocks[i], blocks[j]
                first = first.oriented(first.n if first.p == node else first.p)
                second = second.oriented(node)
                # Do not merge a chain whose outer ends would close onto the same node.
                # If neither series nor parallel reduction is possible, return None
                # instead of forcing a bridge or another unsupported graph into a tree.
                if first.p != second.n:
                    blocks[i] = _combine("series", (first, second), first.p, second.n)
                    del blocks[j]
                    break
            else:
                return None
    # Success requires one block spanning exactly the two distinct requested
    # terminals. Orient it from the requested first terminal for placement.
    b = blocks[0]
    if terminals[0] != terminals[1] and set(terminals) == {b.p, b.n}:
        return b.oriented(terminals[0])
    return None


# Place nested series/parallel blocks recursively so their shared endpoints line up.
def place_tree(layout, circuit, block, x, y):
    # A leaf uses its oriented p endpoint on the left. place_symbol() retains
    # the original component p/n order in the stored symbol geometry.
    if block.kind == "part":
        place_symbol(layout, block.part, block.p, (x, y), (x + block.width, y))
    # Series children touch end-to-end without extra connecting leads.
    # label_node() marks intermediate nets according to the circuit's named
    # wire/group display rules.
    elif block.kind == "series":
        for i, child in enumerate(block.children):
            place_tree(layout, circuit, child, x, y)
            x += child.width
            if i + 1 < len(block.children):
                label_node(layout, circuit, (x, y), child.n)
    else:
        # Center each parallel child between common left/right rails. Move the
        # next branch below the previous child's full depth, leaving a fixed gap.
        top = y
        right = x + block.width
        for i, child in enumerate(block.children):
            left = x + (block.width - child.width) / 2
            add_wire(layout, block.p, (x, y), (left, y))
            place_tree(layout, circuit, child, left, y)
            add_wire(layout, block.n, (left + child.width, y), (right, y))
            if i + 1 < len(block.children):
                layout.junctions.setdefault(block.p, []).append((x, y))
                layout.junctions.setdefault(block.n, []).append((right, y))
                y -= child.depth + 2
        # Close the two side rails across all branch rows, retaining their distinct
        # net identities. Junction markers above identify the branch attachments.
        add_wire(layout, block.p, (x, top), (x, y))
        add_wire(layout, block.n, (right, top), (right, y))


def grounded_bridge(parts, source):
    """Draw a passive bridge with two legs and a link between their midpoints."""
    # This shape requires one source touching global 0 and exactly five other
    # branches: two divider legs plus a link between their midpoint nodes.
    # Named-return variants can use another strategy.
    if not source or len(parts) != 6 or (source.p == "0") == (source.n == "0"):
        return None
    supply = source.n if source.p == "0" else source.p
    branches = _passive_edges(parts, source)
    if branches is None:
        return None
    # Find the two nonground nodes directly below the supply. Unique undirected
    # edges were already checked, so the expected branch lookups are unambiguous.
    midpoints = [
        part.n if part.p == supply else part.p
        for edge, part in branches.items()
        if supply in edge and "0" not in edge
    ]
    if len(midpoints) != 2:
        return None
    left, right = midpoints
    left_x, right_x = 6.5, 14
    mid_y, ground_y = -3.6, -7.2
    # Specify every required connection and its conventional geometry: four
    # vertical leg elements and a horizontal midpoint link. Require all five
    # edges so a different fifth branch cannot masquerade as the bridge link.
    placements = (
        (supply, left, (left_x, 0), (left_x, mid_y)),
        (left, "0", (left_x, mid_y), (left_x, ground_y)),
        (supply, right, (right_x, 0), (right_x, mid_y)),
        (right, "0", (right_x, mid_y), (right_x, ground_y)),
        (left, right, (left_x, mid_y), (right_x, mid_y)),
    )
    if any(frozenset((a, b)) not in branches for a, b, _, _ in placements):
        return None

    # Draw only after recognition succeeds. Orient each component by its
    # assigned first net, then join the supply rail and mark both sensing nodes.
    layout = Layout()
    place_symbol(layout, source, supply, (0, 0), (0, ground_y))
    for a, b, start, end in placements:
        place_symbol(layout, branches[frozenset((a, b))], a, start, end)
    add_wire(layout, supply, (0, 0), (left_x, 0), (right_x, 0))
    layout.junctions[supply] = [(left_x, 0)]
    layout.labels[supply] = ((left_x + right_x) / 2, 0)
    for node, x in ((left, left_x), (right, right_x)):
        layout.junctions[node] = [(x, mid_y)]
        layout.labels[node] = (x, mid_y)
    # Use local global-ground symbols at the source and each leg instead of
    # drawing a long return bus across the bottom.
    for x in (0, left_x, right_x):
        ground_at(layout, (x, ground_y))
    return layout


def _parallel_tees(circuit, source, labels):
    """Draw T sections with a shared input and output, with or without a load."""
    # A parallel-T view needs exactly one source endpoint in the ground set.
    # Build a unique passive-edge map before looking for shared input/output
    # sections, preserving each section's own return net.
    if source is None or (source.p in circuit.grounds) == (source.n in circuit.grounds):
        return None
    supply, ground = (source.n, source.p) if source.p in circuit.grounds else (source.p, source.n)
    branches = _passive_edges(circuit.components, source)
    if branches is None:
        return None
    # Index undirected neighboring nets. Every accepted T has three branches
    # at its midpoint: from supply, toward shared output, and to one ground.
    neighbors = {}
    for part in branches.values():
        neighbors.setdefault(part.p, set()).add(part.n)
        neighbors.setdefault(part.n, set()).add(part.p)

    # Require at least two supply-fed midpoints, none itself grounded. This
    # keeps a lone divider or direct supply shunt out of the parallel-T path.
    taps = [
        part.n if part.p == supply else part.p
        for edge, part in branches.items() if supply in edge
    ]
    if len(taps) < 2 or circuit.grounds.intersection(taps):
        return None
    legs = []
    used = set()
    output = None
    # For each midpoint, require one return and one onward node apart from
    # the supply. All onward nodes must be the same output and must not be
    # another T midpoint.
    for tap in taps:
        returns = neighbors[tap] & circuit.grounds
        onward = neighbors[tap] - returns - {supply}
        if len(returns) != 1 or len(onward) != 1:
            return None
        (return_net,) = returns
        (next_net,) = onward
        if next_net in taps or (output is not None and next_net != output):
            return None
        output = next_net
        edges = [
            frozenset(pair)
            for pair in ((supply, tap), (tap, output), (tap, return_net))
        ]
        # Retain each leg as midpoint, return, input part, output part, and shunt.
        # Track used edges so no extra branch disappears from the specialized view.
        legs.append((tap, return_net, *(branches[edge] for edge in edges)))
        used.update(edges)
    # After all T legs, allow at most one additional branch, connecting output
    # to a declared ground. Any other remaining connection needs general routing.
    loads = branches.keys() - used
    if len(loads) > 1:
        return None
    load = next(iter(loads), None)
    if load is not None and (output not in load or not (load - {output}) & circuit.grounds):
        return None

    # Use caption-aware horizontal spacing and one row per T. Shared input
    # and output rails join those rows, with explicit T-junction markers.
    span = _label_pitch(branches.values(), labels, 6.5, 1, 0.40)
    left_x, tap_x = 3.0, 3.0 + span
    right_x = tap_x + span
    rows = [-5.8 * i for i in range(len(legs))]
    drawing = Layout()
    _grounded_source(drawing, source, supply, ground, (0, 0), (0, -3.3))
    add_wire(drawing, supply, (0, 0), *((left_x, y) for y in rows))
    add_wire(drawing, output, *((right_x, y) for y in rows))
    drawing.junctions[supply] = [(left_x, y) for y in rows[:-1]]
    drawing.junctions[output] = [(right_x, y) for y in rows[1:]]

    # Place each row's two horizontal series elements around its midpoint and
    # draw the shunt downward to its actual return. Midpoint captions stay
    # attached to the shared contact of those three components.
    for y, (tap, return_net, first, second, shunt) in zip(rows, legs):
        center = (tap_x, y)
        place_symbol(drawing, first, supply, (left_x, y), center)
        place_symbol(drawing, second, tap, center, (right_x, y))
        place_symbol(drawing, shunt, tap, center, (tap_x, y - 3.3))
        ground_at(drawing, (tap_x, y - 3.3), return_net)
        drawing.junctions[tap] = [center]
        drawing.labels[tap] = center

    # Expose output at the bottom of its rail. An optional load drops from
    # that same output node; the connector remains present with or without it.
    bottom = (right_x, rows[-1])
    terminal = (right_x + 3.0, rows[-1])
    _connector(drawing, output, bottom, terminal)
    if load is not None:
        (return_net,) = load - {output}
        end = (right_x, rows[-1] - 3.3)
        place_symbol(drawing, branches[load], output, bottom, end)
        ground_at(drawing, end, return_net)
    return drawing


# Count horizontal lanes needed when the tree is drawn vertically. Parallel
# children occupy separate lanes; successive series children can reuse the
# same space, so their widest child determines the required lane count.
def _branch_columns(block):
    if block.kind == "part":
        return 1
    spans = [_branch_columns(child) for child in block.children]
    return sum(spans) if block.kind == "parallel" else max(spans)


def _vertical_tree(layout, circuit, block, x, top, height, pitch):
    """Draw a series-parallel block downward, with local symbols at ground returns."""
    # The supplied height is the full downward span. Return the rightmost
    # contact on this block's TOP input rail, used for an output connector;
    # this return value is not the whole subtree's bounding-box extent.
    bottom = top - height
    if block.kind == "part":
        place_symbol(layout, block.part, block.p, (x, top), (x, bottom))
        # Grounded leaf returns get a local symbol at the lower contact. Reversed
        # original components are still oriented by the block's chosen first net.
        if block.n in circuit.grounds:
            ground_at(layout, (x, bottom), block.n)
        return x
    right = x
    # Apportion vertical height by child drawing widths, which act as weights.
    # Stack series children end-to-end and retain only the first child's top
    # rail extent as this block's input-rail extent.
    if block.kind == "series":
        for i, child in enumerate(block.children):
            child_height = height * child.width / block.width
            edge = _vertical_tree(layout, circuit, child, x, top, child_height, pitch)
            if i == 0:
                right = edge
            top -= child_height
            if i + 1 < len(block.children):
                label_node(layout, circuit, (x, top), child.n)
        return right

    # Parallel children share top/bottom levels and consume separate column
    # spans. Advance by each child's maximum lane requirement to leave room
    # for nested parallel sections inside that branch.
    branches = []
    for child in block.children:
        branches.append(x)
        right = _vertical_tree(layout, circuit, child, x, top, height, pitch)
        x += pitch * _branch_columns(child)
    # Join top contacts into the common input rail. Join lower contacts only
    # for nonground output nets; grounded branches already end at local symbols.
    add_wire(layout, block.p, *((x, top) for x in branches))
    layout.junctions.setdefault(block.p, []).extend((x, top) for x in branches[:-1])
    if block.n not in circuit.grounds:
        add_wire(layout, block.n, *((x, bottom) for x in branches))
        layout.junctions.setdefault(block.n, []).extend((x, bottom) for x in branches[:-1])
    return right


def _grounded_tree(circuit, source, tree, labels):
    # Orient the drawing from the source's selected supply toward its return.
    # Try the more specific named-divider-bank view before the general grounded
    # tree arrangement.
    ground, supply = (source.p, source.n) if source.p in circuit.grounds else (source.n, source.p)
    tree = tree.oriented(supply)
    if bank := _divider_bank(circuit, source, tree, ground):
        return bank
    # For an outer series tree, draw earlier children as a horizontal feed and
    # the last child as a vertical load. Other tree shapes become the load
    # directly, with no preceding feed.
    *feed, load = tree.children if tree.kind == "series" else (tree,)
    # Leave room for the vertical component captions to the left of each leg.
    pitch = _label_pitch(
        (part for part in circuit.components if part is not source), labels, 5.8, 1.4, 0.42
    )
    # Measure feed extent before placing the vertical load. A feed with its
    # own downward branches needs extra horizontal clearance beside the load.
    feed_depth = max((block.depth for block in feed), default=0)
    feed_width = sum(block.width for block in feed)
    layout = Layout()
    height = max(load.width, 3.6)
    _grounded_source(layout, source, supply, ground, (0, 0), (0, -height))
    # Keep a short lead before long feeds, while positioning short feeds so
    # their load can align with the normal caption-aware column pitch.
    x = max(1.0, pitch - feed_width) if feed else 0.0
    if feed:
        add_wire(layout, supply, (0, 0), (x, 0))
    for block in feed:
        place_tree(layout, circuit, block, x, 0)
        x += block.width
        if block is not feed[-1]:
            label_node(layout, circuit, (x, 0), block.n)
    # Move a vertical load farther right when needed to clear a deep feed,
    # then connect its top input and draw its complete nested structure.
    load_x = x + pitch if feed_depth else max(x, pitch)
    if x < load_x:
        add_wire(layout, load.p, (x, 0), (load_x, 0))
    right = _vertical_tree(layout, circuit, load, load_x, 0, height, pitch)
    # Expose a useful load-input node when there is a feed or parallel bank.
    # Suppress auto-generated @ names here; place the stub at the rightmost
    # top-rail contact returned by the vertical renderer.
    if not load.p.startswith("@") and (feed or load.kind == "parallel"):
        terminal = (right + 2.5, 0)
        dots = layout.junctions.setdefault(load.p, [])
        if (right, 0) not in dots:
            dots.append((right, 0))
        _connector(layout, load.p, (right, 0), terminal)
    return layout


def _divider_bank(circuit, source, tree, ground):
    # Recognize parallel divider chains made entirely of component leaves.
    # Each chain needs an explicitly named node just above its final shunt,
    # so that node can be presented as the divider's output.
    if tree.kind != "parallel" or any(
        branch.kind != "series"
        or any(child.kind != "part" for child in branch.children)
        or branch.children[-1].p not in circuit.named_wires
        for branch in tree.children
    ):
        return None

    drawing = Layout()
    supply = tree.p
    _grounded_source(drawing, source, supply, ground, (0, 0), (0, -3.6))
    # Put divider chains in separate fixed-pitch columns under one supply rail.
    # Their lower contacts use the vertical renderer's local return symbols.
    columns = [7.0 * (i + 1) for i in range(len(tree.children))]
    add_wire(drawing, supply, (0, 0), *((x, 0) for x in columns))
    drawing.junctions[supply] = [(x, 0) for x in columns[:-1]]
    for branch, x in zip(tree.children, columns):
        shunt = branch.children[-1]
        _vertical_tree(drawing, circuit, branch, x, 0, branch.width, 7.0)
        # The final shunt starts one shunt-width above the chain's bottom because
        # the chain height equals its total drawing width. Attach the output stub
        # to that exact named midpoint rather than to the supply rail.
        tap = (x, -branch.width + shunt.width)
        terminal = (x + 2.5, tap[1])
        _connector(drawing, shunt.p, tap, terminal)
    return drawing


# Return a specialized drawing only when the topology is fully supported, including every
# optional terminal; returning None lets the caller choose the general router.
def compact(circuit, labels=None):
    """Choose a conventional layout when the resolved topology supports one."""
    parts = circuit.components
    # Limit this compact-template path to small graphs and exclude transistor
    # or potentiometer geometry, whose extra terminals need another strategy.
    if len(parts) > 12 or any(part.kind in {"Q", "P", "A"} for part in parts):
        return None
    # Amplifier circuits use their own recognition path rather than entering
    # the passive series/parallel decomposition.
    if any(part.kind == "O" for part in parts):
        return compact_amplifier(circuit)
    # Use the first actual source as the candidate input. Later checks reject
    # other remaining sources, and source-order metadata must identify an
    # input source when the circuit has one.
    source = next((p for p in parts if p.is_source), None)
    passive = [p for p in parts if p is not source]
    if source and not input_source_indices(parts):
        return None
    # Try specific bridge and parallel-T forms before the generic reducible
    # tree. These shapes have conventional rails and midpoint arrangements
    # that a general tree decomposition would miss or display less clearly.
    if bridge := grounded_bridge(parts, source):
        return bridge
    if tees := _parallel_tees(circuit, source, labels):
        return tees
    # The source supplies the tree boundary; a source-free graph instead needs
    # compiled external terminals. A successful tree must cover every remaining
    # component and must not include another actual source.
    terminals = (source.p, source.n) if source else circuit.terminals
    if not terminals:
        return None
    tree = series_parallel(passive, terminals)
    if not tree or any(p.is_source for p in passive):
        return None
    # Recognized grounded passive trees use downward load branches with local
    # returns. Other supported two-terminal trees use the horizontal renderer.
    if source and set(terminals) & circuit.grounds and all(p.kind in "RCLXZK" for p in passive):
        return _grounded_tree(circuit, source, tree, labels)
    layout = Layout()
    place_tree(layout, circuit, tree, 1, 0)
    # Without a source, expose both boundary nodes on the tree itself.
    # With a source, close a return path beneath the entire horizontal tree.
    if not source:
        label_node(layout, circuit, (1, 0), terminals[0])
        label_node(layout, circuit, (tree.width + 1, 0), terminals[1])
    else:
        # Route the source return below the tree's deepest parallel branch and
        # around its right edge, leaving the supply connected at the tree's left.
        bottom, right = -tree.depth - 3.2, tree.width + 2
        add_wire(layout, source.p, (0, 0), (1, 0))
        add_wire(layout, source.n,
                 (tree.width + 1, 0), (right, 0), (right, bottom), (0, bottom))
        layout.positions[source.name] = (0, 0)
        layout.symbols.append((source, (0, 0), (0, bottom), "+"))
        label_node(layout, circuit, (0, 0), source.p)
        # Only literal node 0 gets the global-ground symbol on this generic loop.
        # Other returns retain a net label rather than being silently renamed 0.
        if source.n == "0":
            ground_at(layout, (right / 2, bottom))
        else:
            label_node(layout, circuit, (right / 2, bottom), source.n)
    return layout


# Collect branches reachable through internal nodes without traversing
# other terminal boundaries. This separates input/feedback subnetworks
# at their shared sensing/output nodes rather than crossing into each other.
def _paths(parts, start, terminals):
    visited = {start}
    terminals = set(terminals)
    while True:
        edges = [e for e in parts if e.p in visited or e.n in visited]
        # Keep the initial start visited even if it is a boundary. Newly reached
        # terminal endpoints remain stopping points, while internal nodes extend
        # the search until a fixed point is reached.
        reached = visited | ({node for e in edges for node in (e.p, e.n)} - terminals)
        if reached == visited:
            return edges
        visited = reached


def _powered_input(circuit, chip, sources, branches):
    """Find the one grounded source feeding the inverting input tree."""
    supplies = {}
    for source in sources:
        active, ground = (source.n, source.p) if source.p in circuit.grounds else (source.p, source.n)
        if ground not in circuit.grounds or active in circuit.grounds or active in supplies:
            return None
        supplies[active] = source, ground
    rails = {chip.vplus, chip.vminus}
    if len(rails) != 2 or not rails <= supplies.keys():
        return None
    if rails & {chip.in_minus, chip.in_plus, chip.p}:
        return None
    boundaries = supplies.keys() | circuit.grounds | {chip.in_minus, chip.p}
    candidates = []
    for net in supplies:
        if supplies.keys() - (rails | {net}):
            continue
        parts = _paths(branches, net, boundaries)
        if tree := series_parallel(parts, (net, chip.in_minus)):
            candidates.append((net, parts, tree))
    return (*candidates[0], supplies) if len(candidates) == 1 else None


def _draw_powered_sources(drawing, chip, supplies, feed, input_y, center):
    """Place local rails along the amp's supply-pin axes."""
    left = -1
    active = left, input_y
    # Put the input source on the opposite side of a shared rail's first bend.
    direction = 1 if feed == chip.vminus else -1
    ground_end = left, input_y + direction * 2.5
    source, ground = supplies[feed]
    place_symbol(drawing, source, feed, active, ground_end)
    ground_lead(drawing, ground_end, ground, turns=2 if direction > 0 else 0)
    add_wire(drawing, feed, active, (0, input_y))
    for net, side in ((chip.vplus, 1), (chip.vminus, -1)):
        terminal = center, side * pin_offset(1.25, drawing.pin_grid)
        if net == feed:
            rail_y = side * 1.75
            add_wire(drawing, net, active, (left, rail_y), (center, rail_y), terminal)
            drawing.junctions.setdefault(net, []).append(active)
        else:
            source, ground = supplies[net]
            start = center, terminal[1] + side * 0.5
            end = center, start[1] + side * 2.5
            add_wire(drawing, net, terminal, start)
            place_symbol(drawing, source, net, start, end)
            ground_lead(drawing, end, ground, turns=2 if side > 0 else 0)


# Place the input and feedback branches around one amplifier while retaining their
# actual shared sensing node rather than inventing a grounded input.
def compact_amplifier(circuit):
    parts = circuit.components
    # Identify actual voltage sources and ideal amplifiers.
    sources = [e for e in parts if e.kind == "V" and e.is_source]
    amps = [e for e in parts if e.kind == "O"]
    powered = any(amp.vplus for amp in amps)
    if powered and len(amps) != 1:
        return None
    # Several input sources around one amplifier may form a direct summing
    # topology. Otherwise this path expects one grounded inverting stage,
    # optionally followed by unity buffers.
    if not powered and len(sources) > 1 and len(amps) == 1:
        return _summing_amplifier(circuit, sources, amps[0])
    grounded = circuit.grounds if powered else {"0"}
    first = [e for e in amps if e.in_plus in grounded]
    if len(parts) < 4 or len(first) != 1 or (not powered and len(sources) != 1):
        return None
    if not powered and not input_source_indices(parts):
        return None
    chip = first[0]
    branches = [e for e in parts if e.kind not in "VO"]
    if any(e.kind not in "RCLXZK" for e in branches) or any(e.kind == "V" and e not in sources for e in parts):
        return None
    if powered:
        input_stage = _powered_input(circuit, chip, sources, branches)
        if input_stage is None:
            return None
        feed, inputs, input_tree, supplies = input_stage
    else:
        source = sources[0]
        if source.n != "0":
            return None
        feed = source.p
    # Protect the feed, sensing node, return, and each amplifier output as
    # separate boundaries while identifying the surrounding signal trees.
    terminals = [feed, chip.in_minus, chip.in_plus, *(e.p for e in amps)]
    if len(set(terminals)) != len(terminals):
        return None
    # Follow a unique chain of unity followers from the first output. Every
    # remaining amplifier must fit the next link; branching buffers or ambiguous
    # candidates require a different drawing strategy.
    chain = []
    previous = chip.p
    for _ in range(len(amps) - 1):
        candidates = [
            e for e in amps
            if e is not chip and e.in_plus == previous and e.in_minus == e.p
        ]
        if len(candidates) != 1:
            return None
        chain.append(candidates[0])
        previous = chain[-1].p
    # Only supported two-terminal passive/switch branches may surround the
    # amplifiers. Split them into source-to-sense and sense-to-first-output
    # subnetworks, then require both to reduce to series/parallel trees.
    if powered:
        terminals = set(terminals) | supplies.keys() | circuit.grounds
    else:
        inputs = _paths(branches, feed, terminals)
        input_tree = series_parallel(inputs, (feed, chip.in_minus))
    feedback = _paths([e for e in branches if e not in inputs], chip.in_minus, terminals)
    feedback_tree = series_parallel(feedback, (chip.in_minus, chip.p))
    if not input_tree or not feedback_tree:
        return None
    # Any leftover branches must form a terminal load at the final follower
    # output (or the first output when there are no followers). They cannot
    # touch earlier stage nodes except global ground.
    loads = [e for e in branches if e not in inputs and e not in feedback]
    load_tree = None
    if loads:
        stage_nodes = set(terminals) | {
            node for e in inputs + feedback for node in (e.p, e.n)
        }
        # Preserve first-seen node order when choosing a load boundary. Prefer
        # global ground if present; otherwise try other endpoints until the entire
        # load reduces to one tree spanning the final output and that endpoint.
        nodes = list(dict.fromkeys(node for e in loads for node in (e.p, e.n)))
        if set(nodes) & (stage_nodes - ({previous} | grounded)):
            return None
        returns = [n for n in nodes if n in grounded]
        for n in returns or [n for n in nodes if n != previous]:
            load_tree = series_parallel(loads, (previous, n))
            if load_tree:
                break
        if not load_tree:
            return None
    # After recognition, size the output clearance for both triangle and
    # feedback-tree width. Lift feedback above its own downward branches and
    # put source ground below the input tree's full downward extent.
    d = Layout(pin_grid=.25 if powered else 0)
    x = input_tree.width
    chip_x = x + 0.75
    chip_width = 4.5 if powered else 4.3
    out_x = max(chip_x + (5.0 if powered else 4.7), x + feedback_tree.width + (0.75 if powered else 0.8))
    feedback_clearance = (3.0 if feed == chip.vplus else 5.75) if powered else 1.75
    fy = feedback_clearance + feedback_tree.depth
    input_y = pin_offset(0.35, d.pin_grid)
    # The minus input is above center for this triangle orientation. Align
    # the input tree with that pin; the independent plus input is grounded
    # below the triangle. The sensing node itself remains a separate net.
    place_tree(d, circuit, input_tree, 0, input_y)
    if powered:
        _draw_powered_sources(d, chip, supplies, feed, input_y, chip_x + chip_width / 2)
    else:
        gy = -2.75 - input_tree.depth
        place_symbol(d, source, feed, (0, input_y), (0, gy))
    d.positions[chip.name] = (chip_x, 0)
    d.symbols.append((chip, (chip_x, 0), (chip_x + chip_width, 0), "-"))
    add_wire(d, chip.in_minus, (x, input_y), (chip_x, input_y))
    add_wire(d, chip.in_minus, (x, input_y), (x, fy))
    # Draw the full feedback network above the amplifier and return its far
    # end to the real output. A split marker at out_x also joins onward wiring.
    place_tree(d, circuit, feedback_tree, x, fy)
    add_wire(d, chip.p,
             (x + feedback_tree.width, fy), (out_x, fy), (out_x, 0), (chip_x + chip_width, 0))
    ground_lead(d, (chip_x, -input_y), chip.in_plus, turns=1)
    if not powered:
        ground_at(d, (0, gy))
    label_node(d, circuit, (0, input_y), feed)
    label_node(d, circuit, (x, input_y), chip.in_minus)
    d.junctions.setdefault(chip.p, []).append((out_x, 0))
    # Each follower takes the preceding output at its upper plus input and
    # returns its own output around the bottom to its lower minus input.
    # Advance chip/out_x so later followers and the load use the last output.
    for follower in chain:
        nx = out_x + 1.35
        add_wire(d, chip.p, (out_x, 0), (nx - 0.65, 0), (nx - 0.65, 0.35), (nx, 0.35))
        d.labels[chip.p] = (out_x + 0.4, 0.22)
        d.positions[follower.name] = (nx, 0)
        d.symbols.append((follower, (nx, 0), (nx + 4.3, 0), "+"))
        out_x = nx + 4.8
        add_wire(d, follower.p, (nx + 4.3, 0), (out_x, 0))
        add_wire(d, follower.p, (out_x, 0), (out_x, -1.35),
                 (nx - 0.55, -1.35), (nx - 0.55, -0.35), (nx, -0.35))
        d.junctions.setdefault(follower.p, []).append((out_x, 0))
        chip = follower
    # An unloaded final stage gets a short labeled output extension. Otherwise
    # put the complete load tree below and to the right of that final stage.
    if not load_tree:
        add_wire(d, chip.p, (out_x, 0), (out_x + (1 if powered else 0.9), 0))
        d.labels[chip.p] = (out_x + 1.05, 0)
    else:
        lx, ly = out_x + 0.75, -2.0
        add_wire(d, chip.p, (out_x, 0), (lx, 0), (lx, ly))
        d.labels[chip.p] = (lx, 0.27)
        place_tree(d, circuit, load_tree, lx, ly)
        end = lx + load_tree.width
        # Finish a grounded load with a local 0 symbol; retain a distinct named
        # endpoint as a labeled lead when the accepted load is not grounded.
        if load_tree.n in grounded:
            add_wire(d, load_tree.n, (end, ly), (end + 0.55, ly), (end + 0.55, ly - 0.85))
            ground_at(d, (end + 0.55, ly - 0.85), load_tree.n)
        else:
            add_wire(d, load_tree.n, (end, ly), (end + 0.65, ly))
            d.labels[load_tree.n] = (end + 0.8, ly)
    if powered:
        mark_crossings(d)
    return d


def _summing_amplifier(circuit, sources, amp):
    """Draw independent input legs around one inverting summing junction."""
    # Use the non-inverting input as the declared return. Distinct input
    # sources must share it, and the amplifier output return must agree.
    # The inverting tap is a sensing/summing net, not a ground declaration.
    ground, tap, output = amp.in_plus, amp.in_minus, amp.p
    inputs = {part.p for part in sources}
    # All sources must be eligible inputs with unique output nets, separate
    # from sense/output/ground. These checks allow a consistent named ground
    # as well as literal 0 for this template.
    if (
        len(input_source_indices(circuit.components)) != len(sources)
        or ground not in circuit.grounds
        or amp.n != ground
        or len(inputs) != len(sources)
        or len(inputs | {tap, output, ground}) != len(inputs) + 3
        or any(source.n != ground for source in sources)
    ):
        return None
    # Account for every remaining component as a supported passive/switch edge.
    # Reject loops and parallel duplicates so each input/feedback branch has
    # exactly one component for the conventional arrangement.
    passive = [part for part in circuit.components if part.kind in "RCLXZK"]
    if len(passive) + len(sources) + 1 != len(circuit.components):
        return None
    branches = _passive_edges(passive, None)
    if branches is None:
        return None
    # Require a direct edge from each input source to the sensing tap and one
    # feedback edge from tap to output. Only an optional output-to-ground load
    # may remain; coupled inputs or compound branches need another strategy.
    input_edges = [frozenset((source.p, tap)) for source in sources]
    feedback, load = frozenset((tap, output)), frozenset((output, ground))
    required = {*input_edges, feedback}
    if not required <= branches.keys() or branches.keys() - required - {load}:
        return None

    drawing = Layout()
    sum_x, amp_x, out_x = 4.25, 7.0, 12.0
    # Stack source/input legs down the left, joining their far ends on one
    # summing bus. Feedback sits above that bus and the amplifier stays right.
    rows = [-4.0 * i for i in range(len(sources))]
    for source, edge, y in zip(sources, input_edges, rows):
        _grounded_source(drawing, source, source.p, ground, (0, y), (0, y - 2.9))
        add_wire(drawing, source.p, (0, y), (0.75, y))
        place_symbol(drawing, branches[edge], source.p, (0.75, y), (sum_x, y))
    # Keep the shared tap as its own electrical net while routing it to the
    # upper minus pin. Ground only the lower plus pin using the actual return
    # identity; an ideal virtual-ground voltage does not make the tap a wire to it.
    add_wire(drawing, tap, (sum_x, 2.0), *((sum_x, y) for y in rows))
    drawing.junctions[tap] = [(sum_x, y) for y in rows[:-1]]
    drawing.labels[tap] = (sum_x + 1.0, 0)
    add_wire(drawing, tap, (sum_x, 0), (amp_x - 1.4, 0), (amp_x - 1.4, -0.65), (amp_x, -0.65))
    drawing.symbols.append((amp, (amp_x, -1), (amp_x + 4.3, -1), "-"))
    drawing.positions[amp.name] = (amp_x, -1)
    ground_lead(drawing, (amp_x, -1.35), ground, turns=1)
    # Close the top feedback branch onto the amplifier output and expose an
    # output connector at that same junction. Neither link changes tap identity.
    place_symbol(drawing, branches[feedback], tap, (sum_x, 2.0), (out_x, 2.0))
    end = (out_x + 1.5, -1)
    add_wire(drawing, output, (out_x, 2.0), (out_x, -1), (amp_x + 4.3, -1))
    drawing.junctions[output] = [(out_x, -1)]
    _connector(drawing, output, (out_x, -1), end)
    # When present, the one accepted output shunt drops to the common return.
    # All source, amplifier-input, and load ground records retain that net name.
    if load in branches:
        place_symbol(drawing, branches[load], output, (out_x, -1), (out_x, -4.0))
        ground_at(drawing, (out_x, -4.0), ground)
    return drawing
