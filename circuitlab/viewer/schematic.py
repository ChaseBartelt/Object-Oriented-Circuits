"""PySide6 schematic scene for the shared layout model."""

import math
import re
from hashlib import sha256
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut, QTransform
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QRubberBand,
    QSlider,
    QToolButton,
    QVBoxLayout,
)

from circuitlab.layout.hierarchy import METHOD_PREFIX, hood_levels
from circuitlab.layout.geometry import BJT_PIN_OFFSET, POT_PIN_OFFSET, package_pins, pin_offset, rotate_point, symbol_pins
from circuitlab.layout.grid import align_layout
from circuitlab.layout.shorten import optimize_layout, wire_length
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.math.report import component_labels
from circuitlab.model.graph import CircuitError
from circuitlab.viewer.behavior_diagram import BehaviorDiagram
from circuitlab.viewer.behavior_scope import chip_diagram_model
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.layout.editing import ground_attachment, ground_wire_alignments, move_boundary, move_connector, move_ground, move_symbol, move_symbols, selection_ends, move_wire, split_wire, symbol_terminal_offsets, symbol_wire_alignments
from circuitlab.layout.wiring import labeled
from circuitlab.viewer.snapping import GRID_STEP, grid_delta, grid_step, on_grid, snap_radius

INK = QColor("#192633")
NET = QColor("#006565")
ACCENT = QColor("#006dab")
PROBE = QColor("#974616")
LOGIC_HIGH = QColor("#159b71")
LOGIC_LOW = QColor("#718096")
SCALE = 38

# Grid appearance. Movement spacing and magnet strength are in snapping.py.
GRID_MAJOR_STEPS = 2      # Number of snap intervals per darker square.
GRID_MAJOR_COLOR = "#c4d0da"
GRID_MINOR_COLOR = "#edf1f4"
GRID_LINE_WIDTH = 1.0


def point(p):
    return QPointF(p[0] * SCALE, -p[1] * SCALE)


@dataclass
class SchematicDrag:
    kind: str
    name: str
    key: tuple
    start: QPoint
    original: object
    circuit: object
    edge: tuple | None = None
    terminal: tuple | int | None = None
    preview: object = None
    last_position: QPoint | None = None
    pending: QPoint | None = None
    diagonal: bool = False
    align_axis: int | None = None
    alignments: dict | None = None
    snapped_offset: float | None = None
    terminal_offsets: set | None = None
    snapped_terminal: tuple | None = None
    control: bool = False
    selection: frozenset[str | tuple] = frozenset()
    caption: QGraphicsSimpleTextItem | None = None
    caption_start: QPointF | None = None
    grid_anchor: tuple = (0, 0)
    grid_step: float = 0.25
    blocked: bool = False

    def accepts_offset(self, delta):
        return all(on_grid(a + d, self.grid_step) for a, d in zip(self.grid_anchor, delta))


@dataclass
class PlacementUndo:
    mode: str
    entries: list
    offsets: dict
    records: dict | None = None
    behavior: dict | None = None


