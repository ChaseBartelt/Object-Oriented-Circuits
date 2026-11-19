"""Run or pause a built-in time simulation and plot a circuit node."""
# The scope owns bounded sampling work and a selected-node view of stored simulation history.
# Pause/resume preserves physical state; value-only updates can restamp a compatible circuit
# without treating a slider gesture or a new monitored node as a fresh simulation.

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
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
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(145)
        self.samples = deque(maxlen=600)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1b2028"))
        area = QRectF(self.rect()).adjusted(53, 17, -15, -24)
        painter.setPen(QColor("#697587"))
        painter.drawRect(area)
        if len(self.samples) < 2:
            return
        first, last = self.samples[0][0], self.samples[-1][0]
        low = min(v for _, v in self.samples)
        high = max(v for _, v in self.samples)
        if high - low < 1e-6:
            low, high = low - 0.5, high + 0.5
        painter.setPen(QColor("#cbd5e1"))
        painter.drawText(QPointF(4, area.top() + 5), f"{high:.3g} V")
        painter.drawText(QPointF(4, area.bottom()), f"{low:.3g} V")
        painter.drawText(QPointF(area.left(), area.bottom() + 17), f"{first * 1000:.3g} ms")
        painter.drawText(QPointF(max(area.left(), area.right() - 66), area.bottom() + 17),
                         f"{last * 1000:.3g} ms")
        path = QPainterPath()
        for i, (t, v) in enumerate(self.samples):
            x = area.left() + (t - first) / (last - first) * area.width()
            y = area.bottom() - (v - low) / (high - low) * area.height()
            (path.lineTo if i else path.moveTo)(x, y)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#69bcf6"))
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
        self.step.setRange(0.000001, 100000)
        self.step.setValue(10)
        self.step.setSuffix(" µs")
        self.run = QPushButton("Run")
        self.run.clicked.connect(self.toggle)
        self.reset = QPushButton("Reset")
        self.reset.clicked.connect(self.restart)
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
            row.addWidget(self.run)
            row.addWidget(self.reset)
            layout.addLayout(row)
            fields = QFormLayout()
            fields.addRow("Node", self.node)
            fields.addRow("Time step", self.step)
            fields.addRow("Initial capacitor", self.initial)
            fields.addRow("Initial voltage", self.voltage)
            layout.addLayout(fields)
        else:
            for controls in (
                (self.run, self.reset, QLabel("Node"), self.node, QLabel("Time step"), self.step),
                (QLabel("Initial capacitor"), self.initial, QLabel("Initial voltage"), self.voltage),
            ):
                row = QHBoxLayout()
                for control in controls:
                    row.addWidget(control)
                row.addStretch()
                layout.addLayout(row)
        layout.addWidget(self.waveform, 1)
        layout.addWidget(self.message)
        self.node.currentIndexChanged.connect(self.show_node)
        self.step.valueChanged.connect(self.change_step)
        for control in (self.initial, self.voltage):
            control.setToolTip("Initial conditions apply after Reset; changing them does not interrupt a running circuit.")
        self.hide()

    def set_panel_visible(self, visible):
        self.panel_visible = visible
        self.update_visibility()

    def update_visibility(self):
        circuit = self.circuit
        supported = circuit is not None and circuit.mode in {"dc", "ac", "mixed"} and bool(circuit.components)
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
                self.node.addItems(net for net in circuit.nodes if net not in circuit.grounds)
                index = self.node.findText(previous[0])
                if index < 0:
                    index = self.node.findText("out")
                self.node.setCurrentIndex(max(0, index))
                for part in circuit.components:
                    if part.kind == "C":
                        self.initial.addItem(part.label, part.name)
                self.initial.setCurrentIndex(max(0, self.initial.findData(previous[1])))
                try:
                    frequency = source_frequency(circuit)
                    if not preserve_state and frequency is not None and frequency != self.frequency:
                        self.step.setValue(1e6 / (100 * frequency))
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
        step = self.step.value() * 1e-6
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
                simulation = TimeDomain(self.circuit, self.step.value() * 1e-6)
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
