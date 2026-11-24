"""PySide6 schematic scene for the shared layout model."""
# The electrical graph is authoritative; scene items are a view of separate layout geometry.
# Drag previews propose checked edits, source-backed value gestures use explicit signals,
# and repainting live values must not rebuild the circuit or erase placement/undo history.

import math
import re
from hashlib import sha256
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from functools import partial

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QKeySequence, QPainter, QPainterPath, QPainterPathStroker,
    QPen, QPixmap, QShortcut, QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QRubberBand,
    QSlider,
    QToolButton,
    QVBoxLayout,
)

from circuitlab.layout.assignment import assign_interchangeable_parts
from circuitlab.layout.board_layers import layer_wires, move_board_wire, remap_wire_layers, set_wire_layer, wire_subset
from circuitlab.layout.board_routing import optimize_board_wiring
from circuitlab.layout.checks import clear_package_wires, preserves_nets, segments_intersect
from circuitlab.layout.editing import (
    GroundIsland, attached_ground_islands, flip_opamp_inputs, ground_attachment, ground_wire_alignments,
    move_boundary, move_connector, move_ground, move_symbol, move_symbols, move_wire, rotate_symbols, selection_ends,
    selection_geometry, split_wire, symbol_terminal_offsets, symbol_wire_alignments,
)
from circuitlab.layout.geometry import (
    BJT_PIN_OFFSET, POT_PIN_OFFSET, ChipPackage, Layout, along, opamp_body, package_body_corners,
    package_contacts, pin_offset, quad_package, rotate_point, routed_pins, symbol_bounds, symbol_pins,
)
from circuitlab.layout.grid import align_layout
from circuitlab.layout.ground_clearance import clear_ground_marks, ground_strokes
from circuitlab.layout.hierarchy import METHOD_PREFIX, hood_levels
from circuitlab.layout.lead_spacing import shorten_symbol_leads
from circuitlab.layout.persistence import pack_layout, restore_layout
from circuitlab.layout.perfboard import (
    TWO_TERMINAL, move_perfboard_parts, perfboard_issues, perfboard_layout, reroute_perfboard,
    resize_perfboard_parts, rotate_perfboard_parts, valid_perfboard_layout,
)
from circuitlab.layout.route_quality import crossing_count, wire_length
from circuitlab.layout.routing import fast_layout, schematic_layout
from circuitlab.layout.shorten import optimize_layout, refine_automatic_layout
from circuitlab.layout.stage_selection import local_stage_selection
from circuitlab.layout.wire_edit import merge_strokes
from circuitlab.layout.wiring import labeled
from circuitlab.math.report import component_labels
from circuitlab.model.graph import Circuit, CircuitError
from circuitlab.viewer.behavior_diagram import BehaviorDiagram
from circuitlab.viewer.behavior_scope import chip_diagram_model
from circuitlab.viewer.snapping import GRID_STEP, grid_delta, grid_step, on_grid, snap_radius
from circuitlab.viewer.perfboard import board_point, inverse_board_point, paint_perfboard, underside_offset

INK = QColor("#192633")
NET = QColor("#006565")
ACCENT = QColor("#006dab")
PROBE = QColor("#974616")
LOGIC_HIGH = QColor("#159b71")
LOGIC_LOW = QColor("#718096")
SCALE = 38
SWITCH_CONTACT_OFFSET = 0.65
SWITCH_CONTACT_RADIUS = 0.07
SWITCH_HIT_MARGIN = 8
COMPACT_LABEL_SIZE = 9
COPPER = QColor("#96571f")

# Grid appearance. Movement spacing and magnet strength are in snapping.py.
GRID_MAJOR_STEPS = 2      # Number of snap intervals per darker square.
GRID_MAJOR_COLOR = "#c4d0da"
GRID_MINOR_COLOR = "#edf1f4"
GRID_LINE_WIDTH = 1.0


def build_schematic_layout(circuit, mode, boundary=(), *, nested=False):
    """Generate and refine geometry before creating any Qt scene items."""
    labels = component_labels(circuit)
    if circuit.perfboard is not None and not nested and not boundary:
        return perfboard_layout(circuit, labels, editable=True)
    if mode == "labeled":
        layout = labeled(circuit, labels)
    elif nested or boundary:
        layout = fast_layout(circuit, labels, boundary=boundary)
    else:
        layout = schematic_layout(circuit, labels, mode)
    layout = align_layout(
        circuit, layout, GRID_STEP, prune_cycles=True, keep_package_clearance=True,
    ) or layout
    if mode == "auto":
        layout = refine_automatic_layout(circuit, layout)
    elif layout.pin_grid:
        layout = shorten_symbol_leads(circuit, layout)
    return clear_ground_marks(circuit, layout, grid=GRID_STEP, face_leads=mode == "auto")


def point(p):
    return QPointF(p[0] * SCALE, -p[1] * SCALE)


def opamp_triangle(a, b):
    base, tip = opamp_body(a, b)
    y = a[1]
    return (base, y + .72), (base, y - .72), (tip, y)


def switch_bounds(a, b, turns):
    """Bound the contact area without including the wire leads."""
    span = (SWITCH_CONTACT_OFFSET + SWITCH_CONTACT_RADIUS) / math.dist(a, b)
    first, last = (point(rotate_point(along(a, b, fraction), a, b, turns))
                   for fraction in (0.5 - span, 0.5 + span))
    dx, dy = last.x() - first.x(), last.y() - first.y()
    margin = SWITCH_HIT_MARGIN / math.hypot(dx, dy)
    pad_x, pad_y = abs(dy) * margin, abs(dx) * margin
    return QRectF(first, last).normalized().adjusted(-pad_x, -pad_y, pad_x, pad_y)


