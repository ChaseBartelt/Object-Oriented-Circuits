"""Interactive ideal digital signals, cycle stepping and bounded waveforms."""

from collections import deque
from copy import deepcopy
from math import ceil
from time import perf_counter

from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtGui import QActionGroup, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.viewer.logic_scene import LogicSceneView
from circuitlab.viewer.seven_segment import SevenSegmentDisplay
from circuitlab.viewer.widgets import button


HIGH = "#55d991"
LOW = "#999999"
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
        # transition. During Run, fast-changing buses are sampled; Step always
        # records the complete settled state of the next cycle.
        if len(self.history) >= 2 and self.history[-1][1] == point[1] == self.history[-2][1]:
            self.history[-1] = point
        else:
            self.history.append(point)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#191919"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#f2f2f2"))
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
            painter.setPen(QColor("#f2f2f2"))
            painter.drawText(QRectF(6, top, left - 12, 26), Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(QColor("#393939"))
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
        painter.setPen(QColor("#bbbbbb"))
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
        self.layout_records = {}
        self._skip_layout_capture = False
        self.input_buttons, self.output_labels = {}, {}
        self.user_profile = False
        self.speed = 1.0
        self._last_tick = None
        self._target_cycle = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.advance)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.profile = QComboBox()
        self.profile.addItem("OOC logic", "ooc")
        self.profile.addItem("DE10-Lite", "de10-lite")
        self.profile.currentIndexChanged.connect(self._profile_changed)
        toolbar.addWidget(self.profile)
        self.run_button = button("Run", self.toggle, toolbar)
        self.step_button = button("Step", self.step, toolbar)
        self.reset_button = button("Reset", self.reset, toolbar)
        self.speed_button = QPushButton("Speed: 100%")
        speed_menu = QMenu(self.speed_button)
        self.speed_actions = QActionGroup(speed_menu)
        for percent in (25, 50, 75, 100):
            action = speed_menu.addAction(f"{percent}%")
            action.setCheckable(True)
            action.setData(percent)
            action.setChecked(percent == 100)
            self.speed_actions.addAction(action)
        self.speed_actions.triggered.connect(lambda action: self.set_speed(action.data()))
        self.speed_button.setMenu(speed_menu)
        toolbar.addWidget(self.speed_button)
        self.pace_label = QLabel()
        self.pace_label.hide()
        toolbar.addWidget(self.pace_label)
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
        self.hex_display = SevenSegmentDisplay()
        self.hex_display.segment_probed.connect(self.select_signal)
        layout.addWidget(self.hex_display)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.port_view = LogicSceneView()
        self.port_view.input_toggled.connect(self.toggle_input)
        self.port_view.signal_probed.connect(self.select_signal)
        self.port_view.setToolTip("Click inputs to toggle · click chip to open · drag blocks or wires to arrange · Ctrl+Z to undo · wheel to zoom")
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
        self.history_label.setToolTip(
            "Run keeps a bounded, sampled history. Fast buses may change between samples; "
            "use Step to inspect consecutive cycles. Simulation state is not decimated.")
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
            if self.simulation.signals[name] == 1:
                self.set_input(name, 1 - self.simulation.values[name])
            else:
                self.edit_input(name)

    def edit_input(self, name):
        sim = self.simulation
        if sim is None or name not in sim.inputs:
            return
        width = sim.signals[name]
        text, accepted = QInputDialog.getText(
            self, f"Input {name}", f"Unsigned {width}-bit value (decimal, 0x hex, or 0b binary):",
            text=str(sim.values[name]))
        if not accepted or self.simulation is not sim:
            return
        try:
            text = text.strip()
            if len(text) > 4098:
                raise ValueError
            base = 16 if text.lower().startswith("0x") else 2 if text.lower().startswith("0b") else 10
            value = int(text, base)
            if not 0 <= value < (1 << width):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Input value", f"Enter an unsigned value that fits in {width} bits.")
            return
        self.set_input(name, value)

    def select_signal(self, name):
        if self.probe.findText(name) >= 0:
            self.probe.setCurrentText(name)

    def _profile_changed(self, *_):
        self.user_profile = True
        self.set_source(self.source)

    def logic_placements(self):
        if self.simulation is not None and not self._skip_layout_capture:
            self.layout_records[self.simulation.profile] = self.port_view.placement()
        return deepcopy(self.layout_records)

    def load_logic_placements(self, records):
        """Install one file's saved layouts before compiling its new source."""
        self.layout_records = deepcopy(records) if isinstance(records, dict) else {}
        self._skip_layout_capture = True

    def _clear(self):
        if not self._skip_layout_capture:
            self.logic_placements()
        self._skip_layout_capture = False
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
        self.hex_display.configure(None)
        self.waveform.configure((), {})
        self.history_label.setText("512 points · plateaus compressed")
        for control in (self.run_button, self.step_button, self.reset_button, self.probe, self.all_ports, self.speed_button):
            control.setEnabled(False)
        self._target_cycle = 0.0
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
        self.hex_display.configure(sim)
        for index, name in enumerate(sim.inputs):
            control = QPushButton(name)
            scalar = sim.signals[name] == 1
            control.setCheckable(scalar)
            control.setToolTip(f"Toggle {name} between 0 and 1" if scalar else
                               f"Set {name}: unsigned {sim.signals[name]}-bit bus")
            control.setStyleSheet(f"QPushButton:checked {{ background: #235e43; color: {HIGH}; }}")
            control.clicked.connect(lambda checked=False, pin=name: self.toggle_input(pin))
            self.input_buttons[name] = control
            self.input_grid.addWidget(control, index // 5, index % 5)
        for index, name in enumerate(name for name in sim.outputs if name not in self.hex_display.pins):
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setTextFormat(Qt.TextFormat.PlainText)
            self.output_labels[name] = label
            self.output_grid.addWidget(label, index // 5, index % 5)
        self.output_toggle.setEnabled(bool(self.output_labels))
        self.output_group.setVisible(bool(self.output_labels) and self.output_toggle.isChecked())
        self.port_view.configure(sim)
        self.port_view.restore_placement(self.layout_records.get(sim.profile, {}))
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
        timed = clocked and sim.frequency is not None
        self.run_button.setEnabled(timed)
        self.run_button.setToolTip("Run at the selected fraction of the declared clock rate" if timed else
                                   "Clockless logic updates immediately when inputs change" if not clocked else
                                   "Declare a clock frequency, e.g. clock(clk, 50MHz), to run in real time")
        self.speed_button.setEnabled(timed)
        self._speed_tooltip()
        self.step_button.setEnabled(clocked)
        self.step_button.setToolTip("Advance one clock edge" if clocked else
                                    "Clockless logic has no clock edges to step")
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
            self.history_label.setText(f"{axis} {first:,}–{last:,} · sampled")
        for name, control in self.input_buttons.items():
            with QSignalBlocker(control):
                control.setChecked(bool(sim.values[name]))
            width = sim.signals[name]
            control.setText(f"{name}  {display_value(sim.values[name], width)}")
        for name, label in self.output_labels.items():
            value, width = sim.values[name], sim.signals[name]
            label.setText(f"● {name}  {value}" if width == 1 else f"{name}[{width}]  0x{value:X} ({value})")
            color = HIGH if value else LOW
            label.setStyleSheet(f"color: {color if width == 1 else '#cccccc'}; font-weight: 600; padding: 5px;")
        for row, (name, width) in enumerate(sim.signals.items()):
            self.table.item(row, 2).setText(display_value(sim.values[name], width))
        self.port_view.set_values(sim.values)
        self.hex_display.set_values(sim.values)
        self.values_changed.emit(dict(sim.values))

    def _failure(self, error):
        self._clear()
        self._show_error(error)

    def set_input(self, name, value):
        if self.simulation is None:
            return
        # Advance due old-input work within the UI budget. If the design is
        # behind, the click applies at the displayed simulation cycle.
        if self.timer.isActive():
            self.advance()
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
            self._target_cycle = self.simulation.cycles
            self._refresh(sample=True)
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def toggle(self):
        if self.timer.isActive():
            self.advance()
            self.shutdown()
        elif self.simulation is not None and self.simulation.clocks and self.simulation.frequency:
            self._last_tick = perf_counter()
            # Keep a partial clock period, but never charge paused wall time.
            self._target_cycle = self.simulation.cycles + self._target_cycle % 1
            self.timer.start()
            self.run_button.setText("Pause")
        self._refresh()

    def _accrue_time(self, now):
        if self._last_tick is not None:
            self._target_cycle += max(0.0, now - self._last_tick) * self.simulation.frequency * self.speed
        self._last_tick = now

    def _speed_tooltip(self):
        rate = self.simulation.frequency if self.simulation else None
        self.speed_button.setToolTip(
            f"Target: {rate * self.speed:,.0f} cycles/s ({self.speed:.0%} of {rate:,.0f} Hz). "
            "If this computer cannot keep up, Behind target appears; simulated cycles are never discarded."
            if rate else "Requires a declared clock frequency")

    def set_speed(self, percent):
        if percent not in (25, 50, 75, 100):
            raise ValueError("Speed must be 25, 50, 75 or 100 percent")
        if self.timer.isActive():
            self._accrue_time(perf_counter())
        self.speed = percent / 100
        self.speed_button.setText(f"Speed: {percent}%")
        for action in self.speed_actions.actions():
            action.setChecked(action.data() == percent)
        self._speed_tooltip()

    def advance(self):
        if self.simulation is None or not self.timer.isActive():
            return
        now = perf_counter()
        self._accrue_time(now)
        sim = self.simulation
        due = max(0, int(self._target_cycle + 1e-7) - sim.cycles)
        try:
            if due:
                sim.advance(due, traces=self.waveform.traces, sample=self.waveform.sample,
                            sample_every=max(1, ceil(due / 128)), deadline=now + .008)
            overdue = max(0, int(self._target_cycle + 1e-7) - sim.cycles)
            lag = overdue / (sim.frequency * self.speed)
            if lag > .1:
                self.pace_label.setText("Behind target")
                self.pace_label.setToolTip(
                    f"Simulation is {lag:.2f} s behind the selected pace. "
                    "This circuit exceeds the current software throughput. Input clicks apply at the displayed "
                    "simulation cycle; use Pause and Step for precise input timing.")
                self.pace_label.show()
            elif lag < .02:
                self.pace_label.hide()
            self._refresh()
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def reset(self):
        self.shutdown()
        if self.simulation is None:
            return
        try:
            self.simulation.reset()
            self._target_cycle = 0.0
            self._configure_traces()
        except (CircuitError, ValueError) as error:
            self._failure(error)

    def shutdown(self):
        self.timer.stop()
        self._last_tick = None
        self.pace_label.hide()
        self.run_button.setText("Run")

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
