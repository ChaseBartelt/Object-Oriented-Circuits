"""Interactive digital ports and an expandable, source-backed dependency graph."""
# Digital graph nodes and edges keep source/runtime identities behind their screen positions.
# An input click changes logic; output and dependency clicks probe values. A drag changes
# presentation geometry and must not accidentally perform the click action on release.

import math
from copy import deepcopy

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QGraphicsView

from circuitlab.layout.logic import LogicGeometry
from circuitlab.layout.logic_hierarchy import logic_hierarchy
from circuitlab.viewer.behavior_diagram import _layers, behavior_graph, block_positions, dependency_paths
from circuitlab.viewer.behavior_scope import chip_diagram_model


HIGH, LOW, BUS = "#55d991", "#999999", "#cccccc"
NODE_WIDTH, NODE_HEIGHT, COLUMN, ROW = 180, 58, 244, 82
MAX_NODES = 120


class _Node(QGraphicsObject):
    def __init__(self, view, name, kind, width=1, *, height=NODE_HEIGHT, caption="", detail="", module=None):
        super().__init__()
        self.view, self.name, self.kind, self.width = view, name, kind, width
        self.height, self.caption, self.detail = height, caption, detail
        self.module = module
        self.value = None
        self.probe_name = name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setToolTip(detail or name)

    def boundingRect(self):
        return QRectF(0, 0, NODE_WIDTH, self.height)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.module is not None:
            self._paint_module(painter)
            return
        color = QColor(BUS if self.width > 1 else HIGH if self.value else LOW)
        painter.setPen(QPen(color if self.kind != "chip" else QColor("#888888"), 1.8))
        painter.setBrush(QColor("#343434" if self.kind == "chip" else "#282828"))
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 7, 7)
        if self.kind == "chip":
            painter.setPen(QColor("#f2f2f2"))
            painter.drawText(QRectF(9, 4, NODE_WIDTH - 18, min(self.height - 8, 116)),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             self.caption)
            return
        painter.setBrush(color)
        if self.width > 1:
            painter.drawRect(QRectF(14, 13, 18, 15))
        elif self.kind == "input" and self.probe_name in self.view.inputs:
            painter.drawRoundedRect(QRectF(11, 13, 30, 15), 7, 7)
            painter.setBrush(QColor("#191919"))
            painter.drawEllipse(QPointF(33 if self.value else 19, 20), 5, 5)
        elif self.kind == "output":
            painter.drawEllipse(QPointF(23, 20), 8, 8)
        else:
            painter.drawRect(QRectF(16, 14, 13, 13))
        painter.setPen(QColor("#f2f2f2"))
        label = painter.fontMetrics().elidedText(self.name.removeprefix('ooc_logic.'), Qt.TextElideMode.ElideMiddle, NODE_WIDTH - 55)
        painter.drawText(QRectF(48, 6, NODE_WIDTH - 55, 25), Qt.AlignmentFlag.AlignVCenter, label)
        block = self.view.blocks.get(self.name)
        value = block.value_text(self.view.graph_values) if block else _logic_value(self.value, self.width)
        label = f"{self.kind} · {value}"
        if self.caption:
            label = f"{value} · {self.caption}"
        label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, NODE_WIDTH - 18)
        painter.setPen(color)
        painter.drawText(QRectF(9, self.height - 24, NODE_WIDTH - 18, 20), Qt.AlignmentFlag.AlignVCenter, label)

    def _paint_module(self, painter):
        painter.setPen(QPen(QColor("#81aecb"), 1.8))
        painter.setBrush(QColor("#263744"))
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 7, 7)
        outputs = [port for port in self.module.ports if port.direction == "output"]
        ports = sorted(outputs, key=lambda port: not 1 < port.width <= 32)[:2]
        preview = " · ".join(f"{port.label} {_logic_value(self.view.values.get(port.live), port.width)}"
                             for port in ports)
        for text, y, color in ((self.module.title, 5, "#f2f2f2"),
                               (preview or self.module.caption, 30, "#aaccdf")):
            painter.setPen(QColor(color))
            text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, NODE_WIDTH - 20)
            painter.drawText(QRectF(10, y, NODE_WIDTH - 20, 20), Qt.AlignmentFlag.AlignVCenter, text)


def _logic_value(value, width):
    if value is None:
        return "—"
    return str(value) if width == 1 else f"0x{value:X}"


