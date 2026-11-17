"""Interactive digital ports and an expandable, source-backed dependency graph."""

from collections import defaultdict
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QGraphicsView


HIGH, LOW, BUS = "#55d991", "#999999", "#cccccc"
NODE_WIDTH, NODE_HEIGHT, COLUMN, ROW = 180, 58, 244, 82
MAX_NODES, MAX_EDGES = 120, 400


def _layers(names, edges):
    """Condense feedback cycles, then assign deterministic dependency columns."""
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


class _Node(QGraphicsObject):
    def __init__(self, view, name, kind, width=1, *, height=NODE_HEIGHT, caption="", detail=""):
        super().__init__()
        self.view, self.name, self.kind, self.width = view, name, kind, width
        self.height, self.caption, self.detail = height, caption, detail
        self.value = None
        self.press = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(detail or name)

    def boundingRect(self):
        return QRectF(0, 0, NODE_WIDTH, self.height)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(BUS if self.width > 1 else HIGH if self.value else LOW)
        painter.setPen(QPen(color if self.kind != "chip" else QColor("#888888"), 1.8))
        painter.setBrush(QColor("#343434" if self.kind == "chip" else "#282828"))
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 7, 7)
        if self.kind == "chip":
            painter.setPen(QColor("#f2f2f2"))
            painter.drawText(self.boundingRect().adjusted(9, 4, -9, -4),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             self.caption)
            return
        painter.setBrush(color)
        if self.width > 1:
            painter.drawRect(QRectF(14, 13, 18, 15))
        elif self.kind == "input":
            painter.drawRoundedRect(QRectF(11, 13, 30, 15), 7, 7)
            painter.setBrush(QColor("#191919"))
            painter.drawEllipse(QPointF(33 if self.value else 19, 20), 5, 5)
        elif self.kind == "output":
            painter.drawEllipse(QPointF(23, 20), 8, 8)
        else:
            painter.drawRect(QRectF(16, 14, 13, 13))
        painter.setPen(QColor("#f2f2f2"))
        label = painter.fontMetrics().elidedText(self.name, Qt.TextElideMode.ElideMiddle, NODE_WIDTH - 55)
        painter.drawText(QRectF(48, 6, NODE_WIDTH - 55, 25), Qt.AlignmentFlag.AlignVCenter, label)
        value = "—" if self.value is None else str(self.value) if self.width == 1 else f"0x{self.value:X}"
        label = f"{self.kind} · {value}"
        if self.caption:
            label = f"{value} · {self.caption}"
        label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, NODE_WIDTH - 18)
        painter.setPen(color)
        painter.drawText(QRectF(9, self.height - 24, NODE_WIDTH - 18, 20), Qt.AlignmentFlag.AlignVCenter, label)

    def mousePressEvent(self, event):
        self.press = event.screenPos()
        event.accept()

    def mouseReleaseEvent(self, event):
        clicked = self.press is not None and (event.screenPos() - self.press).manhattanLength() <= 8
        self.press = None
        event.accept()
        if clicked:
            if self.kind == "chip":
                # Rebuild after Qt finishes dispatching the event to this item.
                QTimer.singleShot(0, self.view.toggle_hood)
            elif self.kind == "input":
                self.view.input_toggled.emit(self.name)
            else:
                self.view.signal_probed.emit(self.name)


class _Edge(QGraphicsPathItem):
    def __init__(self, view, path, source, target):
        super().__init__(path)
        self.view, self.press = view, None
        self.source, self.target = source, target
        self.setZValue(-1)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{source} → {target}")
        self.setPen(QPen(QColor(LOW), 1.7))

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        end = self.path().pointAtPercent(1)
        before = self.path().pointAtPercent(.98)
        angle = math.atan2(end.y() - before.y(), end.x() - before.x())
        arrow = QPolygonF([end, end - QPointF(8 * math.cos(angle - .45), 8 * math.sin(angle - .45)),
                          end - QPointF(8 * math.cos(angle + .45), 8 * math.sin(angle + .45))])
        painter.setPen(self.pen())
        painter.setBrush(self.pen().color())
        painter.drawPolygon(arrow)

    def mousePressEvent(self, event):
        self.press = event.screenPos()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.press is not None and (event.screenPos() - self.press).manhattanLength() <= 8:
            self.view.signal_probed.emit(self.source)
        self.press = None
        event.accept()