class SchematicView(QGraphicsView):
    """Paint a layout and map mouse selections back to circuit objects."""

    probed = Signal(str, str)
    pin_requested = Signal(str)
    pin_renamed = Signal(str)
    pin_removed = Signal(str)
    move_refused = Signal()
    value_requested = Signal(str)
    potentiometer_requested = Signal(str)
    switch_requested = Signal(str)
    position_changed = Signal(str, int, bool)
    history_cleared = Signal()
    optimization_finished = Signal(str)
    placement_changed = Signal()
    gate_layout_requested = Signal(str, int)
    device_layout_requested = Signal(str)
    package_edit_requested = Signal(int)
    logic_input_requested = Signal(str)

    def __init__(self, gate_layouts=None):
        super().__init__()
        self.gate_layouts = gate_layouts
        self.root_boundary = ()
        self.detail_root = None
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#ffffff"))
        self.grid_enabled = True
        self.grid_pattern = None
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.highlight = None
        self.trace_nets = set()
        self.logic_values = {}
        self.logic_simulation = None
        self.logic_sample = {}
        self.behavior_models = {}
        self.behavior_diagrams = {}
        self.behavior_placements = {}
        self.paths = {}
        self.net_items = defaultdict(list)
        self.anchors = {}
        self.pin_items = []
        self.captions = []
        self.label_offsets = {}
        self.place_pin = False
        self.source_editing = False
        self.position_name = None
        self.position_panel = QFrame(self.viewport())
        self.position_panel.setObjectName('potentiometerControl')
        self.position_panel.setStyleSheet('QFrame#potentiometerControl { background: #333; border: 1px solid #888; border-radius: 5px; } QLabel { color: #f2f2f2; background: transparent; } QSlider { background: transparent; } QToolButton { color: #f2f2f2; background: #444; border: 0; }')
        self.position_label = QLabel()
        self.position_reading = QLabel('Drag to preview; release to update code.')
        self.position_reading.setWordWrap(True)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 100)
        self.position_slider.setAccessibleName('Potentiometer position from low (%)')
        close = QToolButton()
        close.setText('×')
        close.setToolTip('Close position control')
        heading = QHBoxLayout()
        heading.addWidget(self.position_label)
        heading.addStretch()
        heading.addWidget(close)
        panel = QVBoxLayout(self.position_panel)
        panel.setContentsMargins(8, 6, 8, 6)
        panel.addLayout(heading)
        panel.addWidget(self.position_slider)
        panel.addWidget(self.position_reading)
        self.position_panel.setFixedWidth(285)
        self.position_panel.hide()
        self.position_commit = QTimer(self)
        self.position_commit.setSingleShot(True)
        self.position_commit.setInterval(300)
        self.position_commit.timeout.connect(self.finish_position)
        self.position_slider.sliderPressed.connect(self.position_commit.stop)
        self.position_slider.valueChanged.connect(self.preview_position)
        self.position_slider.sliderReleased.connect(self.finish_position)
        close.clicked.connect(self.close_position_control)
        self.circuit = None
        self.layout = None
        self.layout_mode = "auto"
        self.fitted = True
        self.fit_scale = 1.0
        self.label_font = QFont("Inter")
        self.label_font.setPixelSize(12)
        self.saved_pins = {}
        self.expanded = set()
        self.selection_key = None
        self.selected_symbols = set()
        self.selection_items = []
        self.selection_start = None
        self.selection_box = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self.selection_box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.packages = {}
        self.level_layouts = {}
        self.levels = {}
        self.manual_layouts = {}
        self.placement_records = {}
        self.rotation_bases = {}
        self.undo_history = deque(maxlen=50)
        self.undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self.undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.undo_handler = None
        self.undo_shortcut.activated.connect(self.undo)
        self.level_offsets = {}
        self.level = None
        self.drag = None
        self.drag_timer = QTimer(self)
        self.drag_timer.setSingleShot(True)
        self.drag_timer.setInterval(16)
        self.drag_timer.timeout.connect(self.paint_pending_drag)
        self.offset = QPointF()
        self.symbol_turn = None
        self.pin_grid = 0

    def set_grid_enabled(self, enabled):
        self.grid_enabled = bool(enabled)
        if self.drag is not None:
            self.drag.last_position = None
        self.viewport().update()

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if not self.grid_enabled:
            return
        spacing = grid_step(max(self.transform().m11() * SCALE, 0.01)) * SCALE
        pixels = max(4, round(spacing * self.transform().m11()))
        key = spacing, pixels, GRID_MAJOR_STEPS, GRID_MAJOR_COLOR, GRID_MINOR_COLOR, GRID_LINE_WIDTH
        if self.grid_pattern is None or self.grid_pattern[0] != key:
            # Cache a small repeating tile. Dense grids add no scene
            # items or thousands of Python QPointF allocations during a drag.
            size = pixels * GRID_MAJOR_STEPS
            tile = QPixmap(size, size)
            tile.fill(Qt.GlobalColor.white)
            tile_painter = QPainter(tile)
            tile_painter.setPen(self.pen(QColor(GRID_MINOR_COLOR), GRID_LINE_WIDTH))
            for i in range(1, GRID_MAJOR_STEPS):
                tile_painter.drawLine(i * pixels, 0, i * pixels, size)
                tile_painter.drawLine(0, i * pixels, size, i * pixels)
            tile_painter.setPen(self.pen(QColor(GRID_MAJOR_COLOR), GRID_LINE_WIDTH))
            tile_painter.drawLine(0, 0, size, 0)
            tile_painter.drawLine(0, 0, 0, size)
            tile_painter.end()
            brush = QBrush(tile)
            brush.setTransform(QTransform.fromScale(spacing / pixels, spacing / pixels))
            self.grid_pattern = key, brush
        painter.fillRect(rect, self.grid_pattern[1])

    def placements(self):
        return {**self.placement_records, **{
            key: pack_layout(layout) for key, layout in self.manual_layouts.items()
        }}

    def clear(self, *, preserve_placement=False):
        self.hide_position_control()
        self.placement_records = self.placements() if preserve_placement else {}
        if not preserve_placement:
            self.behavior_placements.clear()
        self.logic_values.clear()
        self.logic_simulation = None
        self.logic_sample.clear()
        self.behavior_models.clear()
        self._clear_scene()
        self.circuit = None
        self.layout = None
        self.detail_root = None
        self.expanded.clear()
        self.selection_key = None
        self.selected_symbols.clear()
        self.selection_start = None
        self.selection_box.hide()
        self.level_layouts.clear()
        self.manual_layouts.clear()
        self.rotation_bases.clear()
        if self.undo_handler is None:
            self.undo_history.clear()
        self.history_cleared.emit()
        self.drag = None
        self.drag_timer.stop()

    def _clear_scene(self):
        if self.drag is not None and self.drag.caption is not None:
            self.drag_timer.stop()
            self.drag = None
        self.scene().clear()
        for items in (self.selection_items, self.paths, self.net_items, self.anchors,
                      self.pin_items, self.captions, self.packages, self.levels, self.level_offsets,
                      self.behavior_diagrams):
            items.clear()
        self.highlight = None

    def draw(self, circuit, pins=None, *, boundary=(), layout=None, detail_root=None):
        """Open a circuit, or isolate one package for shared layout editing."""
        if circuit is not self.circuit:
            self.clear(preserve_placement=True)
        self.circuit = circuit
        self.saved_pins = pins or {}
        self.root_boundary = boundary
        self.detail_root = detail_root
        if detail_root is not None:
            self.expanded.add(detail_root)
        if layout is not None:
            self.level_layouts[None, self.layout_mode] = layout
        self._redraw()

    def refresh_gate_layouts(self):
        self.level_layouts = {
            key: layout for key, layout in self.level_layouts.items()
            if not (key[0] and key[0].startswith(METHOD_PREFIX))
        }
        self._redraw()

    def _redraw(self, focus=None, *, preview=None, selected=None):
        circuit = self.circuit
        offsets = dict(self.level_offsets) if preview is not None or focus is not None else {}
        self._clear_scene()
        if circuit is None or (not circuit.components and not circuit.instances):
            self.layout = None
            return
        self.offset = QPointF()
        for name, view, body, boundary in hood_levels(circuit, self.expanded):
            if self.detail_root is not None:
                if name != self.detail_root:
                    continue
                name = None
            self.level = name
            if name is None:
                boundary = self.root_boundary
            package_layout = any(part.kind == "H" for part in view.components)
            key = name, self.layout_mode
            if key in self.placement_records:
                saved = restore_layout(self.placement_records.pop(key), view, boundary)
                if saved is not None:
                    self.manual_layouts[key] = saved
            if key not in self.level_layouts:
                layout = None
                if self.gate_layouts is not None and name:
                    instance = circuit.instances[int(name.removeprefix(METHOD_PREFIX))]
                    layout = self.gate_layouts.for_instance(
                        instance, view, boundary, use_default=self.layout_mode != "labeled",
                    )
                if layout is None:
                    labels = component_labels(view)
                    if self.layout_mode == "labeled":
                        layout = labeled(view, labels)
                    elif name is not None or package_layout or boundary:
                        layout = fast_layout(view, labels, boundary=boundary)
                    else:
                        layout = schematic_layout(view, labels, self.layout_mode)
                    # Only newly generated layouts change automatically. Restored
                    # placements and shared gate designs retain their geometry.
                    layout = align_layout(view, layout, GRID_STEP) or layout
                self.level_layouts[key] = layout
            layout = self.manual_layouts.get(key, self.level_layouts[key])
            if preview is not None and key == preview.key:
                layout = preview.preview or layout
            self.levels[key] = view
            if name is None:
                self.layout = layout
                self._paint_level(view, layout)
                for net, pos in layout.boundary.items():
                    if net not in view.grounds:
                        self.paint_boundary(net, pos, net)
            else:
                self._paint_details(name, view, layout, body, offsets)
        self.offset = QPointF()
        self.level = None
        self.label_offsets = self.layout.label_offsets
        self.paint_selection(preview)
        self.update_pins(self.saved_pins, fit=False, resize_scene=False)
        for net in self.logic_values:
            self.paint_highlight(net, False)
        scene = self.scene()
        if preview is not None:
            if selected:
                self.highlight_net(selected)
            return
        bounds = scene.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if focus is not None and not self.fitted:
            # A smaller scene would be automatically recentered by QGraphicsView,
            # shifting the grid under the pointer when a move is released.
            bounds = bounds.united(self.mapToScene(self.viewport().rect()).boundingRect())
        scene.setSceneRect(bounds)
        if focus is not None and not self.fitted:
            self.centerOn(focus)
        else:
            self.fit()
        if selected:
            self.highlight_net(selected)
        for net in self.trace_nets:
            self.paint_highlight(net, True)
        self.anchor_position_control()

    def show_position_control(self, name, position):
        self.position_commit.stop()
        self.position_name = name
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(round(position * 100))
        self.position_slider.blockSignals(False)
        self.position_label.setText(f'{name} · {self.position_slider.value()}% from low')
        self.position_reading.setText('Drag to preview; release to update code.')
        self.position_panel.adjustSize()
        self.position_panel.show()
        self.position_panel.raise_()
        self.anchor_position_control()

    def anchor_position_control(self):
        if not self.position_name or self.position_panel.isHidden():
            return
        item = next((item for item in self.scene().items()
                     if item.data(0) == 'component' and item.data(1) == self.position_name), None)
        if item is None:
            self.hide_position_control()
            return
        anchor = self.mapFromScene(item.sceneBoundingRect().bottomRight()) + QPoint(12, 12)
        self.position_panel.move(max(0, min(anchor.x(), self.viewport().width() - self.position_panel.width())),
                                 max(0, min(anchor.y(), self.viewport().height() - self.position_panel.height())))

    def preview_position(self, percent):
        if self.position_name:
            self.position_label.setText(f'{self.position_name} · {percent}% from low')
            self.position_changed.emit(self.position_name, percent, False)
            if not self.position_slider.isSliderDown():
                self.position_commit.start()

    def finish_position(self):
        self.position_commit.stop()
        if self.position_name:
            self.position_changed.emit(self.position_name, self.position_slider.value(), True)

    def hide_position_control(self):
        self.position_commit.stop()
        self.position_panel.hide()
        self.position_name = None

    def close_position_control(self):
        self.finish_position()
        self.hide_position_control()

    def preview_potentiometers(self, circuit):
        """Refresh value glyphs using the current geometry, including expanded packages."""
        parts = {part.name: part for part in circuit.components}
        self.circuit = circuit
        for layouts in (self.level_layouts, self.manual_layouts):
            for key, layout in layouts.items():
                layouts[key] = replace(layout, symbols=[(parts.get(part.name, part), a, b, top)
                                                       for part, a, b, top in layout.symbols])
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=self.highlight)

    def mapped(self, position):
        if self.symbol_turn is not None:
            position = rotate_point(position, *self.symbol_turn)
        return point(position) + self.offset

    def _paint_level(self, circuit, layout):
        scene = self.scene()
        self.label_offsets = layout.label_offsets
        self.pin_grid = layout.pin_grid
        self.level_offsets[self.level, self.layout_mode] = QPointF(self.offset)
        labels = component_labels(circuit)
        for name in layout.wires:
            path = self.paths[name].path() if name in self.paths else QPainterPath()
            for a, b in layout.visible_segments(name):
                path.moveTo(self.mapped(a))
                path.lineTo(self.mapped(b))
            if name in self.paths:
                self.paths[name].setPath(path)
                continue
            item = scene.addPath(path, self.pen(INK, 1.15))
            self.paths[name] = item
            item.setData(0, "wire")
            item.setData(1, name)
            item.setToolTip(name)
        for net, dots in layout.junctions.items():
            for dot in dots:
                p = self.mapped(dot)
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
            turns = layout.rotations.get(part.name, 0)
            self.symbol_turn = (a, b, turns) if turns else None
            if part.kind == "H":
                number_pins = part.name.startswith(METHOD_PREFIX) and (
                    circuit.instances[int(part.name.removeprefix(METHOD_PREFIX))].kind == "chip"
                )
                if not part.name.startswith(METHOD_PREFIX) and part.name in self.expanded:
                    self.package(part, a, b, number_pins=False, transistor_open=True)
                else:
                    self.package(part, a, b, number_pins=number_pins)
                self.symbol_turn = None
                continue
            counts[part.kind] += 1
            name = labels[part.name]
            if len(name[0]) > 24:
                prefix = "U" if part.kind == "O" else part.kind
                name = [f"{prefix}#{counts[part.kind]}", *name[1:]]
            pos, alignment = self.symbol(part, a, b, top)
            powered_amp = part.kind == "O" and bool(part.vplus and part.vminus)
            if part.kind == "P":
                height = pin_offset(POT_PIN_OFFSET, self.pin_grid)
                first, second = self.mapped((a[0], a[1] - height)), self.mapped((b[0], b[1] + height))
            elif powered_amp:
                pins = [self.mapped(pin) for _, pin in symbol_pins(part, a, b, top, grid=self.pin_grid)]
                first = QPointF(min(pin.x() for pin in pins), min(pin.y() for pin in pins))
                second = QPointF(max(pin.x() for pin in pins), max(pin.y() for pin in pins))
            else:
                first, second = self.mapped(a), self.mapped(b)
            margin_x, margin_y = (24, 24) if part.kind == "K" else (8, 14)
            bounds = QRectF(first, second).normalized().adjusted(-margin_x, -margin_y, margin_x, margin_y)
            handle = scene.addRect(
                bounds, QPen(Qt.PenStyle.NoPen), QBrush(QColor(0, 0, 0, 0))
            )
            handle.setZValue(0.5)
            self.drag_handle(handle, "component", part.name)
            if part.kind == "K":
                state = "closed" if part.closed else "open"
                hint = " · Click to toggle; drag to move" if self.source_editing else ""
                handle.setToolTip(f"{part.label or part.name} · {state}{hint}")
                if self.source_editing:
                    handle.setCursor(Qt.CursorShape.PointingHandCursor)
            if turns:
                start, end = (rotate_point(pin, a, b, turns) for pin in (a, b))
                x, y = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
                padding = pin_offset(POT_PIN_OFFSET, self.pin_grid) + 0.25
                if start[0] == end[0]:
                    pos, alignment = (x - (1.5 if powered_amp else padding if part.kind == "P" else 0.61), y), "right"
                else:
                    pos, alignment = (x, y + (1.65 if powered_amp else padding if part.kind == "P" else 0.65)), "center"
            self.symbol_turn = None
            component_captions.append((name, pos, ("component", part.name), alignment))
        for pos in layout.grounds:
            net = layout.ground_names.get(pos, "0")
            self.ground(pos, net, layout.ground_rotations.get(pos, 0))
            self.anchors.setdefault(net, self.scene_position(pos))
        for name, pos, probe, alignment in component_captions:
            self.place_caption(name, pos, INK, probe, alignment, move_key=("component", probe[1]))
        for name, pos in layout.labels.items():
            if (
                pos not in layout.grounds
                and name not in tap_names
                and not name.startswith("@")
                and not re.fullmatch(r"g\d+_\d+", name)
            ):
                if "." not in name:
                    self.place_caption(
                        name.replace("_", " "), (pos[0], pos[1] + 0.17), NET, ("node", name),
                        move_key=("net", name),
                    )
                self.anchors.setdefault(name, self.scene_position(pos))
        for index, (net, pos, name, left, below) in enumerate(layout.connectors):
            p = self.mapped(pos)
            item = scene.addEllipse(
                p.x() - 2.3,
                p.y() - 2.3,
                4.6,
                4.6,
                self.pen(NET, 1.2),
                QBrush(Qt.GlobalColor.white),
            )
            item.setZValue(1.1)
            self.net_items[net].append(item)
            caption = self.place_caption(
                name,
                (pos[0] + (-0.16 if left else 0.16), pos[1]),
                NET,
                ("node", net),
                ("right" if left else "left") + (" below" if below else ""),
                move_key=("connector", *pos),
            )
            for handle in (item, caption):
                self.drag_handle(handle, "connector", net)
                handle.setData(3, index)
                handle.setToolTip(net)
            self.anchors.setdefault(net, self.scene_position(pos))
        symbols = {
            part.name: tuple(rotate_point(p, a, b, layout.rotations.get(part.name, 0))
                             for p in (a, b))
            for part, a, b, _ in layout.symbols
        }
        connector_names = {(net, name) for net, _, name, _, _ in layout.connectors}
        tap_points = defaultdict(list)
        for tap in circuit.wire_taps:
            if (tap.net, tap.name.replace("_", " ")) in connector_names:
                continue
            pos = symbols[tap.component][tap.terminal]
            tap_points[tap.net, pos].append(tap.name)
        for (net, pos), names in tap_points.items():
            self.anchors.setdefault(net, self.scene_position(pos))
            x, y = pos
            alignment = "left" if pos in layout.grounds else "center"
            label_pos = (x + 0.45, y + 0.14) if alignment == "left" else (x, y + 0.17)
            self.place_caption(
                " / ".join(dict.fromkeys(names)),
                label_pos,
                NET,
                ("node", net),
                alignment,
                move_key=("tap", *names),
            )
            p = self.mapped(pos)
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
            self.anchors.setdefault(net, self.scene_position(pos))
            p = self.mapped(pos)
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

    def scene_position(self, pos):
        p = self.mapped(pos)
        return p.x() / SCALE, -p.y() / SCALE

    def drag_handle(self, item, kind, name):
        item.setData(0, kind)
        item.setData(1, name)
        item.setData(2, (self.level, self.layout_mode))
        item.setCursor(Qt.CursorShape.OpenHandCursor)

    def paint_boundary(self, net, pos, name):
        p = self.mapped(pos)
        marker = self.scene().addEllipse(
            p.x() - 3, p.y() - 3, 6, 6, self.pen(NET, 1.2), QBrush(Qt.GlobalColor.white)
        )
        marker.setZValue(3)
        self.net_items[net].append(marker)
        caption = self.place_caption(
            name, (pos[0] - 0.22, pos[1]), NET, ("node", net), "right",
            move_key=("boundary", net),
        )
        for handle in (marker, caption):
            self.drag_handle(handle, "boundary", net)
        marker.setToolTip(f"Drag pin {name}")
        self.anchors.setdefault(net, self.scene_position(pos))

    def paint_selection(self, preview=None):
        for item in self.selection_items:
            self.scene().removeItem(item)
        self.selection_items.clear()
        key = self.selection_key
        if preview is not None and preview.selection:
            key, names = preview.key, preview.selection
        else:
            names = self.selected_symbols
        if key not in self.level_offsets or not names:
            return
        layout = self.manual_layouts.get(key, self.level_layouts[key])
        if preview is not None and preview.key == key:
            layout = preview.preview or layout
        offset = self.level_offsets[key]
        pen = self.pen(ACCENT, 1.5)
        for identity, net, pos in selection_ends(layout):
            if identity in names:
                center = point(pos) + offset
                self.selection_items.append(self.scene().addEllipse(center.x()-6, center.y()-6, 12, 12, pen))
        for part, a, b, _ in layout.symbols:
            if part.name not in names:
                continue
            turns = layout.rotations.get(part.name, 0)
            height = (max(0.8, (len(part.package_pins) + 1) // 2 / 2 + 0.4) if part.kind == "H"
                      else pin_offset(POT_PIN_OFFSET, layout.pin_grid) if part.kind == "P" else 0.8)
            corners = ((min(a[0], b[0]), min(a[1], b[1]) - height),
                       (max(a[0], b[0]), max(a[1], b[1]) + height))
            first, second = (point(rotate_point(c, a, b, turns)) + offset for c in corners)
            rect = QRectF(first, second).normalized().adjusted(-4, -3, 4, 3)
            self.selection_items.append(self.scene().addRect(rect, pen))

    @staticmethod
    def selection_id(kind, name, terminal=None):
        if kind == "ground":
            return kind, tuple(terminal)
        if kind == "connector":
            return kind, terminal
        if kind == "boundary":
            return kind, name
        return name

    def clear_selection(self):
        if self.selected_symbols:
            self.selected_symbols.clear()
            self.selection_key = None
            self.paint_selection()

    def select_box(self, bounds):
        candidates = defaultdict(set)
        for key, offset in self.level_offsets.items():
            drawing = self.manual_layouts.get(key, self.level_layouts[key])
            for identity, net, pos in selection_ends(drawing):
                if bounds.contains(self.mapFromScene(point(pos) + offset)):
                    candidates[key].add(identity)
            for part, a, b, _ in drawing.symbols:
                center = point(tuple((first + second) / 2 for first, second in zip(a, b)))
                if bounds.contains(self.mapFromScene(center + offset)):
                    candidates[key].add(part.name)
        if not candidates:
            self.clear_selection()
            return
        key = self.selection_key
        if key not in candidates:
            key = max(candidates, key=lambda level: len(candidates[level]))
            self.selected_symbols.clear()
        self.selection_key = key
        self.selected_symbols.update(candidates[key])
        self.paint_selection()

    def package(self, part, a, b, *, number_pins, transistor_open=False):
        x, y = a
        pins = list(package_pins(part, self.pin_grid))
        height = max(0.8, (len(part.package_pins) + 1) // 2 / 2 + 0.2,
                     max((abs(offset) for _, _, _, offset in pins), default=0) + 0.2)
        left, right = x + 0.55, b[0] - 0.55
        top = self.mapped((left, y + height))
        bottom = self.mapped((right, y - height))
        bounds = QRectF(top, bottom).normalized()
        rect = self.scene().addRect(
            bounds, self.pen(INK), QBrush(QColor("#eaf1f5")),
        )
        self.drag_handle(rect, "hood", part.name)
        tooltip = f"{'Close' if part.name in self.expanded else 'Open'} {part.label}"
        rect.setToolTip(tooltip)
        self.packages[part.name] = (part, rect.rect())
        title = self.label(
            part.label, ((left + right) / 2, y + height + 0.1), INK, ("hood", part.name),
            move_key=("component", part.name),
        )
        title.setToolTip(tooltip)
        self.drag_handle(title, "hood", part.name)
        if transistor_open:
            self.transistor_symbol(part, a, b)
            return
        for index, (pin, net, on_left, offset) in enumerate(pins, 1):
            pin_y = y + offset
            self.lines([
                ((x if on_left else b[0]), pin_y),
                ((left if on_left else right), pin_y),
            ])
            caption = pin
            if number_pins:
                caption = f"{index}  {pin}" if on_left else f"{pin}  {index}"
            label = self.label(
                caption, ((left + 0.2 if on_left else right - 0.2), pin_y),
                NET, ("node", net), "left" if on_left else "right",
            )
            if on_left:
                label.moveBy(0, label.boundingRect().height() / 2)
            if self.symbol_turn is not None:
                self.rotate_caption(label, self.symbol_turn[2])
            self.movable_caption(label, ("terminal", part.name, index))
        if self.symbol_turn is not None:
            self.rotate_caption(title, self.symbol_turn[2])

    @staticmethod
    def rotate_caption(label, turns):
        label.setTransformOriginPoint(label.boundingRect().center())
        label.setRotation(turns * 90)

    def _paint_details(self, name, circuit, layout, body, offsets):
        part, package = self.packages[name]
        scene = self.scene()
        # Leave the package and its external wiring in place.
        bounds = scene.itemsBoundingRect()
        top = bounds.bottom() + SCALE * 1.2
        points = [point(p) for _, a, b, _ in layout.symbols for p in (a, b)]
        points.extend(
            point(p) for wires in layout.wires.values() for edge in wires for p in edge
        )
        local = QRectF()
        if points:
            xs = [p.x() for p in points]
            ys = [p.y() for p in points]
            local = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        margins = SCALE * 1.8
        left = max(bounds.left(), package.center().x() - local.width() / 2 - margins)
        self.offset = QPointF(left + margins - local.left(), top + margins * 1.4 - local.top())
        if (previous := offsets.get((name, self.layout_mode))) is not None:
            # Keep an expanded chip's contents anchored while a part is dragged.
            self.offset = QPointF(previous)
        if layout.pin_grid:
            spacing = layout.pin_grid * SCALE
            self.offset = QPointF(*(round(value / spacing) * spacing for value in (self.offset.x(), self.offset.y())))
        left, top = self.offset.x() + local.left() - margins, self.offset.y() + local.top() - margins * 1.4
        self._paint_level(circuit, layout)
        header = self.label(part.label, (0, 0), INK, ("hood", name), "left below")
        header.setPos(left + 12, top + 8)
        header.setToolTip(f"Close {part.label}")
        if body:
            contents = self._paint_behavior(name)
            contents_top = max(top + 35, self.offset.y() + local.bottom() + SCALE * .8) if circuit.components else top + 35
            contents.setPos(left + 12, contents_top)
        pin_names = defaultdict(list)
        for pin, net in part.package_pins:
            pin_names[net].append(pin)
        for net, pos in layout.boundary.items():
            if net not in circuit.grounds:
                caption = " / ".join(pin_names[net]) or net
                self.paint_boundary(net, pos, caption)
        area = QRectF(
            left, top, max(header.boundingRect().width() + 24, local.width() + margins * 2),
            local.height() + margins * 2.5,
        )
        if body:
            area.setWidth(max(area.width(), contents.boundingRect().width() + 24))
            area.setHeight(max(area.height(), contents.pos().y() - top + contents.boundingRect().height() + 12))
        border = scene.addRect(area, self.pen(QColor("#97aebf")), QBrush(QColor(246, 249, 251, 90)))
        border.setZValue(-2)
        if body and isinstance(contents, BehaviorDiagram):
            def resize_details():
                border.setRect(area.united(contents.sceneBoundingRect().adjusted(-12, -12, 12, 12)))
                self.fitted = False
                visible = self.mapToScene(self.viewport().rect()).boundingRect()
                self.setSceneRect(self.sceneRect().united(border.rect()).united(visible))
            contents.geometry_changed.connect(resize_details)
            border.setRect(area.united(contents.sceneBoundingRect().adjusted(-12, -12, 12, 12)))
        leader = QPainterPath(QPointF(package.center().x(), package.bottom()))
        edge = package.right() + 8
        leader.lineTo(edge, package.bottom() + 9)
        leader.lineTo(edge, top - 10)
        leader.lineTo(left + 12, top)
        pen = self.pen(QColor("#8ba3b4"), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        scene.addPath(leader, pen).setZValue(-1)
        self.offset = QPointF()

    def _paint_behavior(self, name):
        """Show compiled functional blocks, leaving source code in the IDE."""
        if name not in self.behavior_models:
            try:
                index = int(name.removeprefix(METHOD_PREFIX))
                self.behavior_models[name] = chip_diagram_model(self.circuit, index, self.logic_simulation)
            except (CircuitError, ValueError) as error:
                self.behavior_models[name] = str(error)
        model = self.behavior_models[name]
        if isinstance(model, str):
            item = self.scene().addSimpleText('Functional diagram unavailable; inspect the simulator diagnostic.')
            item.setToolTip(model)
            return item
        model, bindings = model
        diagram = BehaviorDiagram(model)
        if record := self.behavior_placements.get(self._behavior_key(name)):
            diagram.restore_placement(record)
        diagram.set_values({local: self.logic_sample[live] for local, live in bindings.items()
                            if live in self.logic_sample})
        diagram.signal_probed.connect(lambda local: self.probed.emit('node', bindings.get(local, local)))
        diagram.input_toggled.connect(lambda local: self._toggle_logic_input(bindings.get(local, local)))
        diagram.placement_changed.connect(lambda before, after: self._behavior_changed(name, before, after))
        self.scene().addItem(diagram)
        self.behavior_diagrams[name] = diagram
        return diagram

    def _behavior_key(self, name):
        method = self.circuit.instances[int(name.removeprefix(METHOD_PREFIX))]
        identity = sha256(repr((method.name, method.pins, method.signal_bindings)).encode()).hexdigest()[:16]
        return f'{getattr(self.logic_simulation, "profile", "ooc")}:{method.name}:{identity}'

    def logic_placements(self):
        return dict(self.behavior_placements)

    def load_logic_placements(self, records):
        self.behavior_placements = {key: value for key, value in records.items()
                                    if isinstance(key, str) and isinstance(value, dict)} if isinstance(records, dict) else {}

    def _behavior_changed(self, name, before, after):
        self.undo_history.append(PlacementUndo(self.layout_mode, [], dict(self.level_offsets),
                                               behavior=dict(self.behavior_placements)))
        self.behavior_placements[self._behavior_key(name)] = after
        self.placement_changed.emit()

    def _toggle_logic_input(self, name):
        if self.logic_simulation is not None and name in self.logic_simulation.inputs:
            self.logic_input_requested.emit(name)
        else:
            self.probed.emit('node', name)

    def set_logic_simulation(self, simulation, values):
        changed = simulation is not self.logic_simulation
        self.logic_simulation = simulation
        self.logic_sample = dict(values)
        if changed:
            self.behavior_models.clear()
            if self.behavior_diagrams:
                self._redraw(self.mapToScene(self.viewport().rect().center()))
        for name, diagram in self.behavior_diagrams.items():
            _, bindings = self.behavior_models[name]
            diagram.set_values({local: values[live] for local, live in bindings.items() if live in values})

    def toggle_package(self, name):
        if self.detail_root is not None and name.startswith(METHOD_PREFIX):
            self.package_edit_requested.emit(int(name.removeprefix(METHOD_PREFIX)))
            return
        if name.startswith(METHOD_PREFIX) and not self.circuit.instances[
            int(name.removeprefix(METHOD_PREFIX))
        ].model_available:
            return
        focus = self.mapToScene(self.viewport().rect().center())
        if name in self.expanded:
            self.expanded.remove(name)
        else:
            self.expanded.add(name)
        self._redraw(focus)

    def redraw(self, pins):
        if self.circuit is not None:
            self.saved_pins = pins
            self._redraw(selected=self.highlight)

    def reset_placement(self):
        if not self.isEnabled() or self.drag is not None:
            return
        if self.manual_layouts or self.placement_records:
            self.remember_placements(self.level_layouts.keys() | self.manual_layouts.keys())
            self.undo_history[-1].records = self.placement_records.copy()
            self.manual_layouts.clear()
            self.placement_records.clear()
            self.rotation_bases.clear()
            self._redraw()
            self.placement_changed.emit()

    def remember_placement(self, key):
        self.remember_placements([key])

    def remember_placements(self, keys):
        self.undo_history.append(PlacementUndo(self.layout_mode, [
            (key, self.manual_layouts.get(key, self.level_layouts.get(key)), self.rotation_bases.get(key))
            for key in keys
        ], dict(self.level_offsets)))

    def undo(self):
        self.undo_placement()

    def align_to_grid(self):
        """Align all displayed levels atomically and retain a single undo step."""
        if not self.isEnabled() or self.drag is not None or not self.levels:
            return True
        changed = {}
        for key, circuit in self.levels.items():
            current = self.manual_layouts.get(key, self.level_layouts[key])
            aligned = align_layout(circuit, current, GRID_STEP)
            if aligned is None:
                return False
            if aligned != current:
                changed[key] = aligned
        if not changed:
            return True
        self.remember_placements(changed)
        self.manual_layouts.update(changed)
        for key in changed:
            self.rotation_bases.pop(key, None)
        focus = self.mapToScene(self.viewport().rect().center())
        self.fitted = False
        self._redraw(focus, selected=self.highlight)
        self.placement_changed.emit()
        return True

    def optimize_wiring(self):
        """Shorten displayed wiring explicitly, with one undo step for all levels."""
        if not self.isEnabled() or self.drag is not None or not self.levels:
            return
        changed = {}
        before_length = after_length = 0
        self.viewport().setCursor(Qt.CursorShape.WaitCursor)
        try:
            for key, circuit in self.levels.items():
                current = self.manual_layouts.get(key, self.level_layouts[key])
                optimized = optimize_layout(circuit, current, move_parts=True, grid=GRID_STEP)
                before_length += wire_length(current)
                after_length += wire_length(optimized)
                if optimized != current:
                    changed[key] = optimized
        finally:
            self.viewport().unsetCursor()
        if not changed:
            self.optimization_finished.emit('No safe wiring improvement found. Layout kept as it was.')
            return
        self.remember_placements(changed)
        self.manual_layouts.update(changed)
        for key in changed:
            self.rotation_bases.pop(key, None)
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=self.highlight)
        self.placement_changed.emit()
        shorter = ''
        if before_length > after_length + 1e-8:
            reduction = 100 * (before_length - after_length) / before_length
            shorter = f' ({reduction:.1f}% shorter)'
        self.optimization_finished.emit(f'Wiring simplified{shorter}. Ctrl+Z restores the previous layout.')

    def undo_placement(self):
        if self.undo_handler is not None:
            self.undo_handler()
        elif self.drag is None and self.undo_history:
            self.restore_placement(self.undo_history.pop())

    def restore_placement(self, entry):
        if self.drag is not None:
            return
        self.layout_mode = entry.mode
        if entry.behavior is not None:
            self.behavior_placements = entry.behavior.copy()
        if entry.records is not None:
            self.placement_records = entry.records.copy()
        for key, layout, rotation in entry.entries:
            for placements, previous in (
                (self.manual_layouts, layout), (self.rotation_bases, rotation),
            ):
                if previous is None:
                    placements.pop(key, None)
                else:
                    placements[key] = previous
        self.level_offsets = entry.offsets
        focus = self.mapToScene(self.viewport().rect().center())
        self._redraw(focus, selected=self.highlight)
        self.placement_changed.emit()

    def update_pins(self, pins, *, fit=True, resize_scene=True):
        """Replace probe markers without recalculating the underlying layout."""
        self.saved_pins = pins
        for item in self.pin_items:
            self.scene().removeItem(item)
        self.pin_items.clear()
        for net, name in pins.items():
            if net not in self.anchors:
                continue
            x, y = self.anchors[net]
            item = self.place_caption(
                name, (x, y + 1.02), PROBE, ("pin", net), "left",
                move_key=("probe", net),
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
        if self.anchors and resize_scene:
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
        self.paint_highlight(self.highlight, False)
        self.paint_highlight(name, True)
        self.highlight = name

    def paint_highlight(self, net, selected):
        level_color = (
            LOGIC_HIGH if self.logic_values[net] else LOGIC_LOW
        ) if net in self.logic_values else None
        color = ACCENT if selected else level_color or INK
        label_color = ACCENT if selected else level_color or NET
        if net in self.paths:
            self.paths[net].setPen(self.pen(color, 2.6 if selected else 1.15))
        for item in self.net_items.get(net, ()):
            if item.data(0) == "ground_stroke":
                item.setPen(self.pen(color, 2.6 if selected else 1.55))
            elif isinstance(item, QGraphicsSimpleTextItem):
                item.setBrush(label_color)
            else:
                item.setPen(self.pen(label_color, 1.8 if selected else 1.2))

    def set_logic_values(self, values):
        """Overlay simulated 0/1 levels without changing layout or source."""
        previous = self.logic_values
        self.logic_values = {
            name: value for name, value in values.items()
            if (name in self.paths or name in self.net_items) and value in (0, 1)
        }
        for net in previous.keys() | self.logic_values.keys():
            self.paint_highlight(net, net == self.highlight or net in self.trace_nets)

    def probe_wire(self, net):
        self.highlight_net(None if net == self.highlight else net)
        self.probed.emit("node", net)

    def fit(self):
        if self.layout is not None:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.fit_scale = self.transform().m11()
            self.fitted = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit()

    def zoom(self, factor):
        scale = self.transform().m11() * factor
        if self.layout is not None and self.fit_scale / 4 < scale < max(
            80, self.fit_scale * 4
        ):
            self.fitted = False
            self.scale(factor, factor)
            self.anchor_position_control()

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
        path = QPainterPath(self.mapped(points[0]))
        for p in points[1:]:
            path.lineTo(self.mapped(p))
        return self.scene().addPath(path, self.pen(INK))

    def label(self, text, p, color, probe=None, alignment="center", *, move_key=None):
        item = self.scene().addSimpleText(
            "\n".join(text) if isinstance(text, list) else text, self.label_font
        )
        item.setBrush(color)
        item.setZValue(2)
        anchor = self.mapped(p)
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
        if move_key is not None:
            self.movable_caption(item, move_key)
        return item

    def movable_caption(self, item, key):
        item.setData(2, (self.level, self.layout_mode))
        item.setData(4, key)
        item.setData(5, item.pos())
        item.setCursor(Qt.CursorShape.OpenHandCursor)
        if key in self.label_offsets:
            item.setPos(item.pos() + point(self.label_offsets[key]))

    def place_caption(self, text, p, color, probe, alignment="center", *, move_key=None):
        """Choose a nearby position clear of component labels and other captions."""
        item = self.label(text, p, color, probe, alignment, move_key=move_key)
        if move_key in self.label_offsets:
            return item
        base = item.pos()
        obstacles = [
            other.sceneBoundingRect().adjusted(-2, -1, 2, 1)
            for other in (*self.captions, *self.pin_items)
            if isinstance(other, QGraphicsSimpleTextItem) and other is not item
        ]
        step = item.boundingRect().height() + 2
        for distance in range(4):
            shifts = (0,) if distance == 0 else (-distance, distance)
            for shift in shifts:
                item.setPos(base + QPointF(0, shift * step))
                bounds = item.sceneBoundingRect()
                if any(bounds.intersects(rect) for rect in obstacles):
                    continue
                if not any(
                    other.data(0) == "wire" for other in self.scene().items(bounds)
                ):
                    return item
        item.setPos(base)
        return item

    def ground(self, p, name="0", turns=0):
        x, y = p

        def turned(position):
            return rotate_point(position, p, p, turns)

        strokes = [self.lines([p, turned((x, y - 0.27))])]
        for i in range(3):
            width = 0.32 - 0.09 * i
            yy = y - 0.27 - 0.12 * i
            strokes.append(self.lines([turned((x - width, yy)), turned((x + width, yy))]))
        for stroke in strokes:
            stroke.setData(0, "ground_stroke")
        self.net_items[name].extend(strokes)
        caption = self.place_caption(
            name, turned((x, y - 0.65)), NET, ("node", name),
            ("bottom", "right", "center", "left")[turns],
            move_key=("ground", *p),
        )
        bounds = QRectF(
            self.mapped(turned((x - 0.42, y))), self.mapped(turned((x + 0.42, y - 0.65)))
        ).normalized()
        handle = self.scene().addRect(
            bounds, QPen(Qt.PenStyle.NoPen), QBrush(QColor(0, 0, 0, 0))
        )
        handle.setZValue(1.1)
        for item in (caption, handle):
            self.drag_handle(item, "ground", name)
            item.setData(3, p)
            item.setToolTip(name)

    def symbol(self, part, a, b, top):
        if part.kind == "O":
            x, y = a

            def path(coords):
                return self.lines([(x + xx, y + yy) for xx, yy in coords])

            path([(1.4, 0.72), (1.4, -0.72), (3.1, 0), (1.4, 0.72)])
            offset = pin_offset(0.35, self.pin_grid)
            for yy in (offset, -offset):
                path([(0, yy), (1.4, yy)])
            self.lines([(x + 3.1, y), b])
            self.label(top, (x + 1.85, y + offset), INK, alignment="right")
            self.label(
                "-" if top == "+" else "+", (x + 1.85, y - offset), INK, alignment="right"
            )
            if part.vplus and part.vminus:
                power_x = (a[0] + b[0]) / 2
                pitch = pin_offset(1.25, self.pin_grid)
                # Meet the triangle's sloping sides and keep the exposed ends
                # identical to symbol_pins(), including rotated symbols.
                edge = .72 * (x + 3.1 - power_x) / 1.7
                for net, sign, direction in ((part.vplus, "+V", 1), (part.vminus, "-V", -1)):
                    lead = self.lines([(power_x, y + direction * edge), (power_x, y + direction * pitch)])
                    lead.setData(0, "node")
                    lead.setData(1, net)
                    lead.setZValue(1.1)
                    lead.setToolTip(f"{sign} · {net}")
                    self.net_items[net].append(lead)
                    self.label(sign, (power_x + .16, y + (.95 if direction > 0 else -.65)),
                               NET, ("node", net), alignment="left below")
                return (x + 2.15, y + 1.65), "center"
            return (x + 2.15, y + 0.77), "center"
        if part.kind == "Q":
            return self.transistor_symbol(part, a, b)
        if part.kind == "P":
            return self.potentiometer_symbol(part, a, b)
        if part.kind == "S":
            x, y = a
            right = b[0]
            self.lines([(x + 0.8, y + 0.95), (right - 0.8, y + 0.95),
                        (right - 0.8, y - 0.95), (x + 0.8, y - 0.95),
                        (x + 0.8, y + 0.95)])
            pitch = pin_offset(0.6, self.pin_grid)
            for offset, sign in ((pitch, "+"), (-pitch, "-")):
                self.lines([(x, y + offset), (x + 0.8, y + offset)])
                self.lines([(right - 0.8, y + offset), (right, y + offset)])
                self.label(sign, (x + 1.1, y + offset), INK)
            self.lines([(right - 1.3, y - 0.55), (right - 1.9, y + 0.47)])
            return ((x + right) / 2, y + 1.0), "center"
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
        elif part.kind == "K":
            # Two contacts and a lever distinguish a manual switch from the
            # four-terminal, voltage-controlled switch model above.
            end = 0.65
            path([(-end, 0), (end, 0 if part.closed else 0.48)])
            for xx in (-end, end):
                path([(xx + 0.07 * math.cos(math.tau * i / 16),
                       0.07 * math.sin(math.tau * i / 16)) for i in range(17)])
        elif part.kind == "Z":
            path([(-end, -0.3), (end, -0.3), (end, 0.3), (-end, 0.3), (-end, -0.3)])
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
            return (x, y + (0.68 if part.kind == "K" else 0.42)), "center"
        return (x - 0.61, y), "right"

    def potentiometer_symbol(self, part, a, b):
        x, y = a
        center = x + (b[0] - x) * 0.38
        offset = pin_offset(POT_PIN_OFFSET, self.pin_grid)
        body = [
            (center + (0 if i in (0, 9) else 0.2 if i % 2 else -0.2), y + 0.6 - 1.2 * i / 9)
            for i in range(10)
        ]
        self.lines([(x, y + offset), (center, y + offset), body[0]])
        self.lines(body)
        self.lines([body[-1], (center, y - offset), (x, y - offset)])
        # The tap moves along the resistive track; its external terminal stays fixed.
        at = (1 - part.position) * 9
        index = min(int(at), 8)
        fraction = at - index
        tip = tuple(start + (end - start) * fraction for start, end in zip(body[index], body[index + 1]))
        tail = center + 1.1, y
        self.lines([b, tail, tip])
        length = math.dist(tip, tail)
        dx, dy = ((end - start) / length for start, end in zip(tip, tail))
        self.lines([(tip[0] + .25 * dx + .12 * dy, tip[1] + .25 * dy - .12 * dx),
                    tip, (tip[0] + .25 * dx - .12 * dy, tip[1] + .25 * dy + .12 * dx)])
        return ((a[0] + b[0]) / 2, y + offset + 0.2), "center"

    def transistor_symbol(self, part, a, b):
        x, y = a

        def path(coords):
            return self.lines([(x + xx, y + yy) for xx, yy in coords])

        width = b[0] - x
        offset = pin_offset(BJT_PIN_OFFSET, self.pin_grid)
        base_x, base_offset = 1.7, 0.25
        elbow_x = base_x + 1
        path([(0, 0), (base_x, 0)])
        path([(base_x, 0.66), (base_x, -0.66)])
        path([(base_x, base_offset), (elbow_x, offset), (width, offset)])
        path([(base_x, -base_offset), (elbow_x, -offset), (width, -offset)])

        # The arrow lies on the emitter lead and points outwards for an NPN.
        direction = 1 if part.transistor_type == "npn" else -1
        slope = offset - base_offset
        tip = 0.5 + 0.28 * direction
        tail = tip - 0.34 * direction
        tail_x, tail_y = base_x + tail, -(base_offset + slope * tail)
        wing = 0.13
        tip_point = (base_x + tip, -(base_offset + slope * tip))
        arrow = path([
            tip_point,
            (tail_x - slope * wing, tail_y - wing),
            (tail_x + slope * wing, tail_y + wing),
            tip_point,
        ])
        arrow.setBrush(QBrush(INK))
        return (x + width / 2, y + offset + 0.15), "center"

    def target(self, pos, nets_only=False):
        for item in self.items(pos):
            kind, name = item.data(0), item.data(1)
            if kind in {"ground", "connector", "boundary"}:
                return "node", name
            if kind in {"wire", "node", "pin"} or (
                kind in {"component", "hood"} and not nets_only
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
                if item.data(0) in {"wire", "node", "pin", "ground", "connector", "boundary"}
            }
            if len(names) == 1:
                return "node", names.pop()
        return None

    def mousePressEvent(self, event):
        if isinstance(self.itemAt(event.position().toPoint()), BehaviorDiagram):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            pos = event.position().toPoint()
            control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if not self.place_pin:
                stop_at_terminal = False
                for item in self.items(pos):
                    key = item.data(2)
                    if item.data(4) is not None and not control:
                        self.clear_selection()
                        self.start_drag(item.data(0), item.data(1), tuple(key), pos, caption=item)
                        event.accept()
                        return
                    if item.data(0) in {"node", "pin"}:
                        stop_at_terminal = True
                        break
                    if key is not None:
                        kind, name, key = item.data(0), item.data(1), tuple(key)
                        selectable = kind in {"component", "hood", "ground", "connector", "boundary"}
                        identity = self.selection_id(kind, name, item.data(3))
                        if control and not selectable:
                            continue
                        if not (selectable and (control or (
                            key == self.selection_key and identity in self.selected_symbols
                        ))):
                            self.clear_selection()
                        self.start_drag(
                            kind, name, key, pos, terminal=item.data(3),
                            control=control and selectable,
                        )
                        event.accept()
                        return
                if control:
                    self.selection_start = pos
                    self.selection_box.setGeometry(QRect(pos, pos))
                    self.selection_box.show()
                    event.accept()
                    return
                if not stop_at_terminal and (wire := self.nearest_wire(pos)):
                    self.clear_selection()
                    key, net, edge = wire
                    self.start_drag("wire", net, key, pos, edge)
                    event.accept()
                    return
            self.clear_selection()
            target = self.target(pos, self.place_pin)
            if target:
                kind, name = target
                if kind == "hood":
                    self.toggle_package(name)
                    return
                if kind == "wire" and not self.place_pin:
                    self.probe_wire(name)
                    return
                if kind != "component":
                    self.highlight_net(name)
                if self.place_pin:
                    self.pin_requested.emit(name)
                else:
                    self.probed.emit(kind, name)
                return
        super().mousePressEvent(event)

    def start_drag(self, kind, name, key, pos, edge=None, *, terminal=None, control=False, caption=None):
        layout = self.manual_layouts.get(key, self.level_layouts[key])
        selection = self.selected_symbols if key == self.selection_key else set()
        identity = self.selection_id(kind, name, terminal) if caption is None else None
        members = selection | {identity} if control or identity in selection else ()
        if caption is not None:
            anchor = (0, 0)
        elif kind == "wire":
            anchor = edge[0]
        elif kind == "ground":
            anchor = terminal
        elif kind == "connector":
            anchor = layout.connectors[terminal][1]
        elif kind == "boundary":
            anchor = layout.boundary[name]
        else:
            anchor = layout.positions.get(name, (0, 0))
        origin = self.level_offsets.get(key, QPointF())
        anchor = anchor[0] + origin.x() / SCALE, anchor[1] - origin.y() / SCALE
        self.drag = SchematicDrag(
            kind, name, key, pos, layout, self.levels[key], edge=edge, terminal=terminal,
            control=control, selection=frozenset(members),
            caption=caption, caption_start=caption.pos() if caption is not None else None,
            grid_anchor=anchor, grid_step=grid_step(max(self.transform().m11() * SCALE, 0.01)),
        )

    def nearest_wire(self, pos, *, straight_only=True):
        scene_pos = self.mapToScene(pos)
        tolerance = 5 / max(self.transform().m11(), 0.01)
        best = tolerance
        match = None
        for key, offset in self.level_offsets.items():
            layout = self.manual_layouts.get(key, self.level_layouts[key])
            for net, edges in layout.wires.items():
                for a, b in edges:
                    diagonal = a[0] != b[0] and a[1] != b[1]
                    if a == b or (straight_only and diagonal):
                        continue
                    first, second = point(a) + offset, point(b) + offset
                    run = second - first
                    fraction = QPointF.dotProduct(scene_pos - first, run) / QPointF.dotProduct(run, run)
                    fraction = max(0, min(1, fraction))
                    closest = first + fraction * run
                    distance = math.hypot(closest.x() - scene_pos.x(), closest.y() - scene_pos.y())
                    if distance >= best:
                        continue
                    cuts = layout.crossings.get((net, a, b), ())
                    if diagonal:
                        gap = any(abs(fraction - cut) * math.dist(a, b) < 0.12 for cut in cuts)
                    else:
                        gap = a[0] == b[0] and any(
                            abs(-(closest.y() - offset.y()) / SCALE - cut) < 0.12 for cut in cuts
                        )
                    if gap:
                        continue
                    best = distance
                    match = key, net, (a, b)
        return match

    def preview_drag(self, pos):
        drag = self.drag
        if pos == drag.last_position:
            return
        drag.last_position = pos
        delta = self.mapToScene(pos) - self.mapToScene(drag.start)
        self.fitted = False
        if drag.caption is not None:
            drag.caption.setPos(drag.caption_start + delta)
            drag.preview = drag.original
            if not delta.isNull():
                offset = drag.caption.pos() - drag.caption.data(5)
                label_key = tuple(drag.caption.data(4))
                drag.preview = replace(drag.original, label_offsets={
                    **drag.original.label_offsets, label_key: (offset.x() / SCALE, -offset.y() / SCALE),
                })
            return
        raw = delta.x() / SCALE, -delta.y() / SCALE
        dx, dy = grid_delta(drag.grid_anchor, raw, drag.grid_step)
        pixels_per_unit = max(self.transform().m11() * SCALE, 0.01)
        acquire, release = snap_radius(pixels_per_unit, drag.grid_step)
        previous = drag.preview
        if drag.kind == "wire":
            a, b = drag.edge
            distance = dx if a[0] == b[0] else dy
            pointer = raw[0] if a[0] == b[0] else raw[1]
            if (
                drag.snapped_offset is not None
                and abs(pointer - drag.snapped_offset) <= release
            ):
                return
            drag.preview, drag.snapped_offset = move_wire(
                drag.circuit, drag.original, drag.name, drag.edge, distance,
                snap=acquire, pointer=pointer,
                accept_offset=lambda offset: on_grid(drag.grid_anchor[0 if a[0] == b[0] else 1] + offset, drag.grid_step),
            )
        elif len(drag.selection) > 1:
            drag.preview = move_symbols(
                drag.circuit, drag.original, drag.selection, (dx, dy), diagonal=drag.diagonal,
            )
        elif drag.kind == "connector":
            self.preview_connector_drag(drag, (dx, dy), raw, pixels_per_unit)
        elif drag.kind == "boundary":
            drag.preview = move_boundary(
                drag.circuit, drag.original, drag.name, (dx, dy), diagonal=drag.diagonal
            )
        else:
            self.preview_symbol_drag(drag, (dx, dy), raw, pixels_per_unit)
        drag.blocked = drag.preview is None
        if drag.blocked:
            drag.preview = previous or drag.original
        self._redraw(preview=drag, selected=drag.name if drag.kind == "wire" else self.highlight)
        self.viewport().setCursor(Qt.CursorShape.ForbiddenCursor if drag.blocked else Qt.CursorShape.ClosedHandCursor)

    def preview_connector_drag(self, drag, delta, raw, pixels_per_unit):
        index = drag.terminal
        net, start, name, *_ = drag.original.connectors[index]
        end = tuple(a + b for a, b in zip(start, delta))
        pointer = tuple(a + b for a, b in zip(start, raw))
        matching = sorted(
            (math.dist(pointer, pos), i, pos)
            for i, (other, pos, label, *_) in enumerate(drag.original.connectors)
            if i != index and (other, label) == (net, name)
        )
        acquire, _ = snap_radius(pixels_per_unit, drag.grid_step)
        for distance, i, pos in matching:
            if distance > acquire:
                break
            if not drag.accepts_offset(tuple(p - s for p, s in zip(pos, start))):
                continue
            if distance >= math.dist(start, pos):
                continue
            drawing = move_connector(
                drag.circuit, drag.original, index, pos, join=i, diagonal=drag.diagonal
            )
            if drawing is not None:
                drag.preview = drawing
                return
        # A split end may rejoin anywhere on its own net, including a branch
        # before the other label. The layout editor checks physical islands;
        # equal voltages or wires across a component are never enough to join.
        candidates = set()
        for a, b in drag.original.wires.get(net, ()):
            if a[0] == b[0]:
                pos = (a[0], min(max(end[1], min(a[1], b[1])), max(a[1], b[1])))
            elif a[1] == b[1]:
                pos = (min(max(end[0], min(a[0], b[0])), max(a[0], b[0])), a[1])
            else:
                dx, dy = b[0] - a[0], b[1] - a[1]
                t = min(1, max(0, ((pointer[0] - a[0]) * dx + (pointer[1] - a[1]) * dy) / (dx * dx + dy * dy)))
                pos = (round(a[0] + t * dx, 8), round(a[1] + t * dy, 8))
            candidates.add(pos)
        for pos in sorted(candidates, key=lambda p: math.dist(pointer, p)):
            distance = math.dist(pointer, pos)
            if distance > acquire:
                break
            if distance >= math.dist(start, pos) or not drag.accepts_offset(tuple(p - s for p, s in zip(pos, start))):
                continue
            drawing = move_connector(
                drag.circuit, drag.original, index, pos, join_wire=True, diagonal=drag.diagonal
            )
            if drawing is not None:
                drag.preview = drawing
                return
        drag.preview = move_connector(
            drag.circuit, drag.original, index, end, diagonal=drag.diagonal
        )

    def preview_symbol_drag(self, drag, delta, raw, pixels_per_unit):
        ground = tuple(drag.terminal) if drag.kind == "ground" else None
        move = move_ground if ground is not None else move_symbol
        target = ground if ground is not None else drag.name
        acquire, release = snap_radius(pixels_per_unit, drag.grid_step)
        snapping = not drag.diagonal
        if ground is None and snapping and self.snap_symbol_terminal(drag, raw, pixels_per_unit):
            return
        drag.snapped_terminal = None
        if drag.alignments is None and snapping:
            alignments = ground_wire_alignments if ground is not None else symbol_wire_alignments
            drag.align_axis, drag.alignments = alignments(drag.original, target)
        if ground is not None and snapping:
            attachment = ground_attachment(drag.original, ground)
            direct = tuple(end - start for start, end in zip(ground, attachment))
            if math.dist(raw, direct) <= acquire and drag.accepts_offset(direct):
                drawing = move(drag.circuit, drag.original, target, direct)
                if drawing is not None:
                    drag.preview = drawing
                    return
        axis = drag.align_axis
        if drag.alignments and snapping:
            distance = raw[axis]
            held = drag.snapped_offset
            offsets = sorted(
                (offset for offset in drag.alignments if abs(offset - distance) <= acquire),
                key=lambda offset: (
                    abs(offset - distance), -drag.alignments[offset], offset
                ),
            )
            if held is not None and abs(held - distance) <= release:
                offsets.insert(0, held)
            for offset in dict.fromkeys(offsets):
                snapped = list(delta)
                snapped[axis] = offset
                if not drag.accepts_offset(snapped):
                    continue
                drawing = move(
                    drag.circuit, drag.original, target, snapped, align_axis=axis
                )
                if drawing is not None:
                    drag.preview, drag.snapped_offset = drawing, offset
                    return
        drag.snapped_offset = None
        drag.preview = move(
            drag.circuit, drag.original, target, delta, diagonal=drag.diagonal
        )

    def snap_symbol_terminal(self, drag, delta, pixels_per_unit):
        acquire, release = snap_radius(pixels_per_unit, drag.grid_step)
        if drag.terminal_offsets is None:
            drag.terminal_offsets = symbol_terminal_offsets(drag.original, drag.name)
        if (
            drag.snapped_terminal is not None
            and math.dist(delta, drag.snapped_terminal) <= release
        ):
            return True
        candidates = sorted(
            (distance, offset) for offset in drag.terminal_offsets
            if (distance := math.dist(delta, offset)) <= acquire
            and drag.accepts_offset(offset)
        )
        for _, offset in candidates:
            drawing = move_symbol(drag.circuit, drag.original, drag.name, offset)
            if drawing is not None:
                drag.preview = drawing
                drag.snapped_terminal = offset
                drag.snapped_offset = None
                return True
        return False

    def paint_pending_drag(self):
        if self.drag is not None and self.drag.pending is not None:
            self.preview_drag(self.drag.pending)

    def mouseMoveEvent(self, event):
        if self.selection_start is not None:
            bounds = QRect(self.selection_start, event.position().toPoint()).normalized()
            self.selection_box.setGeometry(bounds)
            event.accept()
            return
        if self.drag is None:
            super().mouseMoveEvent(event)
            return
        end = event.position().toPoint()
        diagonal = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if diagonal != self.drag.diagonal:
            self.drag.diagonal = diagonal
            self.drag.last_position = None
        if (end - self.drag.start).manhattanLength() >= 4 or self.drag.last_position is not None:
            self.drag.pending = end
            if not self.drag_timer.isActive():
                self.drag_timer.start()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.selection_start is not None:
            bounds = QRect(self.selection_start, event.position().toPoint()).normalized()
            self.selection_start = None
            self.selection_box.hide()
            if bounds.width() >= 4 and bounds.height() >= 4:
                self.select_box(bounds)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton or self.drag is None:
            super().mouseReleaseEvent(event)
            return
        drag = self.drag
        self.drag_timer.stop()
        end = event.position().toPoint()
        diagonal = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if diagonal != drag.diagonal:
            drag.diagonal = diagonal
            drag.last_position = None
        if (end - drag.start).manhattanLength() < 4 and drag.pending is None:
            self.drag = None
            if drag.control:
                if self.selection_key != drag.key:
                    self.selected_symbols.clear()
                self.selection_key = drag.key
                self.selected_symbols.symmetric_difference_update({self.selection_id(drag.kind, drag.name, drag.terminal)})
                self.paint_selection()
            elif drag.kind == "hood":
                self.toggle_package(drag.name)
            elif drag.kind == "wire":
                self.probe_wire(drag.name)
            elif drag.kind in {"ground", "connector", "boundary", "node", "pin"}:
                self.highlight_net(drag.name)
                self.probed.emit("pin" if drag.kind == "pin" else "node", drag.name)
            else:
                self.probed.emit(drag.kind, drag.name)
                if self.source_editing and drag.kind == 'component':
                    part = next((part for part in self.circuit.components if part.name == drag.name), None)
                    if part and part.kind == 'P':
                        self.potentiometer_requested.emit(drag.name)
                    elif part and part.kind == 'K':
                        self.switch_requested.emit(drag.name)
        else:
            if drag.control:
                self.selection_key = drag.key
                self.selected_symbols = set(drag.selection)
            self.preview_drag(end)
            self.drag = None
            if drag.blocked:
                self.move_refused.emit()
            if drag.preview is not None and drag.preview is not drag.original:
                self.remember_placement(drag.key)
                self.rotation_bases.pop(drag.key, None)
                self.manual_layouts[drag.key] = drag.preview
                if drag.selection:
                    self.selection_key = drag.key
                    self.selected_symbols = {
                        ("ground", drag.preview.grounds[drag.original.grounds.index(member[1])])
                        if isinstance(member, tuple) and member[0] == "ground" else member
                        for member in drag.selection
                    }
                self.placement_changed.emit()
            focus = self.mapToScene(self.viewport().rect().center())
            self._redraw(focus, selected=self.highlight)
        self.viewport().unsetCursor()
        event.accept()

    def contextMenuEvent(self, event):
        wire = None
        if not self.place_pin:
            for item in self.items(event.pos()):
                kind = item.data(0)
                key = item.data(2)
                if kind == "ground":
                    ground, net = tuple(item.data(3)), item.data(1)
                    menu = QMenu(self)
                    menu.addAction("Rotate clockwise", lambda: self.rotate_ground(tuple(key), ground))
                    menu.addAction(
                        "Rotate counterclockwise", lambda: self.rotate_ground(tuple(key), ground, direction=-1)
                    )
                    menu.addAction("Add pin…", lambda: self.pin_requested.emit(net))
                    menu.exec(event.globalPos())
                    return
                if kind in {"node", "pin", "connector", "boundary"}:
                    break
                if key is not None and kind in {"component", "hood"}:
                    name = item.data(1)
                    menu = QMenu(self)
                    if self.source_editing and kind == 'component':
                        part = next((p for p in self.circuit.components if p.name == name), None)
                        if part and part.kind in {'R', 'C', 'L', 'Z', 'P'}:
                            menu.addAction('Edit value…', lambda: self.value_requested.emit(name))
                        if part and part.kind == 'P':
                            menu.addAction('Adjust position…', lambda: self.potentiometer_requested.emit(name))
                        if part and part.kind == 'K':
                            menu.addAction('Open switch' if part.closed else 'Close switch',
                                           lambda: self.switch_requested.emit(name))
                    if kind == "hood" and name.startswith(METHOD_PREFIX):
                        instance = self.circuit.instances[int(name.removeprefix(METHOD_PREFIX))]
                        if instance.builtin_gate:
                            menu.addAction(
                                "Edit shared gate layout…",
                                lambda: self.gate_layout_requested.emit(instance.name, len(instance.pins) - 3),
                            )
                        elif instance.name.casefold() in getattr(self.gate_layouts, "devices", {}):
                            menu.addAction(
                                "Edit shared chip / circuit layout…",
                                lambda: self.device_layout_requested.emit(instance.name.casefold()),
                            )
                    menu.addAction(
                        "Rotate clockwise", lambda: self.rotate_component(tuple(key), name)
                    )
                    menu.addAction(
                        "Rotate counterclockwise", lambda: self.rotate_component(tuple(key), name, direction=-1)
                    )
                    menu.addAction("Optimize wiring", self.optimize_wiring)
                    menu.exec(event.globalPos())
                    return
            wire = self.nearest_wire(event.pos(), straight_only=False)
        target = self.target(event.pos(), nets_only=True)
        if not target:
            menu = QMenu(self)
            menu.addAction("Optimize wiring", self.optimize_wiring)
            menu.exec(event.globalPos())
            return
        kind, net = target
        menu = QMenu(self)
        if kind == "pin":
            menu.addAction("Rename pin…", lambda: self.pin_renamed.emit(net))
            menu.addAction("Remove pin", lambda: self.pin_removed.emit(net))
        else:
            menu.addAction("Add pin…", lambda: self.pin_requested.emit(net))
            if wire and wire[1] == net:
                pos = self.mapToScene(event.pos()) - self.level_offsets[wire[0]]
                location = pos.x() / SCALE, -pos.y() / SCALE
                menu.addAction("Split", lambda: self.split_connection(*wire, location))
        menu.addAction("Optimize wiring", self.optimize_wiring)
        menu.exec(event.globalPos())

    def split_connection(self, key, net, edge, location):
        if not self.isEnabled():
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        offset = self.level_offsets.get(key, QPointF())
        drawing = split_wire(self.levels[key], original, net, edge, location,
                             grid=grid_step(max(self.transform().m11() * SCALE, 0.01)),
                             origin=(offset.x() / SCALE, -offset.y() / SCALE))
        if drawing is None:
            self.move_refused.emit()
            return
        self.remember_placement(key)
        self.rotation_bases.pop(key, None)
        self.manual_layouts[key] = drawing
        self.placement_changed.emit()
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=net)

    def rotate_ground(self, key, point, *, direction=1):
        if not self.isEnabled():
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        rotations = dict(original.ground_rotations)
        turns = (rotations.pop(point, 0) + direction) % 4
        if turns:
            rotations[point] = turns
        self.remember_placement(key)
        self.rotation_bases.pop(key, None)
        self.manual_layouts[key] = replace(original, ground_rotations=rotations)
        self.placement_changed.emit()
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=self.highlight)

    def rotate_component(self, key, name, *, direction=1):
        if not self.isEnabled():
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        part, base, turns = self.rotation_bases.get(key, (name, original, 0))
        if part != name:
            base, turns = original, 0
        turns = (turns + direction) % 4
        drawing = (
            move_symbol(self.levels[key], base, name, (0, 0), rotate=turns)
            if turns else base
        )
        if drawing is None:
            # A pin may land on a different net at the exact pivot, as
            # when flipping a part in an end-to-end chain. Try a small nudge.
            for offset in ((0, 1.5), (0, -1.5), (1.5, 0), (-1.5, 0),
                           (0, 2.5), (0, -2.5), (2.5, 0), (-2.5, 0)):
                drawing = move_symbol(self.levels[key], base, name, offset, rotate=turns)
                if drawing is not None:
                    break
        if drawing is None:
            self.move_refused.emit()
            return
        self.remember_placement(key)
        self.manual_layouts[key] = drawing
        self.rotation_bases[key] = name, base, turns
        self.placement_changed.emit()
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=self.highlight)