class _Edge(QGraphicsPathItem):
    def __init__(self, view, path, source, target):
        super().__init__(path)
        self.view = view
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

class LogicSceneView(QGraphicsView):
    input_toggled = Signal(str)
    signal_probed = Signal(str)
    placement_changed = Signal()

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
        self.blocks = {}
        self.graph_values, self.bindings, self.edge_signals = {}, {}, {}
        self.scope = None
        self.expanded = False
        self.notice = ""
        self._pan = None
        self.geometry = None
        self._placements, self._history = {}, []
        self._drag = None

    def configure(self, simulation):
        self.geometry = None
        self._placements, self._history = {}, []
        self._drag = None
        self.simulation = simulation
        self.inputs = tuple(simulation.inputs) if simulation else ()
        self.outputs = tuple(simulation.outputs) if simulation else ()
        self.values = dict(simulation.values) if simulation else {}
        # A large assembly opens on its block diagram, where its internals fit.
        self.expanded = bool(self.instances and behavior_graph(simulation)[2])
        self.scope = None
        self._build()

    @property
    def instances(self):
        return getattr(self.simulation, "instances", ())

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
        if start.y() != end.y():
            middle = (start.x() + end.x()) / 2
            path.lineTo(middle, start.y())
            path.lineTo(middle, end.y())
        path.lineTo(end)
        edge = _Edge(self, path, source, target)
        self.edge_items[source, target] = edge
        self.scene().addItem(edge)

    def _build(self):
        self.geometry = None
        scene = self.scene()
        scene.clear()
        self.input_items, self.output_items, self.node_items, self.edge_items = {}, {}, {}, {}
        self.chip_item = None
        self.blocks = {}
        self.graph_values, self.bindings, self.edge_signals = {}, {}, {}
        self.notice = ""
        sim = self.simulation
        if sim is None:
            self.resetTransform()
            self.setSceneRect(QRectF())
            return
        if not self.expanded and self.instances and len(self.inputs) + len(self.outputs) > MAX_NODES:
            self.expanded = True
        if not self.expanded and len(self.inputs) + len(self.outputs) > MAX_NODES:
            self.notice = f"Port view exceeds {MAX_NODES} nodes; use the signal table and probes."
            title = scene.addText(self.notice)
            title.setDefaultTextColor(QColor("#f2f2f2"))
            self.fit()
            return
        if self.expanded:
            blocks, edges, hierarchy = self._open_graph()
            self.expanded = bool(self.instances) or not self.notice
        if self.notice or self.expanded:
            title = scene.addText(self.notice or self._graph_title(hierarchy))
            title.setDefaultTextColor(QColor("#bbbbbb"))
            title.setPos(0, -55)
        if self.expanded:
            self._paint_graph(blocks, edges, hierarchy)
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
                self._connect("__chip__", name, QPointF(COLUMN + NODE_WIDTH, point.y()), QPointF(COLUMN * 2, point.y()))
        items = dict(self.node_items)
        if not self.expanded:
            items['__chip__'] = self.chip_item
        rects = {name: (item.pos().x(), item.pos().y(), item.boundingRect().width(), item.boundingRect().height())
                 for name, item in items.items()}
        routes = {}
        for edge, item in self.edge_items.items():
            path = item.path()
            routes[edge] = tuple((path.elementAt(index).x, path.elementAt(index).y) for index in range(path.elementCount()))
        self.geometry = LogicGeometry(rects, routes, self._placements.get(self._mode()))
        self._apply_geometry()
        self.set_values(self.values)
        self._frame_initial()

    def _open_graph(self):
        if self.instances:
            hierarchy = logic_hierarchy(self.simulation, self.instances, self.scope)
            if hierarchy.blocks:
                self.notice = hierarchy.notice
                if not self.notice:
                    self.edge_signals = hierarchy.edge_signals
                    return hierarchy.blocks, hierarchy.edges, True
                return {}, (), True
        sim = self.simulation
        if self.scope is not None:
            sim, self.bindings = chip_diagram_model(self.instances, self.scope, sim)
        blocks, edges, self.notice = behavior_graph(sim)
        return ({}, (), False) if self.notice else (blocks, edges, False)

    def _graph_title(self, hierarchy):
        if not self.instances:
            return "Functional schematic · arrows show signal dependencies"
        names = [self.simulation.name]
        index = self.scope
        while index is not None:
            instance = self.instances[index]
            names.insert(1, instance.name)
            index = instance.parent
        return " / ".join(names) + ("   ·   Click a block to open it" if hierarchy else "")

    def _paint_graph(self, blocks, edges, hierarchy):
        scene = self.scene()
        self.blocks = blocks
        positions = block_positions(blocks, edges, width=NODE_WIDTH, height=NODE_HEIGHT)
        for name, rectangle in positions.items():
            block = blocks[name]
            item = _Node(self, name, block.kind, block.width, caption=block.caption,
                         detail=block.detail, module=block if hierarchy else None)
            if not hierarchy:
                item.probe_name = self.bindings.get(block.members[0], block.members[0])
            item.setPos(rectangle.topLeft() + QPointF(0, 70))
            scene.addItem(item)
            self.node_items[name] = item
            if block.kind == "input":
                self.input_items[name] = item
            elif block.kind == "output":
                self.output_items[name] = item
        if self.scope is not None:
            caption = "‹ Back"
        else:
            caption = "‹ Package pins" if hierarchy else "Functional schematic\nClick to close"
        self.chip_item = _Node(self, "__chip__", "chip", caption=caption, height=48)
        scene.addItem(self.chip_item)
        rectangles = {name: item.sceneBoundingRect() for name, item in self.node_items.items()}
        for (source, target), path in dependency_paths(rectangles, edges).items():
            edge = _Edge(self, path, source, target)
            if signals := self.edge_signals.get((source, target)):
                edge.setToolTip(", ".join(signals))
            self.edge_items[source, target] = edge
            scene.addItem(edge)

    def _frame_initial(self):
        """Start readable at the top; Fit remains an explicit whole-graph view."""
        area = self.scene().itemsBoundingRect().adjusted(-25, -25, 25, 25)
        self.setSceneRect(area)
        self.resetTransform()
        scale = max(.8, min(1.0, self.viewport().width() / max(area.width(), 1)))
        self.scale(scale, scale)
        self.centerOn(area.center().x(), area.top() + self.viewport().height() / (2 * scale))

    # Update existing node/edge values without rebuilding the graph or refitting the viewport.
    def set_values(self, values):
        self.values = dict(values)
        self.graph_values = ({name: values[live] for name, live in self.bindings.items() if live in values}
                             if self.bindings else self.values)
        for name, item in self.node_items.items():
            if item.module:
                value = tuple(values.get(port.live) for port in item.module.ports)
                if value != item.value:
                    item.setToolTip(item.module.title + "\n" + "\n".join(
                        f"{port.label} ({port.direction})  {_logic_value(level, port.width)}"
                        + (f"  ·  {port.live}" if port.live != port.label else "")
                        for port, level in zip(item.module.ports, value)))
            else:
                block = self.blocks.get(name)
                value = block.value(self.graph_values) if block else self.values.get(name)
            if value != item.value:
                item.value = value
                item.update()
        for (source, target), item in self.edge_items.items():
            if source == '__chip__':
                source = target
            block = self.blocks.get(source)
            if signals := self.edge_signals.get((source, target)):
                width = sum(self.simulation.signals[signal] for signal in signals)
                value = self.values.get(signals[0], 0)
            else:
                width = block.width if block else self.simulation.signals.get(source, 1) if self.simulation else 1
                value = block.value(self.graph_values) if block else self.values.get(source, 0)
            color = QColor(BUS if width > 1 else HIGH if value else LOW)
            pen_width = 2.5 if width > 1 else 1.7
            if item.pen().color() != color or item.pen().widthF() != pen_width:
                item.setPen(QPen(color, pen_width))

    def toggle_hood(self):
        if self.simulation is not None:
            self._remember_placement()
            if self.scope is None:
                self.expanded = not self.expanded
            else:
                self.scope = self.instances[self.scope].parent
            self._build()

    def _open_instance(self, index, simulation):
        if (self.simulation is not simulation or not self.expanded
                or simulation.instances[index].parent != self.scope):
            return
        self._remember_placement()
        self.scope = index
        self._build()

    def _mode(self):
        if self.scope is not None:
            return f"instance:{self.scope}:{self.instances[self.scope].name}"
        if self.expanded and self.instances:
            return "hierarchy"
        return 'internals' if self.expanded else 'ports'

    def _remember_placement(self):
        if self.geometry is not None:
            self._placements[self._mode()] = self.geometry.snapshot()

    def placement(self):
        self._remember_placement()
        return deepcopy(self._placements)

    # Restore only compatible presentation records; runtime logic and signal bindings are unchanged.
    def restore_placement(self, payload):
        self._placements = deepcopy(payload) if isinstance(payload, dict) else {}
        self._history.clear()
        self._drag = None
        if self.geometry is not None:
            self.geometry.restore(self._placements.get(self._mode()))
            self._apply_geometry()
            self._expand_extent()

    def _apply_geometry(self):
        if self.geometry is None:
            return
        for name, (x, y, _, _) in self.geometry.rects.items():
            item = self.chip_item if name == '__chip__' else self.node_items[name]
            item.setPos(x, y)
        for edge, points in self.geometry.routes.items():
            path = QPainterPath(QPointF(*points[0]))
            for point in points[1:]:
                path.lineTo(*point)
            self.edge_items[edge].setPath(path)

    def _expand_extent(self):
        center = self.mapToScene(self.viewport().rect().center())
        self.setSceneRect(self.sceneRect().united(self.scene().itemsBoundingRect().adjusted(-25, -25, 25, 25)))
        self.centerOn(center)

    def undo_placement(self):
        if self._drag is not None or not self._history:
            return False
        mode, before = self._history.pop()
        self._placements[mode] = before
        if mode == self._mode() and self.geometry is not None:
            self.geometry.restore(before)
            self._apply_geometry()
            self._expand_extent()
        self.placement_changed.emit()
        return True

    def _activate(self, item):
        if isinstance(item, _Node):
            if item.module:
                index, simulation = item.module.instance, self.simulation
                QTimer.singleShot(0, self, lambda: self._open_instance(index, simulation))
            elif item.kind == 'chip':
                QTimer.singleShot(0, self.toggle_hood)
            elif item.kind == 'input' and item.probe_name in self.inputs:
                self.input_toggled.emit(item.probe_name)
            else:
                self.signal_probed.emit(item.probe_name)
        elif isinstance(item, _Edge):
            if signals := self.edge_signals.get((item.source, item.target)):
                self.signal_probed.emit(signals[0])
            else:
                source = item.target if item.source == '__chip__' else item.source
                self.signal_probed.emit(self.node_items[source].probe_name)

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
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.LeftButton and self.geometry is not None:
            point = self.mapToScene(event.position().toPoint())
            segment = None if isinstance(item, _Node) else self.geometry.nearest_segment(
                (point.x(), point.y()), 7 / max(self.transform().m11(), .01))
            if isinstance(item, _Node) or segment is not None:
                if segment is not None:
                    item = self.edge_items[segment[0]]
                self._drag = dict(item=item, segment=segment, origin=point,
                                  screen=event.position().toPoint(), before=self.geometry.snapshot(), moved=False)
                event.accept()
                return
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and not isinstance(item, (_Node, _Edge))):
            self._pan = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            drag = self._drag
            if (event.position().toPoint() - drag['screen']).manhattanLength() > 8:
                drag['moved'] = True
            if drag['moved']:
                delta = self.mapToScene(event.position().toPoint()) - drag['origin']
                self.geometry.restore(drag['before'])
                item, segment = drag['item'], drag['segment']
                if segment is not None:
                    edge, index = segment
                    a, b = self.geometry.routes[edge][index:index + 2]
                    self.geometry.slide_wire(edge, index, delta.y() if a[1] == b[1] else delta.x())
                elif item.name in self.geometry.rects:
                    self.geometry.move_block(item.name, delta.x(), delta.y())
                self._apply_geometry()
            event.accept()
            return
        if self._pan is not None:
            point = event.position().toPoint()
            delta = point - self._pan
            self._pan = point
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    # Commit a valid drag separately from click activation so moving an input does not toggle it.
    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            drag, self._drag = self._drag, None
            if self.geometry.snapshot() != drag['before']:
                self._history.append((self._mode(), drag['before']))
                del self._history[:-100]
                self._remember_placement()
                self._expand_extent()
                self.placement_changed.emit()
            elif not drag['moved']:
                self._activate(drag['item'])
            event.accept()
            return
        if self._pan is not None:
            self._pan = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._drag is not None:
            self.geometry.restore(self._drag['before'])
            self._drag = None
            self._apply_geometry()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Undo) and self.undo_placement():
            event.accept()
            return
        super().keyPressEvent(event)
