"""Paint a physical hole grid beneath the electrical schematic."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen


def underside_offset(board):
    """Place the back beside the component side, with room for their labels."""
    return board.column_count + 5, 0


def board_point(board, at, *, underside=False, offset=(0, 0)):
    """Map a physical hole coordinate into either displayed board face."""
    x, y = at
    if underside:
        x = board.column_count - 1 - x
    return x + offset[0], y + offset[1]


def inverse_board_point(board, at, *, underside=False, offset=(0, 0)):
    """Recover the same physical hole from its displayed face coordinate."""
    x, y = at[0] - offset[0], at[1] - offset[1]
    return (board.column_count - 1 - x if underside else x), y


def paint_perfboard(scene, board, *, scale, mirror=False, offset=(0, 0)):
    """Paint a readable component or mirrored solder side at a display offset."""
    side = "underside" if mirror else "top"

    def position(x, y):
        x, y = board_point(board, (x, y), underside=mirror, offset=offset)
        return QPointF(x * scale, -y * scale)

    def mark(item, kind):
        item.setData(0, kind)
        item.setData(2, side)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        return item

    bounds = QRectF(position(-.5, .5), position(board.column_count - .5, .5 - board.row_count))
    fill = "#b5d5aa" if mirror else "#c5dfbb"
    frame = mark(scene.addRect(bounds.normalized(), QPen(QColor("#5d8052")), QBrush(QColor(fill))), "perfboard")
    frame.setZValue(-100)
    frame.setData(3, offset)
    frame.setToolTip(f"Perfboard: {board.column_count} columns × {board.row_count} rows; {board.pitch_mm:g} mm pitch\n"
                     + ("Underside viewed from below; hole labels match the component side." if mirror else
                        "Component side. Double-click the board to show or hide its underside."))

    # One path keeps large hole grids inexpensive to repaint during a drag.
    path = QPainterPath()
    pads = QPainterPath()
    radius = .085 * scale
    for row in range(board.row_count):
        for column in range(board.column_count):
            path.addEllipse(position(column, -row), radius, radius)
            if mirror:
                pads.addEllipse(position(column, -row), .18 * scale, .18 * scale)
    if mirror:
        copper = mark(scene.addPath(pads, QPen(QColor("#8b6138")), QBrush(QColor("#c99c5c"))), "perfboard-pad")
        copper.setData(1, board.column_count * board.row_count)
        copper.setZValue(-99.5)
    holes = mark(scene.addPath(path, QPen(QColor("#618451")), QBrush(QColor("#f7fbf4"))), "perfboard-hole")
    holes.setData(1, board.column_count * board.row_count)
    holes.setZValue(-99)

    font = QFont("Inter")
    font.setPixelSize(11)

    def label(text, x, y):
        item = scene.addSimpleText(str(text), font)
        item.setBrush(QColor("#34522c"))
        mark(item, "perfboard-label")
        item.setData(1, str(text))
        size = item.boundingRect()
        item.setPos(position(x, y) - QPointF(size.width() / 2, size.height() / 2))
        item.setZValue(-98)
        return item

    for column, name in enumerate(board.columns):
        label(name, column, 1)
    for row, name in enumerate(board.rows):
        label(name, -1, -row)
    title = label(f"{board.pitch_mm:g} mm hole spacing", (board.column_count - 1) / 2, 1.75)
    title.setData(0, "perfboard-scale")
    face = label("Underside · viewed from below" if mirror else "Component side · double-click to show underside",
                 (board.column_count - 1) / 2, 2.5)
    face.setData(0, "perfboard-side")
    return frame