class LogicSceneView(QGraphicsView):
    input_toggled = Signal(str)
    signal_probed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#191919"))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMinimumHeight(220)
        self.simulation = None
        self.inputs, self.outputs, self.values = (), (), {}
        self.input_items, self.output_items, self.node_items, self.edge_items = {}, {}, {}, {}
        self.chip_item = None
        self.expanded = False
        self.notice = ""
        self._pan = None

    def configure(self, simulation):
        self.simulation = simulation
        self.inputs = tuple(simulation.inputs) if simulation else ()
        self.outputs = tuple(simulation.outputs) if simulation else ()
        self.values = dict(simulation.values) if simulation else {}
        self.expanded = False
        self._build()

    def _node(self, name, kind, x, y):
        sim = self.simulation
        expression = getattr(sim, "signal_expressions", {}).get(name, "")
        clock = getattr(sim, "signal_clocks", {}).get(name)
        caption = f"{sim.signals[name]}-bit register" if kind == "register" else kind
        if kind in {"output", "constant"} and expression:
            caption = expression
        if clock:
            caption += f" @ {clock}"
        detail = f"{name}\n{kind}, {sim.signals[name]} bit(s)"
        if clock:
            detail += f"\nClock: {clock}"
        if expression:
            detail += f"\n{expression}"
        item = _Node(self, name, kind, sim.signals[name], caption=caption, detail=detail)
        item.setPos(x, y)
        self.scene().addItem(item)
        self.node_items[name] = item
        if kind == "input":
            self.input_items[name] = item
        elif kind == "output":
            self.output_items[name] = item
        return item

    def _connect(self, source, target, start, end):
        path = QPainterPath(start)
        if end.x() > start.x():
            bend = max(25, (end.x() - start.x()) / 2)
            path.cubicTo(start + QPointF(bend, 0), end - QPointF(bend, 0), end)
        else:
            # Feedback takes an explicit curved return path without junction dots.
            lift = min(start.y(), end.y()) - 65
            path.cubicTo(QPointF(start.x() + 90, lift), QPointF(end.x() - 90, lift), end)
        edge = _Edge(self, path, source, target)
        self.edge_items[source, target] = edge
        self.scene().addItem(edge)

    def _build(self):
        scene = self.scene()
        scene.clear()
        self.input_items, self.output_items, self.node_items, self.edge_items = {}, {}, {}, {}
        self.chip_item = None
        self.notice = ""
        sim = self.simulation
        if sim is None:
            self.resetTransform()
            self.setSceneRect(QRectF())
            return
        if len(self.inputs) + len(self.outputs) > MAX_NODES:
            self.notice = f"Port view exceeds {MAX_NODES} nodes; use the signal table and probes."
            title = scene.addText(self.notice)
            title.setDefaultTextColor(QColor("#f2f2f2"))
            self.fit()
            return
        names = [name for name in sim.signals if name not in sim.clocks]
        allowed = set(names)
        edges = list(dict.fromkeys((source, target) for target in names
                                   for source in sim.dependencies.get(target, ()) if source in allowed))
        if self.expanded and (len(names) > MAX_NODES or len(edges) > MAX_EDGES):
            self.expanded = False
            self.notice = f"Internals contain {len(names)} signals / {len(edges)} connections; view limit is {MAX_NODES} / {MAX_EDGES}. Use probes."
        if self.notice or self.expanded:
            title = scene.addText(self.notice or "Signal dependencies · crossing lines do not join")
            title.setDefaultTextColor(QColor("#bbbbbb"))
            title.setPos(0, -55)
        if self.expanded:
            positions = _layers(names, edges)
            kinds = getattr(sim, "signal_kinds", {})
            for name, (column, row) in positions.items():
                kind = "input" if name in self.inputs else "output" if name in self.outputs else kinds.get(name, "register")
                self._node(name, kind, column * COLUMN, row * ROW + 70)
            self.chip_item = _Node(self, "__chip__", "chip", caption="Signal dependencies\nClick to close", height=48)
            self.chip_item.setPos(0, 0)
            scene.addItem(self.chip_item)
            for source, target in edges:
                a, b = self.node_items[source].sceneBoundingRect(), self.node_items[target].sceneBoundingRect()
                self._connect(source, target, QPointF(a.right(), a.center().y()), QPointF(b.left(), b.center().y()))
        else:
            rows = max(len(self.inputs), len(self.outputs), 1)
            height = max(NODE_HEIGHT, (rows - 1) * ROW + NODE_HEIGHT)
            self.chip_item = _Node(self, "__chip__", "chip", height=height,
                                  caption=f"{getattr(sim, 'name', 'Digital chip')}\n\nClick to open internals", detail="Expand the source-backed signal dependency graph")
            self.chip_item.setPos(COLUMN, 0)
            scene.addItem(self.chip_item)
            for row, name in enumerate(self.inputs):
                item = self._node(name, "input", 0, row * ROW)
                point = item.sceneBoundingRect().center()
                self._connect(name, "__chip__", QPointF(NODE_WIDTH, point.y()), QPointF(COLUMN, point.y()))
            for row, name in enumerate(self.outputs):
                item = self._node(name, "output", COLUMN * 2, row * ROW)
                point = item.sceneBoundingRect().center()
                self._connect(name, name, QPointF(COLUMN + NODE_WIDTH, point.y()), QPointF(COLUMN * 2, point.y()))
        self.set_values(self.values)
        self._frame_initial()

    def _frame_initial(self):
        """Start readable at the top; Fit remains an explicit whole-graph view."""
        area = self.scene().itemsBoundingRect().adjusted(-25, -25, 25, 25)
        self.setSceneRect(area)
        self.resetTransform()
        scale = max(.8, min(1.0, self.viewport().width() / max(area.width(), 1)))
        self.scale(scale, scale)
        self.centerOn(area.center().x(), area.top() + self.viewport().height() / (2 * scale))

    def set_values(self, values):
        self.values = dict(values)
        for name, item in self.node_items.items():
            value = self.values.get(name)
            if value != item.value:
                item.value = value
                item.update()
        for (source, _), item in self.edge_items.items():
            width = self.simulation.signals.get(source, 1) if self.simulation else 1
            color = QColor(BUS if width > 1 else HIGH if self.values.get(source, 0) else LOW)
            pen_width = 2.5 if width > 1 else 1.7
            if item.pen().color() != color or item.pen().widthF() != pen_width:
                item.setPen(QPen(color, pen_width))

    def toggle_hood(self):
        if self.simulation is not None:
            self.expanded = not self.expanded
            self._build()

    def fit(self):
        area = self.scene().itemsBoundingRect().adjusted(-25, -25, 25, 25)
        self.setSceneRect(area)
        if not area.isEmpty():
            self.fitInView(area, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor):
        if not math.isfinite(factor) or factor <= 0:
            return
        current = self.transform().m11()
        desired = min(6.0, max(.08, current * factor))
        # Fit can lie outside the manual zoom range; never reverse the request.
        desired = max(current, desired) if factor > 1 else min(current, desired)
        if current:
            self.scale(desired / current, desired / current)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.zoom(1.15 if delta > 0 else 1 / 1.15)
        event.accept()

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and not isinstance(item, (_Node, _Edge))):
            self._pan = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan is not None:
            point = event.position().toPoint()
            delta = point - self._pan
            self._pan = point
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan is not None:
            self._pan = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)
