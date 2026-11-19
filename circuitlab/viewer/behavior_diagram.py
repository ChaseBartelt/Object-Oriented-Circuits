"""Compact functional diagrams derived from compiled digital signal metadata."""
# Functional blocks come from compiled signal dependencies rather than parsing source again.
# Group compatible output/display bits for readability, condense dependency cycles for layout,
# and retain the exact live signal identities behind every painted block and arrow.

from collections import defaultdict
from dataclasses import dataclass
import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsObject

from circuitlab.layout.logic import LogicGeometry


WIDTH, HEIGHT, COLUMN, ROW = 176, 62, 246, 84
MAX_NODES, MAX_EDGES = 120, 400


# Collapse strongly connected dependencies before assigning columns so feedback does not
# make a topological layering pass loop forever.
def _layers(names, edges):
    """Condense feedback cycles before assigning dependency columns."""
    order = {name: index for index, name in enumerate(names)}
    children = defaultdict(list)
    for source, target in edges:
        children[source].append(target)
    index, low, stack, active, groups = {}, {}, [], set(), []

    def visit(name):
        index[name] = low[name] = len(index)
        stack.append(name)
        active.add(name)
        for target in children[name]:
            if target not in index:
                visit(target)
                low[name] = min(low[name], low[target])
            elif target in active:
                low[name] = min(low[name], index[target])
        if low[name] == index[name]:
            group = []
            while True:
                target = stack.pop()
                active.remove(target)
                group.append(target)
                if target == name:
                    break
            groups.append(group)

    for name in names:
        if name not in index:
            visit(name)
    owner = {name: group for group, members in enumerate(groups) for name in members}
    parents = {group: set() for group in range(len(groups))}
    for source, target in edges:
        if owner[source] != owner[target]:
            parents[owner[target]].add(owner[source])
    ranks = {}
    while len(ranks) < len(groups):
        for group in parents:
            if group not in ranks and parents[group] <= ranks.keys():
                ranks[group] = max((ranks[parent] + 1 for parent in parents[group]), default=0)
    columns = defaultdict(list)
    for name in names:
        columns[ranks[owner[name]]].append(name)
    return {name: (column, row) for column, members in columns.items()
            for row, name in enumerate(sorted(members, key=order.get))}


@dataclass(frozen=True)
class BehaviorBlock:
    name: str
    kind: str
    width: int
    members: tuple[str, ...]
    caption: str
    detail: str
    states: tuple[str, ...] = ()

    @property
    def title(self):
        return self.name.removeprefix('ooc_logic.')

    def value(self, values):
        if any(name not in values for name in self.members):
            return None
        if len(self.members) == 1:
            return values[self.members[0]]
        return sum(values[name] << index for index, name in enumerate(self.members))

    def value_text(self, values):
        value = self.value(values)
        if value is None:
            return '—'
        state = next((name.rsplit('.', 1)[-1] for name in self.states if values.get(name) == value), None)
        return f'{state} ({value})' if state else str(value) if self.width == 1 else f'0x{value:X}'


