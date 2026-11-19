"""Six live active-low DE10-Lite digits, drawn without images or web content."""
# Each physical segment keeps its board bit identity a through g, then the decimal point.
# The board convention is active-low, so a simulated zero lights a segment and an unknown
# or unconnected value must remain distinguishable from a valid lit/dark state.

import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QSizePolicy, QWidget


HEX_PIN = re.compile(r'HEX([0-5])([0-7])')
SEGMENTS = ('a (top)', 'b (upper right)', 'c (lower right)', 'd (bottom)',
            'e (lower left)', 'f (upper left)', 'g (middle)', 'decimal point')
LIT, UNLIT, UNCONNECTED = '#ff6357', '#4a302e', '#303030'


def segment_paths(area):
    """Return paths in board bit order a,b,c,d,e,f,g,DP, fitting a 56×80 digit."""
    def bar(x, y, width, height):
        bevel = min(width, height) / 2
        points = ((x + bevel, y), (x + width - bevel, y), (x + width, y + bevel),
                  (x + width, y + height - bevel), (x + width - bevel, y + height),
                  (x + bevel, y + height), (x, y + height - bevel), (x, y + bevel))
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(*point) for point in points]))
        path.closeSubpath()
        return path

    paths = [bar(8, 0, 28, 8), bar(36, 8, 8, 28), bar(36, 44, 8, 28),
             bar(8, 72, 28, 8), bar(0, 44, 8, 28), bar(0, 8, 8, 28), bar(8, 36, 28, 8)]
    dot = QPainterPath()
    dot.addEllipse(QRectF(48, 72, 8, 8))
    paths.append(dot)
    transform = QTransform().translate(area.x(), area.y()).scale(area.width() / 56, area.height() / 80)
    return tuple(transform.map(path) for path in paths)


def paint_digit(painter, area, values):
    """Paint eight values (0 lit, 1 dark, None unconnected/unknown)."""
    painter.setPen(Qt.PenStyle.NoPen)
    for path, value in zip(segment_paths(area), values):
        painter.fillPath(path, QColor(UNCONNECTED if value is None else LIT if value == 0 else UNLIT))


class SevenSegmentDisplay(QWidget):
    segment_probed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pins = {}
        self.values = {}
        self.press = None
        self.setMinimumWidth(300)
        self.setFixedHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName('DE10-Lite six-digit seven-segment display')
        self.hide()

    def configure(self, simulation):
        self.pins = {name: (int(match[1]), int(match[2])) for name in simulation.outputs
                     if (match := HEX_PIN.fullmatch(name)) and simulation.signals[name] == 1} if simulation else {}
        self.values = {}
        self.press = None
        self.setVisible(bool(self.pins))
        self.set_values(simulation.values if simulation else {})

    def set_values(self, values):
        values = {name: values[name] for name in self.pins if name in values}
        if values != self.values:
            self.values = values
            self.update()

    def digit_values(self, digit):
        return tuple(self.values.get(f'HEX{digit}{bit}') for bit in range(8))

    def lit_segments(self, digit):
        return {bit for bit, value in enumerate(self.digit_values(digit)) if value == 0}

    def digit_rect(self, digit):
        cell = min(76.0, (self.width() - 24) / 6)
        height = min(74.0, cell * 80 / 64)
        width = height * 56 / 80
        left = (self.width() - cell * 6) / 2 + (5 - digit) * cell + (cell - width) / 2
        return QRectF(left, 8, width, height)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#505050'), 1))
        painter.setBrush(QColor('#252525'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5, 5)
        for digit in range(5, -1, -1):
            area = self.digit_rect(digit)
            paint_digit(painter, area, self.digit_values(digit))
            connected = any(number == digit for number, _ in self.pins.values())
            painter.setPen(QColor('#dddddd' if connected else '#777777'))
            painter.drawText(QRectF(area.x() - 5, 84, area.width() + 10, 17),
                             Qt.AlignmentFlag.AlignCenter, f'HEX{digit}')

    def _pin_at(self, point):
        for digit in range(6):
            for bit, path in enumerate(segment_paths(self.digit_rect(digit))):
                pin = f'HEX{digit}{bit}'
                if pin in self.pins and path.contains(point):
                    return pin
        return None

    def mouseMoveEvent(self, event):
        pin = self._pin_at(event.position())
        if pin:
            value = self.values.get(pin)
            state = 'unknown' if value is None else 'lit (0)' if value == 0 else 'off (1)'
            self.setToolTip(f'{pin} · {SEGMENTS[self.pins[pin][1]]} · {state}\nClick to probe this active-low signal.')
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setToolTip('HEX5 through HEX0 · active-low segments: 0 = lit, 1 = off. Undeclared segments are blank.')
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press = event.position()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.press is not None:
            if (event.position() - self.press).manhattanLength() <= 8:
                if pin := self._pin_at(event.position()):
                    self.segment_probed.emit(pin)
            self.press = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)