@dataclass
class SchematicDrag:
    kind: str
    name: str
    key: tuple
    start: QPoint
    original: Layout
    circuit: Circuit
    edge: tuple | None = None
    terminal: tuple | int | None = None
    preview: Layout | None = None
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
    stage_group: frozenset[str | tuple] = frozenset()
    attached_grounds: tuple[GroundIsland, ...] = ()
    caption: QGraphicsSimpleTextItem | None = None
    caption_start: QPointF | None = None
    grid_anchor: tuple = (0, 0)
    grid_step: float = 0.25
    keep_package_clearance: bool = False
    blocked: bool = False
    underside: bool = False
    mirrored: bool = False

    @property
    def moving_selection(self):
        """Include the attached stage during a drag and keep explicit selection separate."""
        return self.stage_group or self.selection

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
    layout_issues_changed = Signal(object)
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
        self.caption_bodies = {}
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
        self.layout_issues = ()
        self.layout_mode = "auto"
        self.fitted = True
        self.fit_scale = 1.0
        self.content_bounds = QRectF()
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
        self.underside_visible = False

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
        self._set_layout_issues(())
        self.content_bounds = QRectF()
        self.setSceneRect(QRectF(0, 0, 1, 1))
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
        self.underside_visible = False

    def _clear_scene(self):
        if self.drag is not None and self.drag.caption is not None:
            self.drag_timer.stop()
            self.drag = None
        self.scene().clear()
        for items in (self.selection_items, self.paths, self.net_items, self.anchors,
                      self.pin_items, self.captions, self.caption_bodies, self.packages, self.levels, self.level_offsets,
                      self.behavior_diagrams):
            items.clear()
        self.highlight = None

    # Choose visible hierarchy levels and restore compatible placement records before painting.
    # The underlying compiled component/net identities remain unchanged by opening a package.
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
        if circuit is None or (not circuit.components and not circuit.instances and circuit.perfboard is None):
            self.layout = None
            self.content_bounds = QRectF()
            self.setSceneRect(QRectF(0, 0, 1, 1))
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
            key = name, self.layout_mode
            if key in self.placement_records:
                saved = restore_layout(self.placement_records.pop(key), view, boundary,
                                       editable_perfboard=view.perfboard is not None)
                if saved is not None and view.perfboard is not None and saved.pin_grid != 1:
                    saved = None
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
                    # Only newly generated layouts change automatically. Restored
                    # placements and shared gate designs retain their geometry.
                    layout = build_schematic_layout(
                        view, self.layout_mode, boundary, nested=name is not None,
                    )
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
        if preview is not None:
            if selected:
                self.highlight_net(selected)
            return
        self.content_bounds = self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if focus is not None and not self.fitted:
            self._update_pan_bounds(focus)
            self.centerOn(focus)
        else:
            self.fit()
        if selected:
            self.highlight_net(selected)
        for net in self.trace_nets:
            self.paint_highlight(net, True)
        self.anchor_position_control()
        board = self.levels.get((None, self.layout_mode))
        self._set_layout_issues(perfboard_issues(board, self.layout) if board and board.perfboard else ())

    def _set_layout_issues(self, issues):
        # Placement warnings belong to current geometry, not the compiled circuit.
        issues = tuple(issues)
        if [(issue.code, issue.message, issue.line, issue.path) for issue in issues] != [
                (issue.code, issue.message, issue.line, issue.path) for issue in self.layout_issues]:
            self.layout_issues = issues
            self.layout_issues_changed.emit(issues)

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

    # Refresh the glyph/value presentation using existing geometry, including opened packages;
    # a live value preview must not restart placement or replace its undo history.
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

    # Attach scene items to their model identities so probing and gestures can resolve a
    # painted object back to the correct component or canonical electrical net.
    def _paint_level(self, circuit, layout):
        scene = self.scene()
        self.label_offsets = layout.label_offsets
        self.pin_grid = layout.pin_grid
        wires_visible = layout.wires_visible and (self.layout is None or self.layout.wires_visible)
        if self.level is None and self.detail_root is None and circuit.perfboard is not None:
            paint_perfboard(scene, circuit.perfboard, scale=SCALE)
        self.level_offsets[self.level, self.layout_mode] = QPointF(self.offset)
        labels = component_labels(circuit)
        for symbol in layout.symbols:
            a, b = symbol_bounds(symbol, layout.rotations.get(symbol[0].name, 0), body_only=True)
            self.caption_bodies[self.level, symbol[0].name] = QRectF(point(a), point(b)).normalized().translated(self.offset)
        top_wires = layer_wires(layout) if circuit.perfboard is not None else layout.wires
        top_drawing = replace(layout, wires=top_wires)
        for name in (top_wires if wires_visible else ()):
            path = self.paths[name].path() if name in self.paths else QPainterPath()
            for a, b in merge_strokes(top_drawing.visible_segments(name)):
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
        if circuit.perfboard is not None and wires_visible:
            for net, edges in layer_wires(layout, underside=True).items():
                path = QPainterPath()
                for a, b in merge_strokes(edges):
                    path.moveTo(self.mapped(a))
                    path.lineTo(self.mapped(b))
                pen = self.pen(COPPER, 1.15)
                pen.setStyle(Qt.PenStyle.DashLine)
                item = scene.addPath(path, pen)
                item.setData(0, "under-wire")
                item.setData(1, net)
                item.setToolTip(f"{net} · underside wire")
                self.net_items[net].append(item)
        for net, dots in (layout.junctions.items() if wires_visible else ()):
            for dot in dots:
                if circuit.perfboard is not None and not any(
                        segments_intersect((dot, dot), edge) for edge in top_wires.get(net, ())):
                    continue
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
                self.package(part, a, b, transistor_open=bool(part.base and part.name in self.expanded))
                self.symbol_turn = None
                continue
            counts[part.kind] += 1
            name = labels[part.name]
            compact = part.name in layout.compact_labels and len(name) > 1
            if compact:
                name = name[1:]
            elif len(name[0]) > 24:
                prefix = "U" if part.kind in {"O", "A"} else part.kind
                name = [f"{prefix}#{counts[part.kind]}", *name[1:]]
            pos, alignment = self.symbol(part, a, b, top)
            powered_amp = part.kind in {"O", "A"} and bool(part.vplus and part.vminus)
            transparent = QBrush(QColor(0, 0, 0, 0))
            no_pen = QPen(Qt.PenStyle.NoPen)
            if part.kind == "O":
                first, second, third = map(self.mapped, opamp_triangle(a, b))
                body = QPainterPath(first)
                body.lineTo(second)
                body.lineTo(third)
                body.closeSubpath()
                edge = QPainterPathStroker()
                edge.setWidth(8)
                handle = scene.addPath(body.united(edge.createStroke(body)), no_pen, transparent)
            elif part.kind == "K":
                bounds = switch_bounds(a, b, turns).translated(self.offset)
                handle = scene.addRect(bounds, no_pen, transparent)
            else:
                if part.kind == "P":
                    height = pin_offset(POT_PIN_OFFSET, self.pin_grid)
                    first = self.mapped((a[0], a[1] - height))
                    second = self.mapped((b[0], b[1] + height))
                elif powered_amp:
                    pins = [self.mapped(pin) for _, pin in symbol_pins(part, a, b, top, grid=self.pin_grid)]
                    first = QPointF(min(pin.x() for pin in pins), min(pin.y() for pin in pins))
                    second = QPointF(max(pin.x() for pin in pins), max(pin.y() for pin in pins))
                else:
                    first, second = self.mapped(a), self.mapped(b)
                bounds = QRectF(first, second).normalized().adjusted(-8, -14, 8, 14)
                handle = scene.addRect(bounds, no_pen, transparent)
            handle.setZValue(0.5)
            self.drag_handle(handle, "component", part.name)
            handle.setToolTip("\n".join(labels[part.name]))
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
                if part.kind == "A":
                    pos, alignment = (x, y + .1), "center"
            self.symbol_turn = None
            component_captions.append((name, pos, ("component", part.name), alignment, compact))
        for pos in (layout.grounds if wires_visible else ()):
            net = layout.ground_names.get(pos, "0")
            self.ground(pos, net, layout.ground_rotations.get(pos, 0))
            self.anchors.setdefault(net, self.scene_position(pos))
        compact_font = QFont(self.label_font)
        compact_font.setPixelSize(COMPACT_LABEL_SIZE)
        for name, pos, probe, alignment, compact in component_captions:
            caption = self.place_caption(name, pos, INK, probe, alignment,
                                         move_key=("component", probe[1]),
                                         font=compact_font if compact else None)
            caption.setToolTip("\n".join(labels[probe[1]]))
        for name, pos in (layout.labels.items() if wires_visible else ()):
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
        for index, (net, pos, name, left, below) in enumerate(layout.connectors if wires_visible else ()):
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
            item.setToolTip(f"Drag node {name}")
            caption.setToolTip(f"Drag label {name}")
            self.anchors.setdefault(net, self.scene_position(pos))
        symbols = {
            part.name: tuple(rotate_point(p, a, b, layout.rotations.get(part.name, 0))
                             for p in (a, b))
            for part, a, b, _ in layout.symbols
        }
        connector_names = {(net, name) for net, _, name, _, _ in layout.connectors}
        tap_points = defaultdict(list)
        for tap in (circuit.wire_taps if wires_visible else ()):
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
            terminal.setData(2, (self.level, self.layout_mode))
            terminal.setData(3, pos)
            terminal.setZValue(1)
            terminal.setToolTip(net)
        if self.level is None and self.detail_root is None and circuit.perfboard and self.underside_visible:
            self._paint_underside(circuit, layout)

    def _paint_underside(self, circuit, layout):
        """Show solder pads and back wiring in a mirrored view of the same holes."""
        board = circuit.perfboard
        offset = underside_offset(board)
        paint_perfboard(self.scene(), board, scale=SCALE, mirror=True, offset=offset)

        def mapped(at):
            return point(board_point(board, at, underside=True, offset=offset))

        if layout.wires_visible:
            back = replace(layout, wires=layer_wires(layout, underside=True))
            for net in back.wires:
                path = QPainterPath()
                for a, b in merge_strokes(back.visible_segments(net)):
                    path.moveTo(mapped(a))
                    path.lineTo(mapped(b))
                item = self.scene().addPath(path, self.pen(COPPER, 1.8))
                item.setData(0, "back-wire")
                item.setData(1, net)
                item.setToolTip(f"{net} · drag underside wire")
                item.setCursor(Qt.CursorShape.OpenHandCursor)
                self.net_items[net].append(item)
            for net, dots in layout.junctions.items():
                for dot in dots:
                    if any(segments_intersect((dot, dot), edge) for edge in back.wires.get(net, ())):
                        center = mapped(dot)
                        item = self.scene().addEllipse(center.x() - 2.4, center.y() - 2.4, 4.8, 4.8,
                                                       QPen(Qt.PenStyle.NoPen), QBrush(COPPER))
                        item.setData(0, "back-pin")
                        item.setData(1, net)
                        item.setToolTip(f"{net} · underside junction")
        key = None, self.layout_mode
        for symbol in layout.symbols:
            part = symbol[0]
            for index, (net, at) in enumerate(routed_pins(symbol, layout.rotations.get(part.name, 0), layout.pin_grid)):
                center = mapped(at)
                radius = .16 * SCALE
                pad = self.scene().addEllipse(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius,
                                             self.pen(COPPER, 1.4), QBrush(QColor("#f7e1a9")))
                pad.setZValue(2)
                pad.setData(0, "back-pin")
                pad.setData(1, net)
                pad.setData(2, key)
                pad.setData(3, at)
                pad.setToolTip(f"{part.label or part.name} · pin {index + 1} · {net}\nUnderside")
                self.net_items[net].append(pad)

    def toggle_underside(self):
        circuit = self.levels.get((None, self.layout_mode))
        if circuit is None or circuit.perfboard is None or self.drag is not None:
            return
        self.underside_visible = not self.underside_visible
        self.fitted = True
        self._redraw(selected=self.highlight)

    def _underside_at(self, pos):
        if not self.underside_visible:
            return False
        if any(item.data(2) == "underside" for item in self.items(pos)):
            return True
        # Back-side leads can extend beyond the board. Keep the same screen
        # tolerance there as thin front-side wires have when picking them.
        nearby = self.items(pos.x() - 5, pos.y() - 5, 10, 10, Qt.ItemSelectionMode.IntersectsItemShape)
        return any(item.data(0) in {"back-wire", "back-pin"} for item in nearby)

    def _board_location(self, pos, key):
        at = self.mapToScene(pos) - self.level_offsets.get(key, QPointF())
        at = at.x() / SCALE, -at.y() / SCALE
        board = self.levels[key].perfboard
        if board is not None and self._underside_at(pos):
            at = inverse_board_point(board, at, underside=True, offset=underside_offset(board))
        return at

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
        caption.setToolTip(f"Drag label {name}")
        self.anchors.setdefault(net, self.scene_position(pos))

    def paint_selection(self, preview=None):
        for item in self.selection_items:
            self.scene().removeItem(item)
        self.selection_items.clear()
        moving = preview.moving_selection if preview is not None else ()
        key = preview.key if moving else self.selection_key
        names = moving or self.selected_symbols
        if key not in self.level_offsets or not names:
            return
        layout = self.manual_layouts.get(key, self.level_layouts[key])
        if preview is not None and preview.key == key:
            layout = preview.preview or layout
            if moving:
                names = self.drag_selection(preview, layout, include_stage=True)
        offset = self.level_offsets[key]
        ends, wires = selection_geometry(layout, names) if layout.wires_visible else ((), ())
        corners = [point(pos) for edge in wires for pos in edge]
        for (kind, _), pos in ends:
            corners.append(point(pos))
            if kind == "ground":
                turns = layout.ground_rotations.get(pos, 0)
                corners.extend(point(end) for edge in ground_strokes(pos, turns) for end in edge)
        for symbol in layout.symbols:
            part, a, b, _ = symbol
            if part.name not in names:
                continue
            turns = layout.rotations.get(part.name, 0)
            if part.kind == "K":
                bounds = switch_bounds(a, b, turns)
                corners.extend((bounds.topLeft(), bounds.bottomRight()))
            else:
                corners.extend(map(point, symbol_bounds(symbol, turns, body_only=part.kind == "O")))
        if corners:
            xs, ys = [p.x() for p in corners], [p.y() for p in corners]
            rect = QRectF(QPointF(min(xs), min(ys)), QPointF(max(xs), max(ys)))
            pen = self.pen(ACCENT, 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            outline = self.scene().addRect(rect.translated(offset).adjusted(-6, -6, 6, 6), pen)
            outline.setZValue(4)
            outline.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.selection_items.append(outline)

    @staticmethod
    def drag_selection(drag, layout, *, include_stage=False):
        """Keep selections attached to moved markers and drop joined connectors."""
        names = set()
        for member in (drag.moving_selection if include_stage else drag.selection):
            if isinstance(member, tuple):
                kind, identity = member
                if kind == "ground":
                    member = kind, layout.grounds[drag.original.grounds.index(identity)]
                elif kind == "connector" and len(layout.connectors) != len(drag.original.connectors):
                    continue
            names.add(member)
        return names

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

    def package(self, part, a, b, *, transistor_open=False):
        original_font = self.label_font
        if isinstance(part, ChipPackage) and part.pin_pitch:
            self.label_font = QFont(original_font)
            self.label_font.setPixelSize(9)
        try:
            self._package(part, a, b, transistor_open=transistor_open)
        finally:
            self.label_font = original_font

    def _package(self, part, a, b, *, transistor_open=False):
        (left, lower), (right, upper) = package_body_corners(part, a, b, self.pin_grid)
        center = (left + right) / 2
        top = self.mapped((left, upper))
        bottom = self.mapped((right, lower))
        bounds = QRectF(top, bottom).normalized()
        fill = QBrush(QColor("#eaf1f5"))
        is_chip = isinstance(part, ChipPackage)
        quad = quad_package(part, a, b)
        if is_chip and not quad:
            # The notch marks the pin-1 end and rotates with the physical pins.
            outline = QPainterPath(top)
            outline.lineTo(self.mapped((center - 0.33, upper)))
            outline.cubicTo(self.mapped((center - 0.33, upper - 0.37)),
                            self.mapped((center + 0.33, upper - 0.37)),
                            self.mapped((center + 0.33, upper)))
            for p in ((right, upper), (right, lower), (left, lower)):
                outline.lineTo(self.mapped(p))
            outline.closeSubpath()
            self.scene().addPath(outline, self.pen(INK), fill)
            rect = self.scene().addRect(bounds, QPen(Qt.PenStyle.NoPen), QBrush(QColor(0, 0, 0, 0)))
        else:
            rect = self.scene().addRect(bounds, self.pen(INK), fill)
            if quad:
                # A corner dot marks pin 1 and leaves the top edge free for pins.
                index_mark = self.mapped((left + .29, upper - .29))
                radius = .09 * SCALE
                self.scene().addEllipse(index_mark.x() - radius, index_mark.y() - radius,
                                        2 * radius, 2 * radius, QPen(Qt.PenStyle.NoPen), QBrush(INK))
        self.drag_handle(rect, "hood", part.name)
        tooltip = f"{'Close' if part.name in self.expanded else 'Open'} {part.label}"
        rect.setToolTip(tooltip)
        self.packages[part.name] = (part, rect.rect())
        contacts = tuple(package_contacts(part, a, b, self.pin_grid))
        title_clearance = 0.40 if is_chip else 0.12
        metrics = QFontMetricsF(self.label_font)
        if quad:
            title_clearance += max((metrics.horizontalAdvance(pin) / SCALE
                                    for pin, _, side, _, _ in contacts if side == "top"), default=0)
        half_line = metrics.height() / (2 * SCALE)
        title = self.label(
            part.label, (center, upper - title_clearance - half_line), INK, ("hood", part.name), "middle",
            move_key=("component", part.name),
        )
        title.setToolTip(tooltip)
        self.drag_handle(title, "hood", part.name)
        if transistor_open:
            self.transistor_symbol(part, a, b)
            return
        turns = self.symbol_turn[2] if self.symbol_turn else 0
        for index, contact in enumerate(contacts, 1):
            self.package_pin(part, index, contact, metrics, turns, quad=quad)
        if self.symbol_turn is not None:
            self.rotate_caption(title, turns)

    def package_pin(self, part, index, pin_geometry, metrics, turns, *, quad):
        pin, net, side, contact, edge = pin_geometry
        is_chip = isinstance(part, ChipPackage)
        implicit = is_chip and index > part.physical_pin_count
        vertical = side in {"top", "bottom"}
        if is_chip and not implicit:
            half_tab = .26 if quad else .21
            cx, cy = contact
            ex, ey = edge
            first, last = (((cx - half_tab, cy), (ex + half_tab, ey)) if vertical
                           else ((cx, cy - half_tab), (ex, ey + half_tab)))
            tab = QRectF(self.mapped(first), self.mapped(last)).normalized()
            self.scene().addRect(tab, self.pen(INK), QBrush(QColor("#f8fafc")))
            number = self.label(str(index), ((cx + ex) / 2, (cy + ey) / 2), INK, alignment="middle")
            number.setToolTip(f"{pin}: {net}")
            if part.pin_pitch:
                # Physical DIP widths leave room for pin numbers; the full
                # terminal name remains available on hover.
                return
            if turns:
                self.rotate_caption(number, turns)
        else:
            lead = self.lines((contact, edge))
            if implicit:
                pen = lead.pen()
                pen.setStyle(Qt.PenStyle.DashLine)
                lead.setPen(pen)

        text = f"{pin} (implicit)" if implicit else pin
        x, y = edge
        if vertical:
            inward = -1 if side == "top" else 1
            clearance = .2 + metrics.horizontalAdvance(text) / (2 * SCALE)
            label = self.label(text, (x, y + inward * clearance), NET, ("node", net), "middle")
            self.rotate_caption(label, turns - inward)
        else:
            on_left = side == "left"
            label = self.label(text, (x + (.2 if on_left else -.2), y),
                               NET, ("node", net), "left" if on_left else "right")
            if on_left:
                label.moveBy(0, label.boundingRect().height() / 2)
            if turns:
                self.rotate_caption(label, turns)
        self.movable_caption(label, ("terminal", part.name, index))

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
                self.content_bounds = self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
                self._update_pan_bounds(self.mapToScene(self.viewport().rect().center()))
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
                self.behavior_models[name] = chip_diagram_model(self.circuit.instances, index, self.logic_simulation)
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

    # Validate all displayed levels before accepting the bulk operation, retaining one
    # coherent undo record rather than a partially aligned hierarchy.
    def align_to_grid(self):
        """Align all displayed levels atomically and retain a single undo step."""
        if not self.isEnabled() or self.drag is not None or not self.levels:
            return True
        changed = {}
        for key, circuit in self.levels.items():
            current = self.manual_layouts.get(key, self.level_layouts[key])
            if circuit.perfboard is not None:
                if not valid_perfboard_layout(circuit, current):
                    return False
                continue
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

    # Optimization is explicit and undoable; keep all displayed levels electrically valid
    # before committing the group of geometry changes.
    def optimize_wiring(self):
        """Shorten displayed wiring explicitly, with one undo step for all levels."""
        if not self.isEnabled() or self.drag is not None or not self.levels:
            return
        if self.layout is not None and not self.layout.wires_visible:
            self.optimization_finished.emit("Arrange the parts, then choose Add wires to route their connections.")
            return
        changed = {}
        before_length = after_length = 0
        before_crossings = after_crossings = 0
        reassigned = 0
        self.viewport().setCursor(Qt.CursorShape.WaitCursor)
        try:
            for key, circuit in self.levels.items():
                current = self.manual_layouts.get(key, self.level_layouts[key])
                if circuit.perfboard is not None:
                    assigned = assign_interchangeable_parts(circuit, current)
                    optimized = optimize_board_wiring(circuit, assigned, reference=current)
                    # Keep a working drawing if a proposed identity assignment
                    # leaves no safe route at the fixed physical positions.
                    if (optimized is assigned and assigned is not current
                            and not preserves_nets(circuit, optimized) and preserves_nets(circuit, current)):
                        optimized = optimize_board_wiring(circuit, current)
                    reassigned += sum(before != after for before, after in zip(current.symbols, optimized.symbols))
                else:
                    optimized = optimize_layout(circuit, current, move_parts=True, grid=GRID_STEP)
                    optimized = clear_ground_marks(circuit, optimized, grid=GRID_STEP, opamp_sideways=True)
                before_length += wire_length(current)
                after_length += wire_length(optimized)
                before_crossings += crossing_count(current)
                after_crossings += crossing_count(optimized)
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
        if any(layout.underside_wires for layout in changed.values()) and not self.underside_visible:
            self.underside_visible = True
            self.fitted = True
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=self.highlight)
        self.placement_changed.emit()
        improvements = []
        if reassigned:
            improvements.append(f'{reassigned} matching parts reassigned')
        if before_length > after_length + 1e-8:
            reduction = 100 * (before_length - after_length) / before_length
            improvements.append(f'{reduction:.1f}% shorter')
        removed = before_crossings - after_crossings
        if removed > 0:
            improvements.append('1 fewer crossing' if removed == 1 else f'{removed} fewer crossings')
        if any(layout.underside_wires for layout in changed.values()):
            improvements.append('uses underside routing')
        detail = f' ({", ".join(improvements)})' if improvements else ''
        self.optimization_finished.emit(f'Wiring simplified{detail}. Ctrl+Z restores the previous layout.')

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
            self.content_bounds = self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
            if fit and self.fitted:
                self.fit()
            else:
                self._update_pan_bounds(self.mapToScene(self.viewport().rect().center()))

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
            if item.data(0) in {"back-wire", "under-wire", "back-pin"}:
                pen = self.pen(ACCENT if selected else level_color or COPPER, 2.6 if selected else 1.5)
                if item.data(0) == "under-wire":
                    pen.setStyle(Qt.PenStyle.DashLine)
                item.setPen(pen)
            elif item.data(0) == "ground_stroke":
                item.setPen(self.pen(color, 2.6 if selected else 1.55))
            elif isinstance(item, QGraphicsSimpleTextItem):
                item.setBrush(label_color)
            else:
                item.setPen(self.pen(label_color, 1.8 if selected else 1.2))

    # Update colors from the active simulator without rerouting or modifying source text.
    def set_logic_values(self, values):
        """Overlay simulated 0/1 levels without changing layout or source."""
        previous = self.logic_values
        self.logic_values = {
            name: value for name, value in values.items()
            if (name in self.paths or name in self.net_items) and value in (0, 1)
        }
        for net in previous.keys() | self.logic_values.keys():
            if previous.get(net) != self.logic_values.get(net):
                self.paint_highlight(net, net == self.highlight or net in self.trace_nets)

    def probe_wire(self, net):
        self.highlight_net(None if net == self.highlight else net)
        self.probed.emit("node", net)

    def _update_pan_bounds(self, focus=None, *, factor=1.0):
        # Leave half a viewport around every edge so any part of the drawing can be centered.
        scale = self.transform().m11() * factor
        half_width = (self.viewport().width() / 2 + 2) / scale
        half_height = (self.viewport().height() / 2 + 2) / scale
        bounds = self.content_bounds.adjusted(-half_width, -half_height, half_width, half_height)
        if focus is not None:
            # Preserve the visible area when the contents shrink or move during an edit.
            # Zooming out at the pointer can move the center by the change in viewport radius.
            if factor < 1:
                half_width += half_width * (1 - factor)
                half_height += half_height * (1 - factor)
            visible = QRectF(focus.x() - half_width, focus.y() - half_height,
                             2 * half_width, 2 * half_height)
            bounds = bounds.united(visible)
        self.setSceneRect(bounds)

    def fit(self):
        if self.layout is not None:
            self.setSceneRect(self.content_bounds)
            self.fitInView(self.content_bounds, Qt.AspectRatioMode.KeepAspectRatio)
            self._update_pan_bounds()
            self.centerOn(self.content_bounds.center())
            self.fit_scale = self.transform().m11()
            self.fitted = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitted:
            self.fit()
        elif self.layout is not None:
            self._update_pan_bounds(self.mapToScene(self.viewport().rect().center()))

    def zoom(self, factor):
        scale = self.transform().m11() * factor
        if self.layout is not None and self.fit_scale / 4 < scale < max(
            80, self.fit_scale * 4
        ):
            self.fitted = False
            if factor < 1:
                # Allow the larger visible area before Qt anchors the zoom at the pointer.
                self._update_pan_bounds(self.mapToScene(self.viewport().rect().center()), factor=factor)
            self.scale(factor, factor)
            self._update_pan_bounds(self.mapToScene(self.viewport().rect().center()))
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

    def label(self, text, p, color, probe=None, alignment="center", *, move_key=None, font=None):
        item = self.scene().addSimpleText(
            "\n".join(text) if isinstance(text, list) else text, font or self.label_font
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
        elif alignment == "middle":
            item.setPos(anchor.x() - bounds.width() / 2, anchor.y() - bounds.height() / 2)
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

    def place_caption(self, text, p, color, probe, alignment="center", *, move_key=None, font=None):
        """Choose a nearby position clear of component labels and other captions."""
        item = self.label(text, p, color, probe, alignment, move_key=move_key, font=font)
        if move_key in self.label_offsets:
            return item
        base = item.pos()
        obstacles = [
            other.sceneBoundingRect().adjusted(-2, -1, 2, 1)
            for other in (*self.captions, *self.pin_items)
            if isinstance(other, QGraphicsSimpleTextItem) and other is not item
        ]
        step = item.boundingRect().height() + 2
        offsets = [QPointF()]
        if alignment == "center" and probe and probe[0] == "component":
            own_body = self.caption_bodies.get((self.level, probe[1]))
            if own_body is not None:
                offsets.append(QPointF(0, own_body.bottom() + 3 - item.sceneBoundingRect().top()))
        nearby = ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (1, -1), (-1, 1), (1, 1),
                  (0, -2), (0, 2), (-2, 0), (2, 0), (0, -3), (0, 3))
        offsets.extend(QPointF(dx * step, dy * step) for dx, dy in nearby)
        best, chosen = None, base
        for offset in offsets:
            item.setPos(base + offset)
            bounds = item.sceneBoundingRect()
            # Text bounds include a margin beyond the glyphs.
            text = bounds.adjusted(2, 2, -2, -2)
            bodies = sum(text.intersects(rect) for rect in self.caption_bodies.values())
            if best is not None and bodies > best[0]:
                continue
            collisions = sum(bounds.intersects(rect) for rect in obstacles)
            collisions += sum(other.data(0) == "wire" for other in self.scene().items(bounds))
            if not bodies and not collisions:
                return item
            score = bodies, collisions
            if best is None or score < best:
                best, chosen = score, item.pos()
        item.setPos(chosen)
        return item

    def ground(self, p, name="0", turns=0):
        x, y = p

        def turned(position):
            return rotate_point(position, p, p, turns)

        strokes = [self.lines(edge) for edge in ground_strokes(p, turns)]
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

    # Draw device-specific contacts and their labels from the same anchor geometry used
    # by routing and rotation; decorative glyphs do not introduce new electrical terminals.
    def symbol(self, part, a, b, top):
        if part.kind == "A":
            return self.audio_symbol(part, a, b, top)
        if part.kind == "O":
            x, y = a
            base, tip = opamp_body(a, b)
            middle = (a[0] + b[0]) / 2

            triangle = opamp_triangle(a, b)
            self.lines((*triangle, triangle[0]))
            offset = pin_offset(0.35, self.pin_grid)
            for yy in (offset, -offset):
                self.lines([(x, y + yy), (base, y + yy)])
            self.lines([(tip, y), b])
            self.label(top, (base + .45, y + offset), INK, alignment="right")
            self.label(
                "-" if top == "+" else "+", (base + .45, y - offset), INK, alignment="right"
            )
            if part.vplus and part.vminus:
                pitch = pin_offset(1.25, self.pin_grid)
                # Meet the triangle's sloping sides and keep the exposed ends
                # identical to symbol_pins(), including rotated symbols.
                edge = .72 * (tip - middle) / (tip - base)
                for net, sign, direction in ((part.vplus, "+V", 1), (part.vminus, "-V", -1)):
                    lead = self.lines([(middle, y + direction * edge), (middle, y + direction * pitch)])
                    lead.setData(0, "node")
                    lead.setData(1, net)
                    lead.setZValue(1.1)
                    lead.setToolTip(f"{sign} · {net}")
                    self.net_items[net].append(lead)
                    self.label(sign, (middle + .16, y + (.95 if direction > 0 else -.65)),
                               NET, ("node", net), alignment="left below")
                return (middle, y + 1.65), "center"
            return (middle, y + 0.77), "center"
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
            end = SWITCH_CONTACT_OFFSET
            path([(-end, 0), (end, 0 if part.closed else 0.48)])
            for xx in (-end, end):
                path([(xx + SWITCH_CONTACT_RADIUS * math.cos(math.tau * i / 16),
                       SWITCH_CONTACT_RADIUS * math.sin(math.tau * i / 16)) for i in range(17)])
        elif part.kind == "Z":
            path([(-end, -0.3), (end, -0.3), (end, 0.3), (-end, 0.3), (-end, -0.3)])
        elif part.kind == "D":
            end = 0.4
            path([(-end, -0.32), (-end, 0.32), (end, 0), (-end, -0.32)])
            kind = part.diode.kind if part.diode else "rectifier"
            if kind in {"zener", "avalanche"}:
                path([(end - .14, -.46), (end, -.32), (end, .32), (end + .14, .46)])
            elif kind in {"schottky", "tunnel"}:
                path([(end - .13, -.24), (end - .13, -.36), (end, -.36),
                      (end, .36), (end + .13, .36), (end + .13, .24)])
            else:
                path([(end, -.36), (end, .36)])
            if kind == "varactor":
                path([(end + .16, -.36), (end + .16, .36)])
                end += .16
            if kind in {"led", "photo"}:
                for offset in (-.18, .18):
                    start, stop = (offset - .1, -.45), (offset + .12, -.72)
                    if kind == "photo":
                        start, stop = stop, start
                    path([start, stop])
                    direction = 1 if kind == "led" else -1
                    path([(stop[0] - .15 * direction, stop[1] + .04 * direction), stop,
                          (stop[0] + .01 * direction, stop[1] + .16 * direction)])
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

    def audio_symbol(self, part, a, b, top):
        """Eight physical contacts; behavioral properties stay in the source."""
        x, y = a
        body, tip = x + 1.0, b[0] - .5
        height = .95
        self.lines([(body, y + height), (body, y - height), (tip, y), (body, y + height)])
        pins = list(symbol_pins(part, a, b, top, grid=self.pin_grid))
        for sign, (_, pin) in zip(("+", "-"), pins[:2]):
            self.lines([pin, (body, pin[1])])
            self.label(sign, (body + .35, pin[1]), INK, alignment="right")
        self.lines([(tip, y), b])
        for caption, (net, pin) in zip(("V+", "V-", "GAIN1", "GAIN8", "BYPASS"), pins[3:]):
            direction = 1 if pin[1] > y else -1
            edge = direction * height * (tip - pin[0]) / (tip - body)
            lead = self.lines([(pin[0], y + edge), pin])
            lead.setData(0, "node")
            lead.setData(1, net)
            lead.setZValue(1.1)
            lead.setToolTip(f"{caption} · {net}")
            self.net_items[net].append(lead)
            self.label(caption, (pin[0], pin[1] + .15 * direction), NET, ("node", net),
                       alignment="bottom" if direction < 0 else "center")
        return ((a[0] + b[0]) / 2, y + .1), "center"

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
            if kind in {"back-wire", "under-wire"}:
                return "wire", name
            if kind == "back-pin":
                return "node", name
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
                if item.data(0) in {"wire", "under-wire", "back-wire", "back-pin", "node", "pin", "ground", "connector", "boundary"}
            }
            if len(names) == 1:
                return "node", names.pop()
        return None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.place_pin and not event.modifiers():
            items = self.items(event.position().toPoint())
            board = any(isinstance(item.data(0), str) and item.data(0).startswith("perfboard")
                        for item in items)
            occupied = {"component", "hood", "wire", "under-wire", "back-wire", "back-pin",
                        "node", "pin", "ground", "connector", "boundary"}
            if board and not any(item.data(0) in occupied for item in items):
                self.toggle_underside()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if isinstance(self.itemAt(event.position().toPoint()), BehaviorDiagram):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            pos = event.position().toPoint()
            control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            if not self.place_pin:
                stop_at_terminal = False
                for item in self.items(pos):
                    kind, key = item.data(0), item.data(2)
                    if isinstance(kind, str) and kind.startswith("perfboard"):
                        continue
                    if kind == "back-pin":
                        self.highlight_net(item.data(1))
                        self.probed.emit("node", item.data(1))
                        event.accept()
                        return
                    if item.data(4) is not None and not control:
                        self.clear_selection()
                        self.start_drag(kind, item.data(1), tuple(key), pos, caption=item)
                        event.accept()
                        return
                    if kind in {"node", "pin"}:
                        stop_at_terminal = True
                        break
                    if key is not None:
                        name, key = item.data(1), tuple(key)
                        selectable = kind in {"component", "hood", "ground", "connector", "boundary"}
                        identity = self.selection_id(kind, name, item.data(3))
                        only_symbol = alt and kind in {"component", "hood"}
                        if control and not selectable:
                            continue
                        if only_symbol or not (selectable and (control or (
                            key == self.selection_key and identity in self.selected_symbols
                        ))):
                            self.clear_selection()
                        self.start_drag(
                            kind, name, key, pos, terminal=item.data(3),
                            control=control and selectable and not only_symbol, only_symbol=only_symbol,
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

    # Capture the original layout and pointer offset so every preview is a proposal from a
    # stable baseline rather than an accumulation of unchecked incremental moves.
    def start_drag(self, kind, name, key, pos, edge=None, *, terminal=None, control=False, caption=None,
                   only_symbol=False):
        layout = self.manual_layouts.get(key, self.level_layouts[key])
        selection = self.selected_symbols if key == self.selection_key else set()
        identity = self.selection_id(kind, name, terminal) if caption is None else None
        members = selection | {identity} if not only_symbol and (control or identity in selection) else ()
        moving = members or ((name,) if kind in {"component", "hood"} else ())
        stage_group = frozenset()
        carried = ()
        if caption is None and not only_symbol and layout.wires_visible:
            # An explicit selection is the user's group. Automatic stage
            # companions apply only when dragging one unselected component.
            expanded = frozenset(moving) if members else local_stage_selection(self.levels[key], layout, moving)
            if expanded != frozenset(moving):
                stage_group = expanded
            carried = attached_ground_islands(layout, expanded)
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
            if self.levels[key].perfboard is not None:
                symbol = next((symbol for symbol in layout.symbols if symbol[0].name == name), None)
                if symbol is not None:
                    anchor = routed_pins(symbol, layout.rotations.get(name, 0), layout.pin_grid)[0][1]
        origin = self.level_offsets.get(key, QPointF())
        anchor = anchor[0] + origin.x() / SCALE, anchor[1] - origin.y() / SCALE
        step = grid_step(max(self.transform().m11() * SCALE, 0.01))
        board = self.levels[key].perfboard is not None
        if board:
            # Contacts occupy whole holes; routing lanes and split ends can
            # lie between them. A whole-hole wire grid prevents jog collapse.
            step = 1 if kind in {"component", "hood"} else .5
        self.drag = SchematicDrag(
            kind, name, key, pos, layout, self.levels[key], edge=edge, terminal=terminal,
            control=control, selection=frozenset(members), stage_group=stage_group, attached_grounds=carried,
            caption=caption, caption_start=caption.pos() if caption is not None else None,
            grid_anchor=anchor, grid_step=step,
            keep_package_clearance=not board and layout.uses_packages and clear_package_wires(layout),
            mirrored=board and self._underside_at(pos),
            underside=board and kind == "wire" and (
                self._underside_at(pos) or wire_subset((edge,), layout.underside_wires.get(name, ()))),
        )

    def nearest_wire(self, pos, *, straight_only=True):
        if self.layout is not None and not self.layout.wires_visible:
            return None
        scene_pos = self.mapToScene(pos)
        mirrored = self._underside_at(pos)
        tolerance = 5 / max(self.transform().m11(), 0.01)
        best = tolerance
        match = None
        for key, offset in self.level_offsets.items():
            layout = self.manual_layouts.get(key, self.level_layouts[key])
            if not layout.wires_visible:
                continue
            board = self.levels[key].perfboard
            if mirrored and (key[0] is not None or board is None):
                continue
            if mirrored:
                wires = layer_wires(layout, underside=True)
            elif board is not None and layout.underside_wires:
                wires = {net: list(edges) for net, edges in layer_wires(layout).items()}
                for net, edges in layer_wires(layout, underside=True).items():
                    wires.setdefault(net, []).extend(edges)
            else:
                wires = layout.wires
            for net, edges in wires.items():
                for a, b in edges:
                    diagonal = a[0] != b[0] and a[1] != b[1]
                    if a == b or (straight_only and diagonal):
                        continue
                    if mirrored:
                        first, second = (point(board_point(board, at, underside=True, offset=underside_offset(board)))
                                         for at in (a, b))
                    else:
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

    # Prefer a valid snapped candidate and retain the previous valid preview when the current
    # pointer position would break a contact or short another net.
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
        if drag.mirrored:
            raw = -raw[0], raw[1]
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
            move = move_board_wire if drag.circuit.perfboard is not None else move_wire
            options = {"underside": drag.underside} if drag.circuit.perfboard is not None else {}
            drag.preview, drag.snapped_offset = move(
                drag.circuit, drag.original, drag.name, drag.edge, distance,
                snap=acquire, pointer=pointer,
                accept_offset=lambda offset: on_grid(drag.grid_anchor[0 if a[0] == b[0] else 1] + offset, drag.grid_step),
                repair=drag.circuit.perfboard is not None,
                **options,
            )
        elif not drag.original.wires_visible and drag.kind in {"component", "hood"}:
            # Placement-first mode keeps the old route reference for Add wires,
            # and does no routing work while parts are being arranged.
            names = set(drag.moving_selection) or {drag.name}
            symbols = [(part, (a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy), top)
                       if part.name in names else (part, a, b, top)
                       for part, a, b, top in drag.original.symbols]
            drag.preview = replace(drag.original, symbols=symbols,
                                   positions={part.name: a for part, a, _, _ in symbols})
        elif drag.circuit.perfboard is not None and drag.kind in {"component", "hood"}:
            # Board edits may be unfinished. Move physical contacts on the hole
            # grid without rebuilding unrelated routes or rotating other parts.
            names = set(drag.moving_selection) or {drag.name}
            drag.preview = move_perfboard_parts(drag.circuit, drag.original, names, (dx, dy))
        elif len(drag.moving_selection) > 1:
            drag.preview = move_symbols(
                drag.circuit, drag.original, drag.moving_selection, (dx, dy),
                diagonal=drag.diagonal, ground_islands=drag.attached_grounds,
            )
        elif drag.kind == "connector":
            self.preview_connector_drag(drag, (dx, dy), raw, pixels_per_unit)
        elif drag.kind == "boundary":
            drag.preview = self.endpoint_preview(
                drag, (dx, dy), raw, lambda offset: move_boundary(
                    drag.circuit, drag.original, drag.name, offset, diagonal=drag.diagonal,
                ),
            )
        else:
            self.preview_symbol_drag(drag, (dx, dy), raw, pixels_per_unit)
        if drag.preview is not None and drag.circuit.perfboard is None and drag.keep_package_clearance and not clear_package_wires(drag.preview):
            drag.preview = None
            drag.snapped_offset = drag.snapped_terminal = None
        drag.blocked = drag.preview is None
        if drag.blocked:
            drag.preview = previous or drag.original
        elif drag.circuit.perfboard is not None:
            drag.preview = remap_wire_layers(drag.original, drag.preview,
                                            net=drag.name if drag.kind == "wire" else None,
                                            underside=drag.underside if drag.kind == "wire" else None)
        self._redraw(preview=drag, selected=drag.name if drag.kind == "wire" else self.highlight)
        self.viewport().setCursor(Qt.CursorShape.ForbiddenCursor if drag.blocked else Qt.CursorShape.ClosedHandCursor)

    @staticmethod
    def endpoint_preview(drag, delta, raw, propose):
        """Use a nearby grid point when the new marker would touch a different net."""
        dx, dy = delta
        step = drag.grid_step
        nearby = ((dx - step, dy), (dx + step, dy), (dx, dy - step), (dx, dy + step))
        for offset in (delta, *sorted(nearby, key=lambda candidate: math.dist(raw, candidate))):
            drawing = propose(offset)
            if drawing is None or (offset != delta and drawing is drag.original):
                continue
            if not drag.keep_package_clearance or clear_package_wires(drawing):
                return drawing
        return None

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
                drag.circuit, drag.original, index, pos, join=i, diagonal=drag.diagonal,
                repair=drag.circuit.perfboard is not None,
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
                drag.circuit, drag.original, index, pos, join_wire=True, diagonal=drag.diagonal,
                repair=drag.circuit.perfboard is not None,
            )
            if drawing is not None:
                drag.preview = drawing
                return
        drag.preview = self.endpoint_preview(
            drag, delta, raw, lambda offset: move_connector(
                drag.circuit, drag.original, index,
                tuple(a + b for a, b in zip(start, offset)), diagonal=drag.diagonal,
                repair=drag.circuit.perfboard is not None,
            ),
        )

    def preview_symbol_drag(self, drag, delta, raw, pixels_per_unit):
        ground = tuple(drag.terminal) if drag.kind == "ground" else None
        move = (partial(move_ground, repair=drag.circuit.perfboard is not None) if ground is not None
                else partial(move_symbol, ground_islands=drag.attached_grounds))
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
            drawing = move_symbol(
                drag.circuit, drag.original, drag.name, offset, ground_islands=drag.attached_grounds,
            )
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
            horizontal, vertical = self.horizontalScrollBar(), self.verticalScrollBar()
            previous = horizontal.value(), vertical.value()
            super().mouseMoveEvent(event)
            if previous != (horizontal.value(), vertical.value()):
                self.fitted = False
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

    # A completed drag commits geometry; a click may probe or request a source-backed control
    # change. Keep those paths distinct to avoid toggling a switch while moving it.
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
                    elif part and part.kind == 'K' and drag.caption is None:
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
                    self.selected_symbols = self.drag_selection(drag, drag.preview)
                self.placement_changed.emit()
            focus = self.mapToScene(self.viewport().rect().center())
            self._redraw(focus, selected=self.highlight)
        self.viewport().unsetCursor()
        event.accept()

    # Offer actions for the selected object's actual capabilities and emit source-edit requests
    # rather than changing electrical connections directly in the graphics scene.
    def _value_label_parts(self, key):
        labels = component_labels(self.levels[key])
        return {part.name for part in self.levels[key].components
                if part.kind != "H" and len(labels[part.name]) > 1}

    def set_values_only(self, enabled, *, key=None, name=None):
        """Hide component titles without changing their values or geometry."""
        if not self.isEnabled() or self.drag is not None:
            return
        changed = {}
        for target in ([key] if key is not None else self.levels):
            eligible = self._value_label_parts(target)
            selected = eligible & {name} if name is not None else eligible
            current = self.manual_layouts.get(target, self.level_layouts[target])
            compact = current.compact_labels | selected if enabled else current.compact_labels - selected
            if compact != current.compact_labels:
                changed[target] = replace(current, compact_labels=frozenset(compact))
        if not changed:
            return
        self.remember_placements(changed)
        self.manual_layouts.update(changed)
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=self.highlight)
        self.placement_changed.emit()

    def _label_menu(self, menu, key=None, name=None):
        if key is not None and name in self._value_label_parts(key):
            current = self.manual_layouts.get(key, self.level_layouts[key])
            compact = name in current.compact_labels
            action = menu.addAction("Values only", lambda: self.set_values_only(not compact, key=key, name=name))
            action.setCheckable(True)
            action.setChecked(compact)
        targets = {key: self._value_label_parts(key) for key in self.levels}
        if any(targets.values()):
            compact = all(names <= self.manual_layouts.get(key, self.level_layouts[key]).compact_labels
                          for key, names in targets.items())
            action = menu.addAction("Values only (all components)", lambda: self.set_values_only(not compact))
            action.setCheckable(True)
            action.setChecked(compact)

    def set_wires_visible(self, visible):
        """Arrange board parts freely, then reconnect their current placement."""
        key = None, self.layout_mode
        circuit = self.levels.get(key)
        if (not self.isEnabled() or self.drag is not None or circuit is None
                or circuit.perfboard is None):
            return
        current = self.manual_layouts.get(key, self.level_layouts[key])
        if visible == current.wires_visible:
            return
        reassigned = 0
        if not visible:
            reference = {part.name: (a, b, current.rotations.get(part.name, 0))
                         for part, a, b, _ in current.symbols}
            drawing = replace(current, wires_visible=False, wiring_positions=reference)
        else:
            old_symbols = [(part, *current.wiring_positions[part.name][:2], top)
                           for part, _, _, top in current.symbols]
            reference = replace(current, symbols=old_symbols,
                                positions={part.name: a for part, a, _, _ in old_symbols},
                                rotations={name: turns for name, (_, _, turns) in current.wiring_positions.items()
                                           if turns}, wires_visible=True, wiring_positions={})
            placement = replace(current, wires_visible=True, wiring_positions={})
            assigned = assign_interchangeable_parts(circuit, placement, reference=reference)
            reassigned = sum(before != after for before, after in zip(placement.symbols, assigned.symbols))
            drawing = reroute_perfboard(circuit, assigned, reference=reference, editable=True)
            drawing = remap_wire_layers(reference, drawing)
        self._commit_layout_edit(key, drawing, selected=self.highlight)
        message = ("Wires added from the current placement." if visible else
                   "Wires removed. Arrange parts, then right-click and choose Add wires.")
        if reassigned:
            message = (f"Wires added; {reassigned} matching parts assigned to nearer connections. "
                       "Schematic names updated. Ctrl+Z restores the previous layout.")
        self.optimization_finished.emit(self.layout_issues[0].message if self.layout_issues else message)

    def _wiring_menu(self, menu):
        key = None, self.layout_mode
        circuit = self.levels.get(key)
        if circuit is not None and circuit.perfboard is not None:
            visible = self.manual_layouts.get(key, self.level_layouts[key]).wires_visible
            menu.addAction("Remove all wires" if visible else "Add wires",
                           lambda: self.set_wires_visible(not visible))
            menu.addAction("Hide underside" if self.underside_visible else "Show underside", self.toggle_underside)

    def set_board_wire_layer(self, key, net, *, edge=None, pin=None, location=None, underside=True):
        """Transfer one physical branch between board faces, without changing its net."""
        if not self.isEnabled() or self.drag is not None or key not in self.levels:
            return
        if self.levels[key].perfboard is None:
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        drawing = set_wire_layer(original, net, edge=edge, pin=pin, point=location, underside=underside)
        if drawing == original:
            return
        if underside and not self.underside_visible:
            self.underside_visible = True
            self.fitted = True
        self._commit_layout_edit(key, drawing, selected=net)
        self.optimization_finished.emit("Wire moved underneath the board." if underside else "Wire moved to the component side.")

    def _wire_layer_menu(self, menu, event, wire=None, *, net=None):
        key = wire[0] if wire else (None, self.layout_mode)
        circuit = self.levels.get(key)
        if circuit is None or circuit.perfboard is None:
            return
        drawing = self.manual_layouts.get(key, self.level_layouts[key])
        if not drawing.wires_visible:
            return
        location = self._board_location(event.pos(), key)
        if net is None and wire:
            net = wire[1]
        pin = None
        tolerance = 7 / max(self.transform().m11() * SCALE, .01)
        contacts = [(math.dist(location, at), name, at) for name, at in drawing.pins()
                    if net is None or name == net]
        if contacts:
            distance, pin_net, at = min(contacts)
            if distance <= tolerance:
                net, pin = pin_net, at
        if net is None or (pin is None and wire is None):
            return
        edge = wire[2] if wire and pin is None else None
        back = set_wire_layer(drawing, net, edge=edge, pin=pin, point=location, underside=True)
        front = set_wire_layer(drawing, net, edge=edge, pin=pin, point=location, underside=False)
        if back != drawing:
            menu.addAction("Wire underneath", lambda: self.set_board_wire_layer(
                key, net, edge=edge, pin=pin, location=location, underside=True))
        if front != drawing:
            menu.addAction("Wire on top", lambda: self.set_board_wire_layer(
                key, net, edge=edge, pin=pin, location=location, underside=False))

    def _selected_members(self, key, name=None):
        if self.selection_key == key and (name is None or name in self.selected_symbols):
            return set(self.selected_symbols)
        return {name} if name is not None else set()

    def _length_targets(self, key, name=None):
        if key not in self.levels or self.levels[key].perfboard is None:
            return []
        drawing = self.manual_layouts.get(key, self.level_layouts[key])
        members = self._selected_members(key, name)
        return [symbol for symbol in drawing.symbols
                if symbol[0].name in members and symbol[0].kind in TWO_TERMINAL]

    def adjust_component_length(self, key, name=None):
        """Apply one hole span to the clicked part or its explicit selection."""
        if not self.isEnabled() or self.drag is not None:
            return
        circuit = self.levels.get(key)
        if circuit is None or circuit.perfboard is None:
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        targets = self._length_targets(key, name)
        if not targets:
            return
        symbol = next((symbol for symbol in targets if symbol[0].name == name), targets[0])
        contacts = [at for _, at in routed_pins(symbol, original.rotations.get(symbol[0].name, 0), original.pin_grid)]
        current = max(3, min(256, round(math.dist(*contacts)) + 1))
        subject = f" for {len(targets)} selected parts" if len(targets) > 1 else ""
        holes, accepted = QInputDialog.getInt(
            self, "Adjust length", f"Pin length{subject} (holes, including both ends):", current, 3, 256, 1,
        )
        if not accepted:
            return
        try:
            drawing = resize_perfboard_parts(circuit, original, {symbol[0].name for symbol in targets},
                                             holes, route=original.wires_visible)
        except CircuitError as error:
            self.optimization_finished.emit(error.message)
            return
        if drawing is not original:
            self._commit_layout_edit(key, drawing, selected=self.highlight)

    def _length_menu(self, menu, key=None, name=None):
        key = self.selection_key if key is None else key
        if self._length_targets(key, name):
            menu.addAction("Adjust length…", lambda: self.adjust_component_length(key, name))

    def _selection_transform_menu(self, menu):
        key = self.selection_key
        if key not in self.levels:
            return
        current = self.manual_layouts.get(key, self.level_layouts[key])
        rotatable = {part.name for part, *_ in current.symbols}
        rotatable.update(("ground", pos) for pos in current.grounds)
        if self.selected_symbols & rotatable:
            menu.addAction("Rotate clockwise", lambda: self.rotate_selection(key))
            menu.addAction("Rotate counterclockwise", lambda: self.rotate_selection(key, direction=-1))
        self._length_menu(menu)

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
                    self._wire_layer_menu(menu, event, self.nearest_wire(event.pos(), straight_only=False), net=net)
                    self._length_menu(menu)
                    self._label_menu(menu)
                    self._wiring_menu(menu)
                    menu.exec(event.globalPos())
                    return
                if kind in {"node", "pin", "connector", "boundary", "back-pin"}:
                    break
                if key is not None and kind in {"component", "hood"}:
                    name = item.data(1)
                    key = tuple(key)
                    menu = QMenu(self)
                    part = next((p for p in self.levels[key].components if p.name == name), None)
                    if part and self.source_editing and kind == 'component':
                        if part.kind in {'R', 'C', 'L', 'Z', 'P', 'D'}:
                            menu.addAction('Edit value…', lambda: self.value_requested.emit(name))
                        if part.kind == 'P':
                            menu.addAction('Adjust position…', lambda: self.potentiometer_requested.emit(name))
                        if part.kind == 'K':
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
                        "Rotate clockwise", lambda: self.rotate_component(key, name)
                    )
                    menu.addAction(
                        "Rotate counterclockwise", lambda: self.rotate_component(key, name, direction=-1)
                    )
                    if part and part.kind in {"O", "A"}:
                        menu.addAction("Flip +/− inputs", lambda: self.flip_opamp(key, name))
                    self._length_menu(menu, key, name)
                    self._label_menu(menu, key, name)
                    self._wiring_menu(menu)
                    menu.addAction("Optimize wiring", self.optimize_wiring)
                    menu.exec(event.globalPos())
                    return
            wire = self.nearest_wire(event.pos(), straight_only=False)
        target = self.target(event.pos(), nets_only=True)
        if not target:
            menu = QMenu(self)
            self._selection_transform_menu(menu)
            self._wire_layer_menu(menu, event, wire)
            self._label_menu(menu)
            self._wiring_menu(menu)
            menu.addAction("Optimize wiring", self.optimize_wiring)
            menu.exec(event.globalPos())
            return
        kind, net = target
        menu = QMenu(self)
        self._selection_transform_menu(menu)
        self._wire_layer_menu(menu, event, wire, net=net)
        if kind == "pin":
            menu.addAction("Rename pin…", lambda: self.pin_renamed.emit(net))
            menu.addAction("Remove pin", lambda: self.pin_removed.emit(net))
        else:
            menu.addAction("Add pin…", lambda: self.pin_requested.emit(net))
            if wire and wire[1] == net:
                location = self._board_location(event.pos(), wire[0])
                menu.addAction("Split", lambda: self.split_connection(*wire, location))
        self._label_menu(menu)
        self._wiring_menu(menu)
        menu.addAction("Optimize wiring", self.optimize_wiring)
        menu.exec(event.globalPos())

    def _commit_layout_edit(self, key, drawing, *, selected, rotation=None):
        """Store one undoable layout edit and refresh the current view."""
        original = self.manual_layouts.get(key, self.level_layouts[key])
        if self.levels[key].perfboard is not None:
            drawing = remap_wire_layers(original, drawing)
        self.remember_placement(key)
        self.manual_layouts[key] = drawing
        if rotation is None:
            self.rotation_bases.pop(key, None)
        else:
            self.rotation_bases[key] = rotation
        self.placement_changed.emit()
        self._redraw(self.mapToScene(self.viewport().rect().center()), selected=selected)

    def split_connection(self, key, net, edge, location):
        if not self.isEnabled():
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        offset = self.level_offsets.get(key, QPointF())
        drawing = split_wire(self.levels[key], original, net, edge, location,
                             grid=.5 if self.levels[key].perfboard is not None else grid_step(max(self.transform().m11() * SCALE, 0.01)),
                             origin=(offset.x() / SCALE, -offset.y() / SCALE),
                             repair=self.levels[key].perfboard is not None)
        if drawing is None:
            self.move_refused.emit()
            return
        self._commit_layout_edit(key, drawing, selected=net)

    def rotate_ground(self, key, point, *, direction=1):
        self.rotate_selection(key, ("ground", point), direction=direction)

    def rotate_component(self, key, name, *, direction=1):
        self.rotate_selection(key, name, direction=direction)

    def rotate_selection(self, key, name=None, *, direction=1):
        """Rotate explicit members together, each around its own center."""
        if not self.isEnabled() or self.drag is not None or key not in self.levels:
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        members = self._selected_members(key, name)
        parts = frozenset(part.name for part, *_ in original.symbols if part.name in members)
        grounds = {pos for pos in original.grounds if ("ground", pos) in members}
        identity = parts | frozenset(("ground", pos) for pos in grounds)
        if not identity:
            return
        previous, base, turns = self.rotation_bases.get(key, (identity, original, 0))
        if previous != identity:
            base, turns = original, 0
        turns = (turns + direction) % 4
        drawing = base
        if parts and turns:
            if self.levels[key].perfboard is not None:
                drawing = rotate_perfboard_parts(self.levels[key], base, parts, turns, route=base.wires_visible)
            elif len(parts) > 1:
                drawing = rotate_symbols(self.levels[key], base, parts, turns)
            else:
                part, = parts
                drawing = move_symbol(self.levels[key], base, part, (0, 0), rotate=turns)
            # A pin may land on a different net at the exact pivot, as
            # when flipping a lone part in an end-to-end chain.
            if drawing is None and len(parts) == 1:
                part, = parts
                for offset in ((0, 1.5), (0, -1.5), (1.5, 0), (-1.5, 0),
                               (0, 2.5), (0, -2.5), (2.5, 0), (-2.5, 0)):
                    drawing = move_symbol(self.levels[key], base, part, offset, rotate=turns)
                    if drawing is not None:
                        break
        if drawing is None:
            self.move_refused.emit()
            return
        if grounds and turns:
            rotations = dict(base.ground_rotations)
            for pos in grounds:
                angle = (rotations.pop(pos, 0) + turns) % 4
                if angle:
                    rotations[pos] = angle
            drawing = replace(drawing, ground_rotations=rotations)
        if drawing is not original:
            self._commit_layout_edit(key, drawing, selected=self.highlight, rotation=(identity, base, turns))

    def flip_opamp(self, key, name):
        if not self.isEnabled():
            return
        original = self.manual_layouts.get(key, self.level_layouts[key])
        drawing = flip_opamp_inputs(self.levels[key], original, name)
        if drawing is None:
            self.move_refused.emit()
            return
        self._commit_layout_edit(key, drawing, selected=self.highlight)