# Group only output bits whose compiled dependencies justify one functional block.
# Keep every source binding available so grouped shapes still probe the correct signal.
def behavior_graph(simulation):
    """Group physical displays and proven output slices; retain dependencies."""
    sim = simulation
    kinds = getattr(sim, 'signal_kinds', {})
    expressions = getattr(sim, 'signal_expressions', {})
    clocks = getattr(sim, 'signal_clocks', {})
    names = [name for name in sim.signals if name not in sim.clocks and kinds.get(name) != 'constant']
    bundles = defaultdict(list)
    displays = defaultdict(list)
    for name in sim.outputs:
        # On DE10-Lite the first digit identifies the display, the second its
        # segment. Group the physical display even when separate expressions
        # drive its segments; the edges below retain all their dependencies.
        display = re.fullmatch(r'((?:\w+\.)*HEX[0-5])([0-7])', name)
        if display and sim.signals[name] == 1:
            displays[display[1]].append((int(display[2]), name))
            continue
        output = re.fullmatch(r'(.+?)(\d+)', name)
        expression = re.fullmatch(re.escape(name) + r'\s*=\s*([\w.]+)(?:\[(\d+):(\d+)\])?\[(\d+)\]', expressions.get(name, ''))
        if output and expression and sim.signals[name] == 1:
            index, bit = int(output[2]), int(expression[4]) + int(expression[3] or 0)
            bundles[output[1], expression[1], bit - index].append((index, name))
    grouped = {}
    for prefix, entries in displays.items():
        entries.sort()
        if [index for index, _ in entries] != list(range(8)):
            continue
        members = tuple(member for _, member in entries)
        block = BehaviorBlock(f'{prefix}[7:0]', 'output', 8, members,
                              'Seven-segment display · active low',
                              'Bits 0–6: a–g; bit 7: decimal point\n' + ', '.join(members))
        grouped.update((member, block) for member in members)
    for (prefix, driver, offset), entries in bundles.items():
        entries.sort()
        runs = []
        for entry in entries:
            if not runs or entry[0] != runs[-1][-1][0] + 1:
                runs.append([])
            runs[-1].append(entry)
        for run in runs:
            if len(run) < 2:
                continue
            low, high = run[0][0], run[-1][0]
            name = f'{prefix}[{high}:{low}]'
            members = tuple(member for _, member in run)
            block = BehaviorBlock(name, 'output', len(run), members,
                                  f'{driver}[{high + offset}:{low + offset}]',
                                  'Output bus\n' + ', '.join(members) + '\nDriven by ' + driver)
            grouped.update((member, block) for member in members)
    blocks = {}
    owner = {}
    for name in names:
        block = grouped.get(name)
        if block is None:
            kind = 'input' if name in sim.inputs else 'output' if name in sim.outputs else kinds.get(name, 'register')
            width = sim.signals[name]
            expression = expressions.get(name, '')
            states = []
            caption = f'{width}-bit {kind}'
            if kind == 'register' and re.search(re.escape(name) + r'\s*=\s*\(' + re.escape(name) + r'\s*[+-]', expression):
                caption = f'Counter / accumulator · {width} bit'
            elif kind == 'register':
                assigned = ' '.join(re.findall(re.escape(name) + r'\s*=\s*([^\n]+)', expression))
                states = [source for source in sim.dependencies.get(name, ())
                          if kinds.get(source) == 'constant' and re.search(r'\b' + re.escape(source) + r'\b', assigned)]
                if len(states) >= 2:
                    caption = f'State machine · {width} bit'
            elif kind in {'output', 'wire'} and '?' in expression:
                caption = f'{width}-bit select'
            elif kind in {'output', 'wire'}:
                operations = [label for token, label in (('&', 'AND'), ('|', 'OR'), ('^', 'XOR'), ('!', 'NOT'))
                              if token in expression]
                if operations:
                    caption = ' / '.join(operations)
            if clock := clocks.get(name):
                caption += ' · ↑'
            period = re.search(re.escape(name) + r' == (\d+)', expression)
            if re.search(r'(?:^|\.)ooc_every_\d+$', name) and period and getattr(sim, 'frequency', 0):
                milliseconds = (int(period[1]) + 1) * 1000 / sim.frequency
                caption = f'Every {milliseconds:g} ms · ↑'
            constants = [source for source in sim.dependencies.get(name, ()) if kinds.get(source) == 'constant']
            detail = f'{name}\n{kind}, {width} bit(s)'
            if clock:
                detail += f'\nClock: {clock}'
            if expression:
                detail += '\n' + expression[:500]
            if constants:
                detail += '\nConstants: ' + ', '.join(constants)
            block = BehaviorBlock(name, kind, width, (name,), caption, detail, tuple(states))
        blocks[block.name] = block
        owner[name] = block.name
    edges = tuple(dict.fromkeys((owner[source], owner[target]) for target in names
                                for source in sim.dependencies.get(target, ()) if source in owner))
    notice = ''
    if len(blocks) > MAX_NODES or len(edges) > MAX_EDGES:
        notice = (f'{len(names)} signals / {len(edges)} connections exceed the view limit '
                  f'({MAX_NODES} blocks / {MAX_EDGES} connections). Select signals in the simulator to inspect them.')
    return blocks, edges, notice


def block_positions(blocks, edges, *, width=WIDTH, height=HEIGHT):
    positions = _layers(list(blocks), edges)
    returns = defaultdict(int)
    for source, target in edges:
        if positions[source][0] == positions[target][0]:
            returns[positions[source][0]] += 1
    columns = {}
    x = 0
    for column in sorted({position[0] for position in positions.values()}):
        columns[column] = x
        x += max(COLUMN, width + 46 + returns[column] * 7)
    return {name: QRectF(columns[column], row * ROW, width, height)
            for name, (column, row) in positions.items()}


