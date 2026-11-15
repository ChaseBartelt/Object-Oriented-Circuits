"""Run or pause a built-in time simulation and plot a circuit node."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from circuitlab.model.graph import CircuitError
from circuitlab.math.nonlinear import TimeDomain


def sample_batch(simulation, node, stop):
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
        samples.append((simulation.time, voltage[node]))
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
    def __init__(self):
        super().__init__()
        self.circuit = None
        self.simulation = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="waveform")
        self.pending = None
        self.stop_batch = Event()
        self.timer = QTimer(self)
        self.timer.setInterval(35)
        self.timer.timeout.connect(self.advance)
        self.waveform = Waveform()
        self.node = QComboBox()
        self.initial = QComboBox()
        self.voltage = QDoubleSpinBox()
        self.voltage.setRange(-100, 100)
        self.voltage.setSuffix(" V")
        self.step = QDoubleSpinBox()
        self.step.setDecimals(2)
        self.step.setRange(0.01, 100000)
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
        for controls in (
            (self.run, self.reset, QLabel("Node"), self.node, QLabel("Time step"), self.step),
            (QLabel("Initial capacitor"), self.initial, QLabel("Initial voltage"), self.voltage),
        ):
            row = QHBoxLayout()
            for control in controls:
                row.addWidget(control)
            row.addStretch()
            layout.addLayout(row)
        layout.addWidget(self.waveform)
        layout.addWidget(self.message)
        for signal in (self.node.currentIndexChanged, self.initial.currentIndexChanged,
                       self.voltage.valueChanged, self.step.valueChanged):
            signal.connect(self.restart)
        self.hide()

    def set_circuit(self, circuit):
        if circuit is self.circuit:
            return
        previous = self.node.currentText()
        self.circuit = circuit
        self.restart()
        self.node.clear()
        self.initial.clear()
        self.initial.addItem("None", None)
        supported = circuit is not None and circuit.mode == "dc" and bool(circuit.components)
        self.setVisible(supported)
        if supported:
            self.node.addItems(net for net in circuit.nodes if net not in circuit.grounds)
            index = self.node.findText(previous)
            if index < 0:
                index = self.node.findText("out")
            self.node.setCurrentIndex(max(0, index))
            for part in circuit.components:
                if part.kind == "C":
                    self.initial.addItem(part.label, part.name)

    def pause(self):
        self.timer.stop()
        self.stop_batch.set()
        self.run.setText("Resume" if self.simulation else "Run")

    def shutdown(self):
        self.pause()
        self.worker.shutdown(wait=False, cancel_futures=True)

    def restart(self):
        self.simulation = None
        self.pause()
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
        simulation = self.simulation
        if simulation is None:
            return
        if self.pending is not None:
            owner, future = self.pending
            if not future.done():
                return
            self.pending = None
            samples, error = future.result()
            if owner is simulation:
                self.waveform.samples.extend(samples)
                self.waveform.update()
                if error is not None:
                    self.pause()
                    self.message.setText(
                        f"{error} Try a smaller time step or set an initial capacitor voltage."
                    )
                    return
        node = simulation.nodes.index(self.node.currentText())
        self.pending = simulation, self.worker.submit(sample_batch, simulation, node, self.stop_batch)
