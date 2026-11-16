"""The native circuit source editor and its compiler annotations."""

import re

from PySide6.QtCore import QEvent, QRect, QSize, QStringListModel, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QToolTip, QWidget

from circuitlab.language.gates import GATE_HELP
from circuitlab.math.numbers import NUMBER
from circuitlab.language.syntax import OP, TRANS_CALL
from circuitlab.tooling.source_info import (
    IDENTIFIER, SourceInfo, accepts_calls as _accepts_calls,
)

INDENT = " " * 4
BUILTIN_CALL = re.compile(
    rf"\b(?:{OP.pattern}|{TRANS_CALL.pattern}|{'|'.join(GATE_HELP)})\b(?=\s*\()",
    re.I,
)


class CircuitHighlighter(QSyntaxHighlighter):
    """Color source tokens without requiring the circuit to compile."""

    rules = (
        (re.compile(r"^\s*(@override)\b", re.I), "#b5d49d"),
        (
            re.compile(
                r"\b(?:series|parallel|circuit|chip|gate|between|battery|opamp|transistor|"
                r"end|main|return|measure|wire|ground|model|port|input|output|connector|include|"
                r"for|while|do|if|else|let|int|bool|true|false|break|continue)\b",
                re.I,
            ),
            "#92bfff",
        ),
        (re.compile(r"\b(?:[RCLXVIQ]\d[\w]*|op\d+)\b"), "#e3b5f5"),
        (re.compile(rf"(?<![\w.])[+-]?{NUMBER}(?:[A-Za-zµμΩ°]+)?"), "#ffd18b"),
        (BUILTIN_CALL, "#e3b5f5"),
    )

    def highlightBlock(self, text):
        code, comment, _ = text.partition("#")
        for pattern, color in self.rules:
            for match in pattern.finditer(code):
                self.setFormat(match.start(), len(match[0]), QColor(color))
        if comment:
            start = len(code)
            self.setFormat(start, len(text) - start, QColor("#9bc68b"))
            for match in re.finditer(r"@[A-Za-z]+\b", text[start:]):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor("#b5d49d"))
                fmt.setFontWeight(QFont.Weight.Bold)
                self.setFormat(start + match.start(), len(match[0]), fmt)


class Gutter(QWidget):
    """Paint line numbers alongside the visible source rows."""

    def paintEvent(self, event):
        editor = self.parent()
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#292929"))
        painter.setPen(QColor("#aaaaaa"))
        block = editor.firstVisibleBlock()
        offset = editor.contentOffset()
        while block.isValid():
            bounds = editor.blockBoundingGeometry(block).translated(offset)
            y = round(bounds.top())
            if y > event.rect().bottom():
                break
            if block.isVisible() and bounds.bottom() >= event.rect().top():
                painter.drawText(
                    0,
                    y,
                    self.width() - 8,
                    editor.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(block.blockNumber() + 1),
                )
            block = block.next()