# Use routing gutters for feedback and long dependencies instead of drawing through
# the functional block bodies.
def dependency_paths(rects, edges):
    """Keep feedback in the inter-column gutters, outside component bodies."""
    incoming, lanes, paths = defaultdict(list), defaultdict(int), {}
    for source, target in edges:
        incoming[target].append(source)
    floor = max((rect.bottom() for rect in rects.values()), default=0) + 22
    long_lane = 0
    for source, target in edges:
        a, b = rects[source], rects[target]
        start = QPointF(a.right(), a.center().y())
        offset = incoming[target].index(source) + 1
        end_y = b.top() + b.height() * offset / (len(incoming[target]) + 1)
        path = QPainterPath(start)
        if a.left() == b.left():
            # Enter from the right too: a feedback return must not pass through
            # the other registers in its strongly-connected column.
            lane = a.right() + 16 + lanes[a.left()] * 7
            lanes[a.left()] += 1
            if source == target:
                end_y = b.top() + 12
            path.lineTo(lane, start.y())
            path.lineTo(lane, end_y)
            end = QPointF(b.right(), end_y)
        else:
            end = QPointF(b.left(), end_y)
            if 0 < b.left() - a.left() <= COLUMN + 1:
                middle = (start.x() + end.x()) / 2
                path.lineTo(middle, start.y())
                path.lineTo(middle, end.y())
            else:
                # Skipping columns or returning upstream uses an outside lane.
                lane_y = floor + long_lane * 8
                long_lane += 1
                path.lineTo(a.right() + 14, start.y())
                path.lineTo(a.right() + 14, lane_y)
                path.lineTo(b.left() - 14, lane_y)
                path.lineTo(b.left() - 14, end.y())
        path.lineTo(end)
        paths[source, target] = path
    return paths


