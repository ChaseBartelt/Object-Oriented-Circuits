"""Run or pause a built-in time simulation and plot a circuit node."""
# The scope owns bounded sampling work and a selected-node view of stored simulation history.
# Pause/resume preserves physical state; value-only updates can restamp a compatible circuit
# without treating a slider gesture or a new monitored node as a fresh simulation.

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import math
from threading import Event

from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from circuitlab.model.graph import CircuitError
from circuitlab.math.nonlinear import TimeDomain, source_frequency


# Bound each worker batch and copy returned voltage arrays before the next step mutates them.
# The cancellation event lets edits and shutdown stop work between samples.
def sample_batch(simulation, stop):
    samples = []
    error = None
    for _ in range(12):
        if stop.is_set():
            break
        try:
            voltage = simulation.advance()
        except CircuitError as failure:
            error = failure
            break
        samples.append((simulation.time, voltage.copy()))
    return samples, error


class Waveform(QWidget):
    voltage_auto_changed = Signal(bool)
    VOLTAGE_DIVISIONS = 5

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(145)
        self.setMouseTracking(True)
        self.samples = deque(maxlen=600)
        self.auto_voltage = True
        self._manual_voltage = None
        self._voltage_drag = None
        self.time_label, self.time_scale = "µs", 1e-6
        self.setToolTip("Drag the left voltage axis up to zoom in or down to zoom out. Double-click it, or use Auto V, to fit the signal again.")

    def plot_area(self):
        return QRectF(self.rect()).adjusted(78, 24, -15, -25)

    def set_time_unit(self, label, scale):
        """Change only axis labels; stored sample times always remain seconds."""
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("A time unit must have a finite positive scale.")
        self.time_label, self.time_scale = label, scale
        self.update()

    def voltage_bounds(self):
        """Keep a manual viewport independent of incoming samples and resets."""
        if not self.auto_voltage and self._manual_voltage is not None:
            return self._manual_voltage
        values = [value for _, value in self.samples if math.isfinite(value)]
        low, high = (min(values), max(values)) if values else (-.5, .5)
        center = low / 2 + high / 2
        spread = high - low
        desired = min(1e308, spread * 1.1) if spread > max(1e-9, abs(center) * 1e-12) else 1.0
        desired = max(desired, 16 * math.ulp(center))
        raw_step = desired / self.VOLTAGE_DIVISIONS
        magnitude = 10 ** math.floor(math.log10(raw_step))
        step = next(factor * magnitude for factor in (1, 2, 5, 10) if factor * magnitude >= raw_step)
        return self._centered_bounds(center, step * self.VOLTAGE_DIVISIONS)

    @staticmethod
    def _centered_bounds(center, span):
        # Keep finite, distinct bounds even for a large DC offset where adding
        # half a volt is smaller than one representable floating-point step.
        span = max(span, 16 * math.ulp(center))
        ceiling = math.nextafter(math.inf, 0)
        return max(-ceiling, center - span / 2), min(ceiling, center + span / 2)

    def set_auto_voltage(self, enabled=True):
        enabled = bool(enabled)
        if enabled == self.auto_voltage:
            return
        if not enabled:
            self._manual_voltage = self.voltage_bounds()
        self.auto_voltage = enabled
        self._voltage_drag = None
        self.voltage_auto_changed.emit(enabled)
        self.update()

    def _on_voltage_axis(self, position):
        area = self.plot_area()
        return 0 <= position.x() < area.left() and area.top() - 8 <= position.y() <= area.bottom() + 8

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_voltage_axis(event.position()):
            low, high = self.voltage_bounds()
            self.set_auto_voltage(False)
            self._voltage_drag = event.position().y(), low, high
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._voltage_drag is not None:
            start, low, high = self._voltage_drag
            center = low / 2 + high / 2
            factor = math.exp(max(-20, min(20, (event.position().y() - start) / 120)))
            minimum = max(1e-9, 16 * math.ulp(center))
            span = max(minimum, min(1e12, (high - low) * factor))
            self._manual_voltage = self._centered_bounds(center, span)
            self.update()
            event.accept()
            return
        self.setCursor(Qt.CursorShape.SizeVerCursor if self._on_voltage_axis(event.position())
                       else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._voltage_drag is not None:
            self._voltage_drag = None
            self.setCursor(Qt.CursorShape.SizeVerCursor if self._on_voltage_axis(event.position())
                           else Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._on_voltage_axis(event.position()):
            self.set_auto_voltage(True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        if self._voltage_drag is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#333333"))
        area = self.plot_area()
        if area.width() <= 0 or area.height() <= 0:
            return
        painter.fillRect(area, QColor("#2d2d2d"))
        low, high = self.voltage_bounds()
        painter.setPen(QColor("#484848"))
        for division in range(1, self.VOLTAGE_DIVISIONS):
            y = area.bottom() - division * area.height() / self.VOLTAGE_DIVISIONS
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        for division in range(1, 10):
            x = area.left() + division * area.width() / 10
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        painter.setPen(QColor("#929292"))
        painter.drawRect(area)
        painter.setPen(QColor("#dedede"))
        metrics = painter.fontMetrics()
        for division in range(self.VOLTAGE_DIVISIONS + 1):
            value = low + (high - low) * division / self.VOLTAGE_DIVISIONS
            # Avoid a distracting -0 caused solely by endpoint roundoff.
            if abs(value) < (high - low) * 1e-12:
                value = 0.0
            text = f"{value:.4g} V"
            y = area.bottom() - division * area.height() / self.VOLTAGE_DIVISIONS
            label = QRectF(2, y - metrics.height() / 2, area.left() - 10, metrics.height())
            painter.drawText(label, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        painter.drawText(QPointF(area.left(), 16), f"{(high-low)/self.VOLTAGE_DIVISIONS:.3g} V/div")
        if len(self.samples) < 2:
            return
        first, last = self.samples[0][0], self.samples[-1][0]
        painter.drawText(QPointF(area.left(), area.bottom() + 18), f"{first / self.time_scale:.4g} {self.time_label}")
        end_label = f"{last / self.time_scale:.4g} {self.time_label}"
        painter.drawText(QPointF(max(area.left(), area.right() - metrics.horizontalAdvance(end_label)), area.bottom() + 18), end_label)
        duration = last - first
        if duration <= 0:
            return
        path = QPainterPath()
        for i, (t, v) in enumerate(self.samples):
            x = area.left() + (t - first) / duration * area.width()
            fraction = (v / 2 - low / 2) / ((high - low) / 2)
            y = area.bottom() - max(-1, min(2, fraction)) * area.height()
            (path.lineTo if i else path.moveTo)(x, y)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#69bcf6"))
        # Manual zoom may put the waveform outside the viewport; its trace
        # must never paint over the axis values or neighboring controls.
        painter.setClipRect(area.adjusted(1, 1, -1, -1))
        painter.drawPath(path)


class Oscilloscope(QWidget):
    def __init__(self, *, compact=False):
        super().__init__()
        self.panel_visible = True
        self.circuit = None
        self.simulation = None
        self.frequency = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="waveform")
        self.pending = None
        self.history = deque(maxlen=600)
        self.history_nodes = ()
        self.stop_batch = Event()
        self.timer = QTimer(self)
        self.timer.setInterval(35)
        self.timer.timeout.connect(self.advance)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(15)
        self.refresh_timer.timeout.connect(self.refresh_simulation)
        self.waveform = Waveform()
        self.node = QComboBox()
        self.initial = QComboBox()
        self.voltage = QDoubleSpinBox()
        self.voltage.setRange(-100, 100)
        self.voltage.setSuffix(" V")
        self.step = QDoubleSpinBox()
        self.step.setDecimals(6)
        self.step.setRange(0.000001, 1e11)
        self.step.setValue(10)
        self.step.setMinimumWidth(0)
        self.step.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._step_scale = 1e-6
        self._step_seconds = 10 * self._step_scale
        self.time_unit = QComboBox()
        for unit, scale in (("ns", 1e-9), ("µs", 1e-6), ("ms", 1e-3), ("s", 1.0)):
            self.time_unit.addItem(unit, scale)
        self.time_unit.setCurrentIndex(1)
        self.time_unit.setFixedWidth(58)
        self.time_unit.setToolTip("Time-step and time-axis units. Changing units keeps the same physical time step.")
        self.step.setToolTip("Interval between simulated samples, in the selected time unit.")
        self.waveform.set_time_unit("µs", self._step_scale)
        self.run = QPushButton("Run")
        self.run.clicked.connect(self.toggle)
        self.reset = QPushButton("Reset")
        self.reset.clicked.connect(self.restart)
        self.auto_voltage = QPushButton("Auto V")
        self.auto_voltage.setCheckable(True)
        self.auto_voltage.setChecked(True)
        self.auto_voltage.setFixedWidth(72)
        self.auto_voltage.setToolTip("Fit the voltage scale automatically. Drag the plot's left axis to choose a fixed scale; double-click the axis to restore Auto V.")
        self.auto_voltage.toggled.connect(self.waveform.set_auto_voltage)
        self.waveform.voltage_auto_changed.connect(self.auto_voltage.setChecked)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if compact:
            self.setMinimumWidth(280)
            for combo in (self.node, self.initial):
                combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(10)
            row = QHBoxLayout()
            row.addWidget(QLabel("Oscilloscope"))
            row.addStretch()
            row.addWidget(self.auto_voltage)
            row.addWidget(self.run)
            row.addWidget(self.reset)
            layout.addLayout(row)
            fields = QFormLayout()
            fields.addRow("Node", self.node)
            time_step = QHBoxLayout()
            time_step.setSpacing(5)
            time_step.addWidget(self.step, 1)
            time_step.addWidget(self.time_unit)
            fields.addRow("Time step", time_step)
            fields.addRow("Initial capacitor", self.initial)
            fields.addRow("Initial voltage", self.voltage)
            layout.addLayout(fields)
        else:
            for controls in (
                (self.run, self.reset, self.auto_voltage, QLabel("Node"), self.node, QLabel("Time step"), self.step, self.time_unit),
                (QLabel("Initial capacitor"), self.initial, QLabel("Initial voltage"), self.voltage),
            ):
                row = QHBoxLayout()
                for control in controls:
                    row.addWidget(control, 1 if control is self.step else 0)
                row.addStretch()
                layout.addLayout(row)
        layout.addWidget(self.waveform, 1)
        layout.addWidget(self.message)
        self.node.currentIndexChanged.connect(self.show_node)
        self.step.valueChanged.connect(self.change_step)
        self.time_unit.currentIndexChanged.connect(self.change_time_unit)
        for control in (self.initial, self.voltage):
            control.setToolTip("Initial conditions apply after Reset; changing them does not interrupt a running circuit.")
        self.hide()

    def set_panel_visible(self, visible):
        self.panel_visible = visible
        self.update_visibility()

    def step_seconds(self):
        """Return canonical seconds, independent of the chosen display unit."""
        return self._step_seconds

    def _set_step_seconds(self, seconds):
        # Keep the physical value independently of decimal display rounding.
        # Unit-only changes must not restamp a running solver by a few ULPs.
        self._step_seconds = min(100000.0, max(1e-12, seconds))
        with QSignalBlocker(self.step):
            self.step.setValue(self._step_seconds / self._step_scale)

    def change_time_unit(self, *_):
        """Convert the display and axis, leaving simulation/history untouched."""
        self._step_scale = self.time_unit.currentData()
        with QSignalBlocker(self.step):
            self.step.setDecimals((3, 6, 9, 12)[self.time_unit.currentIndex()])
            self.step.setRange(1e-12 / self._step_scale, 100000 / self._step_scale)
            self.step.setValue(self._step_seconds / self._step_scale)
        self.waveform.set_time_unit(self.time_unit.currentText(), self._step_scale)

    def update_visibility(self):
        circuit = self.circuit
        supported = circuit is not None and circuit.mode in {"dc", "ac", "mixed"} and bool(circuit.components or circuit.behavior)
        self.setVisible(supported and self.panel_visible)
        return supported

    # Preserve the selected node and compatible history during value-only refreshes.
    # Block widget signals while repopulating controls so setup does not trigger extra resets.
    def set_circuit(self, circuit, *, preserve_state=False):
        if circuit is self.circuit:
            return
        previous = self.node.currentText(), self.initial.currentData()
        self.circuit = circuit
        supported = self.update_visibility()
        if not preserve_state or not supported:
            self.restart()
        with QSignalBlocker(self.node), QSignalBlocker(self.initial), QSignalBlocker(self.step):
            self.node.clear()
            self.initial.clear()
            self.initial.addItem("None", None)
            if supported:
                self.node.addItems(net for net in circuit.simulation_nodes if net not in circuit.grounds)
                index = self.node.findText(previous[0])
                if index < 0:
                    index = self.node.findText("out")
                self.node.setCurrentIndex(max(0, index))
                for part in circuit.components:
                    if part.kind == "C" and part.name not in circuit.modeled_members:
                        self.initial.addItem(part.label, part.name)
                self.initial.setCurrentIndex(max(0, self.initial.findData(previous[1])))
                try:
                    frequency = source_frequency(circuit)
                    if not preserve_state and frequency is not None and frequency != self.frequency:
                        self._set_step_seconds(1 / (100 * frequency))
                    self.frequency = frequency
                except CircuitError as error:
                    self.frequency = None
                    self.message.setText(str(error))
        self.show_node()
        if preserve_state and supported and self.simulation is not None:
            self.stop_batch.set()
            self.refresh_simulation()

    def show_node(self, *_):
        """Change probes using the same recorded times, without touching the solver."""
        self.waveform.samples.clear()
        node = self.node.currentText()
        if node in self.history_nodes:
            index = self.history_nodes.index(node)
            self.waveform.samples.extend((time, voltage[index]) for time, voltage in self.history)
        self.waveform.update()

    def change_step(self, *_):
        self._step_seconds = self.step.value() * self._step_scale
        if self.simulation is not None:
            self.stop_batch.set()
            self.refresh_simulation()

    # Accept results only from the current simulation owner. Reorder stored voltage columns
    # when compatible source edits reorder nodes without changing their identities.
    def finish_batch(self):
        if self.pending is None:
            return True
        owner, future = self.pending
        if not future.done():
            return False
        self.pending = None
        samples, error = future.result()
        if owner is self.simulation:
            nodes = tuple(owner.nodes)
            if self.history and nodes != self.history_nodes:
                # Compatible edits may reorder declarations, but keep net identities.
                indices = [self.history_nodes.index(node) for node in nodes]
                self.history = deque(((time, voltage[indices]) for time, voltage in self.history), maxlen=600)
            self.history_nodes = nodes
            self.history.extend(samples)
            self.show_node()
            if error is not None:
                self.pause()
                self.message.setText(f"{error} Try a smaller time step or set an initial capacitor voltage.")
        return True

    # Wait for the worker to release its state before restamping values or the time step.
    # An incompatible topology requires a reset; a value edit can preserve stored energy.
    def refresh_simulation(self):
        """Apply the newest value change only after the worker releases its state."""
        if not self.finish_batch():
            self.refresh_timer.start()
            return False
        self.refresh_timer.stop()
        simulation = self.simulation
        step = self.step_seconds()
        if simulation is not None and (simulation.circuit is not self.circuit or simulation.step != step):
            try:
                self.simulation = simulation.reconfigure(self.circuit, step)
                self.message.clear()
            except CircuitError as error:
                if error.code == "TopologyChanged":
                    self.restart()
                else:
                    self.pause()
                self.message.setText(str(error))
                return False
        return True

    def pause(self):
        self.timer.stop()
        self.stop_batch.set()
        self.run.setText("Resume" if self.simulation else "Run")

    # Stop UI timers and cancel queued sampling work before the widget is released.
    def shutdown(self):
        self.pause()
        self.refresh_timer.stop()
        self.worker.shutdown(wait=False, cancel_futures=True)

    def restart(self):
        self.refresh_timer.stop()
        self.simulation = None
        self.pause()
        self.history.clear()
        self.history_nodes = ()
        self.waveform.samples.clear()
        self.waveform.update()
        self.message.clear()

    def toggle(self):
        if self.timer.isActive():
            self.pause()
            return
        if not self.circuit or not self.node.currentText():
            return
        try:
            if self.simulation is None:
                simulation = TimeDomain(self.circuit, self.step_seconds())
                if self.initial.currentData():
                    simulation.set_initial_voltage(self.initial.currentData(), self.voltage.value())
                self.simulation = simulation
            self.message.clear()
            self.stop_batch = Event()
            self.timer.start()
            self.run.setText("Pause")
            self.advance()
        except CircuitError as error:
            self.message.setText(str(error))

    def advance(self):
        if not self.timer.isActive():
            return
        if not self.refresh_simulation() or not self.timer.isActive():
            return
        simulation = self.simulation
        if simulation is None:
            return
        self.stop_batch = Event()
        self.pending = simulation, self.worker.submit(sample_batch, simulation, self.stop_batch)


class ScopeSidebar(QSplitter):
    """Give the schematic the full width whenever the scope is closed."""

    def __init__(self, schematic):
        super().__init__(Qt.Orientation.Horizontal)
        self.scope = Oscilloscope(compact=True)
        self.panel = QWidget()
        self.panel.setMinimumWidth(self.scope.minimumWidth())
        self.empty = QLabel("Open or render an electrical circuit to use the oscilloscope.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.scope, 1)
        layout.addWidget(self.empty, 1)
        self.addWidget(schematic)
        self.addWidget(self.panel)
        self.setChildrenCollapsible(False)
        self.setStretchFactor(0, 3)
        self.setStretchFactor(1, 1)
        self.setSizes([750, 330])
        self.panel.hide()

    def set_circuit(self, circuit, *, preserve_state=False):
        self.scope.set_circuit(circuit, preserve_state=preserve_state)
        self.empty.setVisible(self.scope.isHidden())

    def set_open(self, opened):
        self.panel.setVisible(opened)


class ScopeToggle(QToolButton):
    def __init__(self, views, sidebar):
        super().__init__(views)
        self.setObjectName("scopeToggle")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        action = QAction("Oscilloscope", self)
        action.setCheckable(True)
        action.setToolTip("Show or hide the oscilloscope beside the schematic.")
        self.setDefaultAction(action)

        def toggle(opened):
            if opened:
                views.setCurrentIndex(0)
            sidebar.set_open(opened)

        action.toggled.connect(toggle)