class CircuitEditor(QPlainTextEdit):
    """Edit circuit source with completion, indentation, and diagnostic marks."""

    wire_hovered = Signal(object, object)
    device_hovered = Signal(object, object, str)

    def __init__(self):
        super().__init__()
        self.setFont(QFont("Consolas", 11))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(INDENT))
        self.setAccessibleName("Circuit source code")
        self.viewport().setMouseTracking(True)
        self.hovered_wire = None
        self.hovered_device = None
        self.read_imports = None
        self.gutter = Gutter(self)
        self.highlighter = CircuitHighlighter(self.document())
        self.completions = QStringListModel(self)
        self.completer = QCompleter(self.completions, self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.activated[str].connect(self.insert_completion)
        self.completer.popup().installEventFilter(self)
        self.completer.popup().setStyleSheet(
            "QListView { background: #282828; color: #f2f2f2; "
            "border: 1px solid #666; selection-background-color: #365777; }"
        )
        self.cursorPositionChanged.connect(self.completer.popup().hide)
        self.textChanged.connect(self.completer.popup().hide)
        self.textChanged.connect(self.clear_hover)
        self.blockCountChanged.connect(self.resize_gutter)
        self.updateRequest.connect(self.update_gutter)
        self.resize_gutter()

    def _before_cursor(self):
        cursor = self.textCursor()
        cursor.setPosition(0, QTextCursor.MoveMode.KeepAnchor)
        return cursor.selectedText().replace("\u2029", "\n")

    def method_signatures(self):
        return SourceInfo(self.toPlainText(), self.read_imports).method_signatures()

    def _completion_prefix(self):
        if self.textCursor().hasSelection():
            return None
        before = self._before_cursor()
        line = before.rsplit("\n", 1)[-1]
        if "#" in line:
            return None
        statement = re.split(r"[;{}]", line)[-1].lstrip()
        if statement and not IDENTIFIER.fullmatch(statement):
            return None
        context = before.removesuffix(statement)
        return statement if _accepts_calls(context) else None

    def suggest(self, *, explicit=False):
        prefix = self._completion_prefix()
        popup = self.completer.popup()
        if prefix is None or (not prefix and not explicit):
            popup.hide()
            return
        methods = self.method_signatures()
        builtin = prefix if OP.fullmatch(prefix) else "op1"
        names = {key: method[1] for key, method in methods.items()}
        names[builtin.casefold()] = builtin
        for gate in GATE_HELP:
            names.setdefault(gate, gate)
        transistor = prefix if TRANS_CALL.fullmatch(prefix) else "transQ1"
        names.setdefault(transistor.casefold(), transistor)
        self.completions.setStringList([names[key] for key in sorted(names)])
        self.completer.setCompletionPrefix(prefix)
        if not self.completer.setCurrentRow(0):
            popup.hide()
            return
        rect = self.cursorRect()
        rect.setWidth(max(130, popup.sizeHintForColumn(0) + 24))
        self.completer.complete(rect)
        popup.setCurrentIndex(self.completer.currentIndex())

    def insert_completion(self, name):
        prefix = self._completion_prefix()
        if prefix is None or not name.casefold().startswith(prefix.casefold()):
            return
        cursor = self.textCursor()
        cursor.setPosition(cursor.position() - len(prefix))
        end = self.textCursor()
        while re.fullmatch(
            r"[A-Za-z0-9_]", self.document().characterAt(end.position())
        ):
            end.movePosition(QTextCursor.MoveOperation.Right)
        cursor.setPosition(end.position(), QTextCursor.MoveMode.KeepAnchor)
        end.movePosition(
            QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        suffix = end.selectedText().lstrip()
        method = self.method_signatures().get(name.casefold())
        if suffix.startswith("("):
            cursor.insertText(name)
        else:
            cursor.insertText(name + "()" + ("" if suffix.startswith(";") else ";"))
            if method is None or method[2]:
                cursor.movePosition(
                    QTextCursor.MoveOperation.Left,
                    QTextCursor.MoveMode.MoveAnchor,
                    1 if suffix.startswith(";") else 2,
                )
        self.setTextCursor(cursor)
        self.completer.popup().hide()

    def _completion_key(self, event):
        popup = self.completer.popup()
        if not popup.isVisible():
            return False
        if (
            event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers()
        ):
            index = popup.currentIndex()
            if index.isValid():
                self.insert_completion(index.data())
            return True
        if event.key() == Qt.Key.Key_Escape:
            popup.hide()
            return True
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            index = popup.currentIndex()
            row = index.row() + (-1 if event.key() == Qt.Key.Key_Up else 1)
            row = max(0, min(row, popup.model().rowCount() - 1))
            popup.setCurrentIndex(popup.model().index(row, 0))
            return True
        return False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and watched is self.completer.popup():
            if self._completion_key(event):
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if self._completion_key(event):
            return
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key.Key_Space and modifiers == Qt.KeyboardModifier.ControlModifier:
            self.suggest(explicit=True)
            return
        cursor = self.textCursor()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not modifiers:
            self.insert_newline(cursor)
            return
        if key == Qt.Key.Key_Tab and not modifiers and not cursor.hasSelection():
            cursor.insertText(INDENT)
            return
        if key == Qt.Key.Key_Backtab or (
            key == Qt.Key.Key_Tab and not modifiers and cursor.hasSelection()
        ):
            self.indent_lines(cursor, dedent=key == Qt.Key.Key_Backtab)
            return
        if (
            event.text() == "}"
            and not cursor.hasSelection()
            and self.dedent_brace(cursor)
        ):
            return
        super().keyPressEvent(event)
        if (
            event.text().isalnum()
            or event.text() == "_"
            or key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
        ):
            self.suggest()

    def indent_lines(self, cursor, *, dedent):
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        block = self.document().findBlock(start)
        cursor.beginEditBlock()
        while block.isValid() and (block.position() < end or block.position() <= start):
            line = QTextCursor(block)
            if dedent:
                indent = re.match(r"\t| {1,4}", block.text())
                if indent:
                    count = len(indent[0])
                    line.movePosition(
                        QTextCursor.MoveOperation.Right,
                        QTextCursor.MoveMode.KeepAnchor,
                        count,
                    )
                    line.removeSelectedText()
                    end -= count
            else:
                line.insertText(INDENT)
                end += len(INDENT)
            block = block.next()
        cursor.endEditBlock()

    def insert_newline(self, cursor):
        cursor.setPosition(cursor.selectionStart())
        before = cursor.block().text().partition("#")[0][: cursor.positionInBlock()]
        indent = re.match(r"[ \t]*", before)[0]
        if before.rstrip().endswith("{"):
            indent += "\t" if indent.startswith("\t") else INDENT
        self.textCursor().insertText("\n" + indent)

    def dedent_brace(self, cursor):
        before = self._before_cursor()
        line = before.rsplit("\n", 1)[-1]
        if line.strip():
            return False
        openings = []
        for text in before.splitlines():
            code = text.partition("#")[0]
            for char in code:
                if char == "{":
                    openings.append(re.match(r"[ \t]*", code)[0])
                elif char == "}" and openings:
                    openings.pop()
        if not openings:
            return False
        cursor.movePosition(
            QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        cursor.insertText(openings[-1] + "}")
        return True

    def hover_help(self, cursor):
        source = SourceInfo(self.toPlainText(), self.read_imports)
        return source.hover_help(cursor.blockNumber(), cursor.positionInBlock())

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.MouseMove:
            cursor = self.cursorForPosition(event.position().toPoint())
            if self.hovered_wire and self.wire_at(cursor) != self.hovered_wire:
                self.set_hovered_wire(None)
            if self.hovered_device and self.device_at(cursor) != self.hovered_device:
                self.set_hovered_device(None)
        elif event.type() == QEvent.Type.Leave:
            self.clear_hover()
        if event.type() == QEvent.Type.ToolTip:
            cursor = self.cursorForPosition(event.pos())
            wire = self.wire_at(cursor)
            if reference := self.device_at(cursor):
                QToolTip.hideText()
                self.set_hovered_wire(None)
                self.set_hovered_device(reference, event.globalPos(), self.hover_help(cursor))
            elif wire:
                self.set_hovered_device(None)
                QToolTip.hideText()
                self.set_hovered_wire(wire, event.globalPos())
            elif help_text := self.hover_help(cursor):
                self.clear_hover()
                QToolTip.showText(event.globalPos(), help_text, self.viewport())
            else:
                self.clear_hover()
                QToolTip.hideText()
                event.ignore()
            return True
        return super().viewportEvent(event)

    def clear_hover(self):
        self.set_hovered_wire(None)
        self.set_hovered_device(None)

    def set_hovered_device(self, reference, position=None, help_text=""):
        if reference != self.hovered_device:
            self.hovered_device = reference
            self.device_hovered.emit(reference, position, help_text)

    def device_at(self, cursor):
        source = SourceInfo(self.toPlainText(), self.read_imports)
        return source.device_at(cursor.blockNumber(), cursor.positionInBlock())

    def set_hovered_wire(self, wire, position=None):
        if wire != self.hovered_wire:
            self.hovered_wire = wire
            self.wire_hovered.emit(wire, position)

    @staticmethod
    def wire_at(cursor):
        source = SourceInfo(cursor.block().text())
        location = source.wire_at(0, cursor.positionInBlock())
        if location is not None:
            return (cursor.blockNumber() + 1, *location[1:])
        return None

    def resize_gutter(self):
        width = (
            self.fontMetrics().horizontalAdvance("9") * len(str(self.blockCount())) + 16
        )
        self.setViewportMargins(width, 0, 0, 0)
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.topLeft(), QSize(width, rect.height())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_gutter()

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def line_cursor(self, line):
        block = self.document().findBlockByNumber(
            max(0, min(line, self.blockCount()) - 1)
        )
        cursor = QTextCursor(block)
        first = len(block.text()) - len(block.text().lstrip())
        cursor.movePosition(
            QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, first
        )
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        return cursor

    def go_to_line(self, line):
        self.setTextCursor(self.line_cursor(line))
        self.setFocus()
        self.centerCursor()

    def annotate(self, issues):
        selections = []
        for issue in issues:
            if issue.line is None:
                continue
            selection = QTextEdit.ExtraSelection()
            selection.cursor = self.line_cursor(issue.line)
            selection.format.setUnderlineStyle(
                QTextCharFormat.UnderlineStyle.WaveUnderline
            )
            selection.format.setUnderlineColor(
                QColor("#f87171" if issue.severity == "error" else "#ffd18b")
            )
            selections.append(selection)
        self.setExtraSelections(selections)
