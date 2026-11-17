"""Interactive ideal digital signals, cycle stepping and bounded waveforms."""

from collections import deque
from time import perf_counter

from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.viewer.logic_scene import LogicSceneView


HIGH = "#55d991"
LOW = "#6c819b"
TRACE_COLORS = ("#55d991", "#6fc5ff", "#ffcf70", "#d7a2ff")


def display_value(value, width):
    return str(value) if width == 1 else f"0x{value:X}"


class DigitalWaveform(QWidget):
    """One step trace per port/probe, using actual simulated cycle positions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = deque(maxlen=512)
        self.traces = ()
        self.widths = {}
        self.clocked = True
        self.event_index = -1
        self.setMinimumHeight(150)

    def configure(self, traces, widths, *, clocked=True):
        self.traces, self.widths = tuple(traces), dict(widths)
        self.clocked, self.event_index = clocked, -1
        self.history.clear()
        self.setMinimumHeight(max(150, 38 * len(self.traces) + 45))
        self.update()

    def sample(self, cycle, values):
        self.event_index += 1
        point = (cycle if self.clocked else self.event_index,
                 {name: values[name] for name in self.traces})
        # Preserve the first and latest point of a plateau, plus every observed
        # transition. We still evaluate every clock cycle; only storage shrinks.
        if len(self.history) >= 2 and self.history[-1][1] == point[1] == self.history[-2][1]:
            self.history[-1] = point
        else:
            self.history.append(point)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111923"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#cbd5e1"))
        if not self.history:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No digital signal data")
            return
        left, right = min(145, self.width() // 3), self.width() - 16
        first, last = self.history[0][0], self.history[-1][0]
        span = max(1, last - first)
        for row, name in enumerate(self.traces):
            top = 16 + row * 38
            value = self.history[-1][1][name]
            label = f"{name}  {display_value(value, self.widths[name])}"
            label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, left - 12)
            painter.setPen(QColor("#cbd5e1"))
            painter.drawText(QRectF(6, top, left - 12, 26), Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(QColor("#29384a"))
            painter.drawLine(QPointF(left, top + 26), QPointF(right, top + 26))
            path = QPainterPath()
            previous_y = None
            maximum = max(1, (1 << self.widths[name]) - 1)
            for cycle, values in self.history:
                x = left + (cycle - first) / span * max(1, right - left)
                y = top + 26 - values[name] / maximum * 22
                if previous_y is None:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, previous_y)
                    path.lineTo(x, y)
                previous_y = y
            path.lineTo(right, previous_y)
            painter.setPen(QPen(QColor(TRACE_COLORS[row % len(TRACE_COLORS)]), 1.8))
            painter.drawPath(path)
        painter.setPen(QColor("#9daec1"))
        bottom = 38 * len(self.traces) + 36
        axis = "Cycle" if self.clocked else "Event"
        painter.drawText(QPointF(left, bottom), f"{axis} {first:,}")
        painter.drawText(QRectF(left, bottom - 16, right - left, 20),
                         Qt.AlignmentFlag.AlignRight, f"{axis} {last:,}")


class DigitalSimulator(QWidget):
    """Software simulation only; never opens a programmer or reads physical pins."""

    values_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = ""
        self.simulation = None
        self.input_buttons, self.output_labels = {}, {}
        self.user_profile = False
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.advance)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.profile = QComboBox()
        self.profile.addItem("OOC logic", "ooc")
        self.profile.addItem("DE10-Lite switches", "de10-lite")
        self.profile.currentIndexChanged.connect(self._profile_changed)
        toolbar.addWidget(self.profile)
        self.run_button = QPushButton("Run")
        self.step_button = QPushButton("Step")
        self.reset_button = QPushButton("Reset")
        self.run_button.clicked.connect(self.toggle)
        self.step_button.clicked.connect(self.step)
        self.reset_button.clicked.connect(self.reset)
        for control in (self.run_button, self.step_button, self.reset_button):
            toolbar.addWidget(control)
        layout.addLayout(toolbar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.hide()
        layout.addWidget(self.status)
        ports = QHBoxLayout()
        self.input_group = QGroupBox()
        self.output_group = QGroupBox()
        self.input_grid = QGridLayout(self.input_group)
        self.output_grid = QGridLayout(self.output_group)
        self.input_toggle = self._port_toggle("Inputs", self.input_group, toolbar)
        self.output_toggle = self._port_toggle("Outputs", self.output_group, toolbar)
        toolbar.addStretch()
        ports.addWidget(self.input_group, 1)
        ports.addWidget(self.output_group, 1)
        layout.addLayout(ports)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.port_view = LogicSceneView()
        self.port_view.input_toggled.connect(self.toggle_input)
        self.port_view.signal_probed.connect(self.select_signal)
        self.port_view.setToolTip("Click inputs to toggle · click chip to open · wheel to zoom · drag blank space to pan")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Signal", "Bits", "Value"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        tabs = QTabWidget()
        tabs.addTab(self.port_view, "Schematic")
        tabs.addTab(self.table, "Signals")
        splitter.addWidget(tabs)
        wave_panel = QWidget()
        wave_layout = QVBoxLayout(wave_panel)
        wave_layout.setContentsMargins(0, 0, 0, 0)
        probes = QHBoxLayout()
        probes.addWidget(QLabel("Probe"))
        self.probe = QComboBox()
        self.probe.setMinimumContentsLength(12)
        self.probe.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.probe.currentIndexChanged.connect(self._configure_traces)
        probes.addWidget(self.probe, 1)
        self.all_ports = QCheckBox("All ports")
        self.all_ports.setToolTip("Add all input/output ports to the selected signal and its dependencies")
        self.all_ports.toggled.connect(self._configure_traces)
        probes.addWidget(self.all_ports)
        self.history_label = QLabel("512 points · plateaus compressed")
        probes.addWidget(self.history_label)
        wave_layout.addLayout(probes)
        self.waveform = DigitalWaveform()
        self.wave_scroll = QScrollArea()
        self.wave_scroll.setWidgetResizable(True)
        self.wave_scroll.setWidget(self.waveform)
        wave_layout.addWidget(self.wave_scroll)
        splitter.addWidget(wave_panel)
        splitter.setSizes([500, 220])
        layout.addWidget(splitter, 1)
        self._clear()

    @staticmethod
    def _port_toggle(title, group, toolbar):
        toggle = QToolButton()
        toggle.setText(title)
        toggle.setCheckable(True)
        toggle.setChecked(True)
        toggle.setArrowType(Qt.ArrowType.DownArrow)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        def expand(shown):
            group.setVisible(shown)
            toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        toggle.toggled.connect(expand)
        toolbar.addWidget(toggle)
        return toggle

    def toggle_input(self, name):
        if self.simulation is not None and name in self.simulation.inputs:
            self.set_input(name, 1 - self.simulation.values[name])

    def select_signal(self, name):
        if self.probe.findText(name) >= 0:
            self.probe.setCurrentText(name)

    def _profile_changed(self, *_):
        self.user_profile = True
        self.set_source(self.source)

    def _clear(self):
        self.shutdown()
        self.simulation = None
        for grid in (self.input_grid, self.output_grid):
            while grid.count():
                item = grid.takeAt(0)
                item.widget().hide()
                item.widget().deleteLater()
        self.input_buttons.clear()
        self.output_labels.clear()
        with QSignalBlocker(self.probe):
            self.probe.clear()
        self.table.setRowCount(0)
        self.port_view.configure(None)
        self.waveform.configure((), {})
        self.history_label.setText("512 points · plateaus compressed")
        for control in (self.run_button, self.step_button, self.reset_button, self.probe, self.all_ports):
            control.setEnabled(False)
        self.status.clear()
        self.status.hide()
        self.values_changed.emit({})

    def _show_error(self, error):
        self.status.setText(str(error))
        self.status.show()

    def set_source(self, source):
        self.source = source
        self._clear()
        if not source.strip():
            return
        if not self.user_profile:
            from circuitlab.tooling.fpga import compile_de10_lite
            try:
                compile_de10_lite(source)
                chosen = 1
            except (CircuitError, ValueError):
                chosen = 0
            with QSignalBlocker(self.profile):
                self.profile.setCurrentIndex(chosen)
        try:
            self.simulation = DigitalSimulation(source, profile=self.profile.currentData())
        except (CircuitError, ValueError) as error:
            self._show_error(error)
            return
        sim = self.simulation
        for index, name in enumerate(sim.inputs):
            control = QPushButton(name)
            control.setCheckable(True)
            control.setToolTip(f"Toggle {name} between 0 and 1")
            control.setStyleSheet(f"QPushButton:checked {{ background: #235e43; color: {HIGH}; }}")
            control.clicked.connect(lambda checked, pin=name: self.set_input(pin, int(checked)))
            self.input_buttons[name] = control
            self.input_grid.addWidget(control, index // 5, index % 5)
        for index, name in enumerate(sim.outputs):
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setTextFormat(Qt.TextFormat.PlainText)
            self.output_labels[name] = label
            self.output_grid.addWidget(label, index // 5, index % 5)
        self.port_view.configure(sim)
        self.table.setRowCount(len(sim.signals))
        with QSignalBlocker(self.probe):
            for row, (name, width) in enumerate(sim.signals.items()):
                self.table.setItem(row, 0, QTableWidgetItem(name))
                self.table.setItem(row, 1, QTableWidgetItem(str(width)))
                self.table.setItem(row, 2, QTableWidgetItem())
                if name not in sim.clocks:
                    self.probe.addItem(name)
            if sim.outputs:
                self.probe.setCurrentText(sim.outputs[0])
        clocked = bool(sim.clocks)
        self.run_button.setEnabled(clocked)
        self.step_button.setEnabled(clocked)
        self.reset_button.setEnabled(True)
        self.probe.setEnabled(True)
        self.all_ports.setEnabled(True)
        self._configure_traces()

    def _configure_traces(self, *_):
        if self.simulation is None:
            return
        sim = self.simulation
        pending = deque((self.probe.currentText(),))
        traces, visited = [], set(sim.clocks)
        while pending:
            name = pending.popleft()
            if name in visited or name not in sim.signals:
                continue
            visited.add(name)
            traces.append(name)
            pending.extend(sim.dependencies.get(name, ()))
        if self.all_ports.isChecked():
            traces.extend(name for name in (*sim.inputs, *sim.outputs) if name not in visited)
        self.waveform.configure(traces, sim.signals, clocked=bool(sim.clocks))
        self.wave_scroll.verticalScrollBar().setValue(0)
        self._refresh(sample=True)

    def _refresh(self, *, sample=False):
        sim = self.simulation
        if sim is None:
            return
        if sample:
            self.waveform.sample(sim.cycles, sim.values)
        if self.waveform.history:
            axis = "Cycles" if sim.clocks else "Events"
            first, last = self.waveform.history[0][0], self.waveform.history[-1][0]
            self.history_label.setText(f"{axis} {first:,}–{last:,} · dependencies included")
        for name, control in self.input_buttons.items():
            with QSignalBlocker(control):
                control.setChecked(bool(sim.values[name]))
            control.setText(f"{name}  {sim.values[name]}")
        for name, label in self.output_labels.items():
            value = sim.values[name]
            label.setText(f"● {name}  {value}")
            label.setStyleSheet(f"color: {HIGH if value else LOW}; font-weight: 600; padding: 5px;")
        for row, (name, width) in enumerate(sim.signals.items()):
            self.table.item(row, 2).setText(display_value(sim.values[name], width))
        self.port_view.set_values(sim.values)
        self.values_changed.emit(dict(sim.values))

    def _failure(self, error):
        self._clear()
        self._show_error(error)

    def set_input(self, name, value):
        if self.simulation is None:
            return
        try:
            self.simulation.set_input(name, value)
            self._refresh(sample=True)
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def step(self):
        self.shutdown()
        if self.simulation is None:
            return
        try:
            self.simulation.step()
            self._refresh(sample=True)
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def toggle(self):
        if self.timer.isActive():
            self.shutdown()
        elif self.simulation is not None and self.simulation.clocks:
            self.timer.start()
            self.run_button.setText("Pause")
        self._refresh()

    def advance(self):
        if self.simulation is None or not self.timer.isActive():
            return
        deadline = perf_counter() + .008
        try:
            for _ in range(128):
                self.simulation.step()
                self.waveform.sample(self.simulation.cycles, self.simulation.values)
                if perf_counter() >= deadline:
                    break
            self._refresh()
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def reset(self):
        self.shutdown()
        if self.simulation is None:
            return
        try:
            self.simulation.reset()
            self._configure_traces()
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def shutdown(self):
        self.timer.stop()
        self.run_button.setText("Run")

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