class BehaviorDiagram(QGraphicsObject):
    """One lightweight item paints the functional blocks and live signal arrows."""
    input_toggled = Signal(str)
    signal_probed = Signal(str)
    placement_changed = Signal(object, object)
    geometry_changed = Signal()

    def __init__(self, simulation, *, dark=False):
        super().__init__()
        self.dark = dark
        self.blocks, self.edges, self.notice = behavior_graph(simulation)
        self.values = dict(getattr(simulation, 'values', {}))
        self.rects, self.paths = {}, {}
        self.selected = None
        self.press = None
        self.drag_target = None
        self.dragged = False
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.heading = 'Functional schematic · arrows show signal dependencies'
        if simulation.clocks:
            self.heading += '\nClock: ' + ', '.join(simulation.clocks) + ' · ↑ marks clocked blocks'
        if self.notice:
            self._bounds = QRectF(0, 0, 640, 150)
            return
        self.rects = {name: rect.translated(18, 72) for name, rect
                      in block_positions(self.blocks, self.edges).items()}
        self.paths = dependency_paths(self.rects, self.edges)
        routes = {edge: tuple((path.elementAt(i).x, path.elementAt(i).y)
                              for i in range(path.elementCount())) for edge, path in self.paths.items()}
        self.geometry = LogicGeometry({name: (r.x(), r.y(), r.width(), r.height())
                                       for name, r in self.rects.items()}, routes)
        self._bounds = QRectF()
        self._update_geometry()

    def _update_geometry(self):
        self.prepareGeometryChange()
        self.rects = {name: QRectF(*rect) for name, rect in self.geometry.rects.items()}
        self.paths = {}
        for edge, points in self.geometry.routes.items():
            path = QPainterPath(QPointF(*points[0]))
            for point in points[1:]:
                path.lineTo(*point)
            self.paths[edge] = path
        self._bounds = QRectF(0, 0, 580, 72)
        for rect in self.rects.values():
            self._bounds = self._bounds.united(rect.adjusted(-18, -18, 38, 18))
        for path in self.paths.values():
            self._bounds = self._bounds.united(path.boundingRect().adjusted(-8, -8, 8, 8))
        self.update()
        self.geometry_changed.emit()

    def placement(self):
        return self.geometry.snapshot() if not self.notice else {}

    def restore_placement(self, record):
        if not self.notice:
            self.geometry.restore(record)
            self._update_geometry()

    def boundingRect(self):
        return self._bounds

    def set_values(self, values):
        self.values = dict(values)
        self.update()

    def _color(self, block):
        value = block.value(self.values)
        return QColor('#777777' if value is None else '#299f68' if value else '#888888')

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self._bounds, QColor('#252525' if self.dark else '#ffffff'))
        foreground = QColor('#eeeeee' if self.dark else '#292929')
        painter.setPen(foreground)
        painter.drawText(QRectF(18, 9, self._bounds.width() - 36, 52), Qt.TextFlag.TextWordWrap, self.heading)
        if self.notice:
            painter.drawText(self._bounds.adjusted(18, 66, -18, -12), Qt.TextFlag.TextWordWrap, self.notice)
            return
        for (source, target), path in self.paths.items():
            selected = self.selected in {source, target}
            color = QColor('#278bda') if selected else self._color(self.blocks[source])
            painter.setPen(QPen(color, 2.4 if selected else 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            end = path.currentPosition()
            before = path.pointAtPercent(.99)
            direction = 1 if end.x() >= before.x() else -1
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([end, end + QPointF(-7 * direction, -4), end + QPointF(-7 * direction, 4)]))
        for name, block in self.blocks.items():
            area = self.rects[name]
            painter.setBrush(QColor('#343434' if self.dark else '#f1f1f1'))
            painter.setPen(QPen(QColor('#278bda' if name == self.selected else '#999999'), 1.8))
            painter.drawRoundedRect(area, 5, 5)
            painter.setPen(foreground)
            metrics = painter.fontMetrics()
            title = metrics.elidedText(block.title, Qt.TextElideMode.ElideMiddle, WIDTH - 18)
            painter.drawText(area.adjusted(9, 4, -9, -36), Qt.AlignmentFlag.AlignVCenter, title)
            caption = metrics.elidedText(block.caption, Qt.TextElideMode.ElideRight, WIDTH - 18)
            painter.drawText(area.adjusted(9, 23, -9, -17), Qt.AlignmentFlag.AlignVCenter, caption)
            text = metrics.elidedText(block.value_text(self.values), Qt.TextElideMode.ElideRight, WIDTH - 18)
            painter.setPen(self._color(block))
            painter.drawText(area.adjusted(9, 42, -9, -2), Qt.AlignmentFlag.AlignVCenter, text)

    def _hit(self, pos):
        for name, area in self.rects.items():
            if area.contains(pos):
                return self.blocks[name], False
        stroker = QPainterPathStroker()
        stroker.setWidth(9)
        for (source, _), path in self.paths.items():
            if stroker.createStroke(path).contains(pos):
                return self.blocks[source], True
        return None, False

    def hoverMoveEvent(self, event):
        block, _ = self._hit(event.pos())
        self.setToolTip(block.detail if block else self.notice or self.heading)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        self.press = (event.screenPos(), event.pos(), self.placement())
        self.dragged = False
        self.drag_target = None
        if not self.notice:
            block, wire = self._hit(event.pos())
            if block and not wire:
                self.drag_target = ('block', block.name)
            elif segment := self.geometry.nearest_segment((event.pos().x(), event.pos().y()), 8):
                self.drag_target = ('wire', *segment)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.press is not None and self.drag_target is not None:
            screen, start, original = self.press
            if self.dragged or (event.screenPos() - screen).manhattanLength() > 8:
                self.dragged = True
                self.geometry.restore(original)
                delta = event.pos() - start
                if self.drag_target[0] == 'block':
                    self.geometry.move_block(self.drag_target[1], delta.x(), delta.y())
                else:
                    _, edge, segment = self.drag_target
                    a, b = self.geometry.routes[edge][segment:segment + 2]
                    self.geometry.slide_wire(edge, segment, delta.y() if a[1] == b[1] else delta.x())
                self._update_geometry()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.press is not None and self.dragged:
            before, after = self.press[2], self.placement()
            if before != after:
                self.placement_changed.emit(before, after)
        elif self.press is not None and (event.screenPos() - self.press[0]).manhattanLength() <= 8:
            block, wire = self._hit(event.pos())
            if block:
                self.selected = block.name
                self.update()
                signal = self.input_toggled if block.kind == 'input' and block.width == 1 and not wire else self.signal_probed
                signal.emit(block.members[0])
        self.press = None
        self.drag_target = None
        self.dragged = False
        event.accept()
