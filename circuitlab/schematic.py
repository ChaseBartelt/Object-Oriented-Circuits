"""PySide6 schematic scene for the shared layout model."""

import math
import re
from collections import Counter, defaultdict

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QMenu,
)

from .report import component_labels
from .routing import schematic_layout

INK = QColor("#192633")
NET = QColor("#006565")
ACCENT = QColor("#006dab")
PROBE = QColor("#974616")
SCALE = 24


def point(p):
    return QPointF(p[0] * SCALE, -p[1] * SCALE)


class SchematicView(QGraphicsView):
    """Paint a layout and map mouse selections back to circuit objects."""

    probed = Signal(str, str)
    pin_requested = Signal(str)
    pin_renamed = Signal(str)
    pin_removed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#ffffff"))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.highlight = None
        self.paths = {}
        self.net_items = defaultdict(list)
        self.anchors = {}
        self.pin_items = []
        self.captions = []
        self.place_pin = False
        self.circuit = None
        self.layout = None
        self.layout_mode = "auto"
        self.fitted = True
        self.fit_scale = 1.0
        self.label_font = QFont("Inter")
        self.label_font.setPixelSize(12)

    def clear(self):
        self.scene().clear()
        self.paths.clear()
        self.net_items.clear()
        self.anchors.clear()
        self.pin_items.clear()
        self.captions.clear()
        self.highlight = None
        self.circuit = None
        self.layout = None

    def draw(self, circuit, pins=None):
        """Render one circuit and retain its geometry for schematic export."""
        self.clear()
        if not circuit.components:
            return
        self.circuit = circuit
        scene = self.scene()
        labels = component_labels(circuit)
        layout = schematic_layout(circuit, labels, self.layout_mode)
        self.layout = layout
        for name in layout.wires:
            path = QPainterPath()
            for a, b in layout.visible_segments(name):
                path.moveTo(point(a))
                path.lineTo(point(b))
            item = scene.addPath(path, self.pen(INK, 1.15))
            self.paths[name] = item
            item.setData(0, "wire")
            item.setData(1, name)
            item.setToolTip(name)
        for net, dots in layout.junctions.items():
            for dot in dots:
                p = point(dot)
                item = scene.addEllipse(
                    p.x() - 2.4,
                    p.y() - 2.4,
                    4.8,
                    4.8,
                    QPen(Qt.PenStyle.NoPen),
                    QBrush(INK),
                )
                item.setData(0, "node")
                item.setData(1, net)
                item.setToolTip(net)
        tap_names = {tap.name for tap in circuit.wire_taps}
        counts = Counter()
        component_captions = []
        for part, a, b, top in layout.symbols:
            counts[part.kind] += 1
            name = labels[part.name]
            if len(name[0]) > 24:
                prefix = "U" if part.kind == "O" else part.kind
                name = [f"{prefix}#{counts[part.kind]}", *name[1:]]
            pos, alignment = self.symbol(part, a, b, top)
            component_captions.append((name, pos, part.name, alignment))
        for pos in layout.grounds:
            net = layout.ground_names.get(pos, "0")
            self.ground(pos, net)
            self.anchors.setdefault(net, pos)
        for name, pos, part_name, alignment in component_captions:
            self.place_caption(name, pos, INK, ("component", part_name), alignment)
        for name, pos in layout.labels.items():
            if (
                pos not in layout.grounds
                and name not in tap_names
                and not name.startswith("@")
                and not re.fullmatch(r"g\d+_\d+", name)
            ):
                if "." not in name:
                    self.place_caption(
                        name.replace("_", " "), (pos[0], pos[1] + 0.17), NET, ("node", name)
                    )
                self.anchors[name] = pos
        for net, pos, name, left, below in layout.connectors:
            p = point(pos)
            item = scene.addEllipse(
                p.x() - 2.3,
                p.y() - 2.3,
                4.6,
                4.6,
                self.pen(NET, 1.2),
                QBrush(Qt.GlobalColor.white),
            )
            item.setData(0, "node")
            item.setData(1, net)
            item.setToolTip(net)
            self.net_items[net].append(item)
            self.place_caption(
                name,
                (pos[0] + (-0.16 if left else 0.16), pos[1]),
                NET,
                ("node", net),
                ("right" if left else "left") + (" below" if below else ""),
            )
            self.anchors.setdefault(net, pos)
        symbols = {part.name: (a, b) for part, a, b, _ in layout.symbols}
        connector_names = {(net, name) for net, _, name, _, _ in layout.connectors}
        tap_points = defaultdict(list)
        for tap in circuit.wire_taps:
            if (tap.net, tap.name.replace("_", " ")) in connector_names:
                continue
            pos = symbols[tap.component][tap.terminal]
            tap_points[tap.net, pos].append(tap.name)
        for (net, pos), names in tap_points.items():
            self.anchors.setdefault(net, pos)
            x, y = pos
            alignment = "left" if pos in layout.grounds else "center"
            label_pos = (x + 0.45, y + 0.14) if alignment == "left" else (x, y + 0.17)
            self.place_caption(
                " / ".join(dict.fromkeys(names)),
                label_pos,
                NET,
                ("node", net),
                alignment,
            )
            p = point(pos)
            marker = scene.addEllipse(
                p.x() - 2.7,
                p.y() - 2.7,
                5.4,
                5.4,
                self.pen(NET, 1.2),
                QBrush(Qt.GlobalColor.white),
            )
            marker.setData(0, "node")
            marker.setData(1, net)
            marker.setZValue(2)
            marker.setToolTip(" / ".join(dict.fromkeys(names)))
            self.net_items[net].append(marker)
        for net, pos in layout.pins():
            self.anchors.setdefault(net, pos)
            p = point(pos)
            terminal = scene.addEllipse(
                p.x() - 4,
                p.y() - 4,
                8,
                8,
                QPen(Qt.PenStyle.NoPen),
                QBrush(Qt.BrushStyle.NoBrush),
            )
            terminal.setData(0, "node")
            terminal.setData(1, net)
            terminal.setZValue(1)
            terminal.setToolTip(net)
        self.update_pins(pins or {}, fit=False)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
        self.fit()

    def update_pins(self, pins, *, fit=True):
        """Replace probe markers without recalculating the underlying layout."""
        for item in self.pin_items:
            self.scene().removeItem(item)
        self.pin_items.clear()
        for net, name in pins.items():
            if net not in self.anchors:
                continue
            x, y = self.anchors[net]
            item = self.place_caption(
                name, (x, y + 1.02), PROBE, ("pin", net), "left"
            )
            item.setToolTip(f"{name} · {net}")
            self.pin_items.append(item)
            p = point((x, y))
            marker = self.scene().addEllipse(
                p.x() - 3.6,
                p.y() - 3.6,
                7.2,
                7.2,
                self.pen(PROBE, 1.8),
                QBrush(Qt.GlobalColor.white),
            )
            marker.setData(0, "pin")
            marker.setData(1, net)
            marker.setToolTip(f"{name} · {net}")
            marker.setZValue(3)
            self.pin_items.append(marker)
        if self.anchors:
            self.scene().setSceneRect(
                self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
            )
            if fit and self.fitted:
                self.fit()

    def set_pin_tool(self, active):
        self.place_pin = active
        self.viewport().setCursor(
            Qt.CursorShape.CrossCursor if active else Qt.CursorShape.ArrowCursor
        )

    def highlight_net(self, name):
        for net, selected in ((self.highlight, False), (name, True)):
            color = ACCENT if selected else INK
            if net in self.paths:
                self.paths[net].setPen(self.pen(color, 2.6 if selected else 1.15))
            for item in self.net_items.get(net, ()):
                if isinstance(item, QGraphicsSimpleTextItem):
                    item.setBrush(ACCENT if selected else NET)
                else:
                    item.setPen(
                        self.pen(ACCENT if selected else NET, 1.8 if selected else 1.2)
                    )
        self.highlight = name

    def fit(self):
        if self.scene().items():
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.fit_scale = self.transform().m11()
            self.fitted = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit()

    def zoom(self, factor):
        scale = self.transform().m11() * factor
        if self.scene().items() and self.fit_scale / 4 < scale < max(
            80, self.fit_scale * 4
        ):
            self.fitted = False
            self.scale(factor, factor)

    def wheelEvent(self, event):
        self.zoom(1.22 if event.angleDelta().y() > 0 else 1 / 1.22)
        event.accept()

    @staticmethod
    def pen(color, width=1.55):
        pen = QPen(color)
        pen.setWidthF(width)
        pen.setCosmetic(True)
        return pen

    def lines(self, points):
        path = QPainterPath(point(points[0]))
        for p in points[1:]:
            path.lineTo(point(p))
        return self.scene().addPath(path, self.pen(INK))

    def label(self, text, p, color, probe=None, alignment="center"):
        item = self.scene().addSimpleText(
            "\n".join(text) if isinstance(text, list) else text, self.label_font
        )
        item.setBrush(color)
        item.setZValue(2)
        anchor = point(p)
        bounds = item.boundingRect()
        if alignment in {"left", "left below", "right below"}:
            offset = bounds.width() if alignment.startswith("right") else 0
            shift = 0 if alignment.endswith("below") else bounds.height()
            item.setPos(anchor.x() - offset, anchor.y() - shift)
        elif alignment == "right":
            item.setPos(anchor.x() - bounds.width(), anchor.y() - bounds.height() / 2)
        elif alignment == "bottom":
            item.setPos(anchor.x() - bounds.width() / 2, anchor.y())
        else:
            item.setPos(anchor.x() - bounds.width() / 2, anchor.y() - bounds.height())
        if probe:
            item.setData(0, probe[0])
            item.setData(1, probe[1])
            item.setToolTip(probe[1])
            if probe[0] == "node":
                self.net_items[probe[1]].append(item)
        if not probe or probe[0] != "pin":
            self.captions.append(item)
        return item

    def place_caption(self, text, p, color, probe, alignment="center"):
        """Choose a nearby position clear of component labels and other captions."""
        item = self.label(text, p, color, probe, alignment)
        base = item.pos()
        obstacles = [
            other.sceneBoundingRect().adjusted(-2, -1, 2, 1)
            for other in (*self.captions, *self.pin_items)
            if isinstance(other, QGraphicsSimpleTextItem) and other is not item
        ]
        step = item.boundingRect().height() + 2
        distance = 0
        while True:
            shifts = (0,) if distance == 0 else (-distance, distance)
            for shift in shifts:
                item.setPos(base + QPointF(0, shift * step))
                bounds = item.sceneBoundingRect()
                if any(bounds.intersects(rect) for rect in obstacles):
                    continue
                if not any(
                    isinstance(other, QGraphicsPathItem)
                    for other in self.scene().items(bounds)
                ):
                    return item
            distance += 1

    def ground(self, p, name="0"):
        x, y = p
        self.lines([(x, y), (x, y - 0.27)])
        for i in range(3):
            width = 0.32 - 0.09 * i
            yy = y - 0.27 - 0.12 * i
            self.lines([(x - width, yy), (x + width, yy)])
        self.place_caption(name, (x, y - 0.65), NET, ("node", name), "bottom")

    def symbol(self, part, a, b, top):
        if part.kind == "O":
            x, y = a

            def path(coords):
                return self.lines([(x + xx, y + yy) for xx, yy in coords])

            path([(1.4, 0.72), (1.4, -0.72), (3.1, 0), (1.4, 0.72)])
            for yy in (0.35, -0.35):
                path([(0, yy), (1.4, yy)])
            path([(3.1, 0), (4.3, 0)])
            self.label(top, (x + 1.85, y + 0.35), INK, alignment="right")
            self.label(
                "-" if top == "+" else "+", (x + 1.85, y - 0.35), INK, alignment="right"
            )
            return (x + 2.15, y + 0.77), "center"
        x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        length = math.dist(a, b)
        dx, dy = (b[0] - a[0]) / length, (b[1] - a[1]) / length

        def transform(xy):
            xx, yy = xy
            return x + xx * dx - yy * dy, y + xx * dy + yy * dx

        def path(coords):
            return self.lines(list(map(transform, coords)))

        end = 0.7
        if part.kind in "RX":
            path(
                [
                    (
                        -end + 2 * end * i / 9,
                        0 if i in (0, 9) else (0.23 if i % 2 else -0.23),
                    )
                    for i in range(10)
                ]
            )
        elif part.kind == "C":
            end = 0.13
            for xx in (-end, end):
                path([(xx, -0.35), (xx, 0.35)])
        elif part.kind == "L":
            for center in (-0.525, -0.175, 0.175, 0.525):
                path(
                    [
                        (
                            center + 0.175 * math.cos(math.pi * (1 - i / 24)),
                            0.24 * math.sin(math.pi * (1 - i / 24)),
                        )
                        for i in range(25)
                    ]
                )
        else:
            end = 0.43
            path(
                [
                    (
                        end * math.cos(math.tau * i / 50),
                        end * math.sin(math.tau * i / 50),
                    )
                    for i in range(51)
                ]
            )
            if part.kind == "I":
                path([(-0.24, 0), (0.24, 0)])
                path([(0.09, 0.11), (0.24, 0), (0.09, -0.11)])
            elif part.equation == "V = I x 0 = 0":
                self.label("0 V", (x, y - 0.18), INK)
            else:
                for side in (-0.19, 0.19):
                    xx, yy = transform((side, 0))
                    self.lines([(xx - 0.10, yy), (xx + 0.10, yy)])
                    if side < 0:
                        self.lines([(xx, yy - 0.10), (xx, yy + 0.10)])
        self.lines([a, transform((-end, 0))])
        self.lines([transform((end, 0)), b])
        if abs(dx) > abs(dy):
            return (x, y + 0.42), "center"
        return (x - 0.61, y), "right"

    def target(self, pos, nets_only=False):
        for item in self.items(pos):
            kind, name = item.data(0), item.data(1)
            if kind in {"wire", "node", "pin"} or (
                kind == "component" and not nets_only
            ):
                return kind, name
        if nets_only:
            # Allow a small screen-space tolerance around thin wire segments.
            nearby = self.items(
                pos.x() - 4, pos.y() - 4, 8, 8, Qt.ItemSelectionMode.IntersectsItemShape
            )
            names = {
                item.data(1)
                for item in nearby
                if item.data(0) in {"wire", "node", "pin"}
            }
            if len(names) == 1:
                return "node", names.pop()
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            target = self.target(event.position().toPoint(), self.place_pin)
            if target:
                kind, name = target
                if kind != "component":
                    self.highlight_net(name)
                if self.place_pin:
                    self.pin_requested.emit(name)
                else:
                    self.probed.emit("node" if kind == "wire" else kind, name)
                return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        target = self.target(event.pos(), nets_only=True)
        if not target:
            return
        kind, net = target
        menu = QMenu(self)
        if kind == "pin":
            menu.addAction("Rename pin…", lambda: self.pin_renamed.emit(net))
            menu.addAction("Remove pin", lambda: self.pin_removed.emit(net))
        else:
            menu.addAction("Add pin…", lambda: self.pin_requested.emit(net))
        menu.exec(event.globalPos())
