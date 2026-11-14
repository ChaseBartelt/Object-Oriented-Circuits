"""Draw standard gate symbols and package pinouts for editor hover."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .gates import gate_accepts


def gate_inputs(name, arguments):
    if "(" in arguments:
        return arguments.partition("(")[2].partition(")")[0].count(",") + 1
    count = arguments.count(",") + 1
    return count - 3 if gate_accepts(name, count - 3) else max(1, count - 2)


class DeviceReference(QWidget):
    def __init__(self):
        super().__init__()
        self.reference = None
        self.setMinimumSize(290, 160)

    def paintEvent(self, event):
        if not self.reference:
            return
        kind, name, pins = self.reference
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f5f8fc"))
        painter.setPen(QPen(QColor("#23364c"), 1.7))
        heading = QRectF(8, 8, self.width() - 16, 22)
        painter.drawText(heading, Qt.AlignmentFlag.AlignCenter, name)
        if kind == "builtin":
            self.gate(painter, name, pins)
        else:
            self.package(painter, pins)

    def gate(self, painter, name, count):
        name = name.casefold()
        left, right = 92, self.width() - 84
        y = (self.height() + 18) / 2
        h = min(44, max(19, count * 9))
        body = QPainterPath(QPointF(left, y - h))
        if name in {"not", "buf"}:
            body.lineTo(right, y)
            body.lineTo(left, y + h)
        elif name in {"and", "nand"}:
            body.lineTo((left + right) / 2, y - h)
            body.cubicTo(right + 20, y - h, right + 20, y + h, (left + right) / 2, y + h)
            body.lineTo(left, y + h)
        else:
            body.cubicTo(right - 25, y - h, right - 18, y - h / 2, right, y)
            body.cubicTo(right - 18, y + h / 2, right - 25, y + h, left, y + h)
            body.quadTo(left + 26, y, left, y - h)
            if name in {"xor", "xnor"}:
                curve = QPainterPath(QPointF(left - 9, y - h))
                curve.quadTo(left + 17, y, left - 9, y + h)
                painter.drawPath(curve)
        body.closeSubpath()
        inverted = name in {"not", "nand", "nor", "xnor"}
        drawn = min(count, 6)
        for i in range(drawn):
            pin_y = y + (i - (drawn - 1) / 2) * (min(16, 2 * h / drawn))
            painter.drawLine(QPointF(51, pin_y), QPointF(left + 18, pin_y))
            caption = "…" if i == 5 and count > drawn else chr(ord("A") + i)
            painter.drawText(QPointF(30, pin_y + 4), caption)
        painter.fillPath(body, QColor("#e4edf7"))
        painter.drawPath(body)
        if inverted:
            painter.drawEllipse(QPointF(right + 4, y), 4, 4)
        painter.drawLine(QPointF(right + (8 if inverted else 0), y), QPointF(right + 31, y))
        painter.drawText(QPointF(right + 36, y + 4), "Y")
        painter.drawText(QRectF(8, self.height() - 24, self.width() - 16, 20),
                         Qt.AlignmentFlag.AlignCenter, "Supply VCC   ·   Return GND")

    def package(self, painter, pins):
        left, right = 29, self.width() - 29
        top = 40
        rows = max(1, (len(pins) + 1) // 2)
        spacing = min(22, (self.height() - top - 17) / rows)
        bottom = top + rows * spacing
        body = QRectF(left, top, right - left, bottom - top)
        painter.fillRect(body, QColor("#e4edf7"))
        painter.drawRect(body)
        notch = QRectF(body.center().x() - 8, top - 7, 16, 14)
        painter.drawArc(notch, 180 * 16, 180 * 16)
        for i, pin in enumerate(pins):
            is_left = i < rows
            row = i if is_left else len(pins) - 1 - i
            y = top + (row + 0.5) * spacing
            x = left if is_left else right
            end = x - 13 if is_left else x + 13
            painter.drawLine(QPointF(x, y), QPointF(end, y))
            text = f"{i + 1}  {pin}" if is_left else f"{pin}  {i + 1}"
            flags = Qt.AlignmentFlag.AlignLeft if is_left else Qt.AlignmentFlag.AlignRight
            half = body.width() / 2 - 5
            area = QRectF(left + 4 if is_left else body.center().x(), y - 8, half, 16)
            painter.drawText(area, flags | Qt.AlignmentFlag.AlignVCenter, text)
