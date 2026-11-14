"""The native circuit source editor and its compiler annotations."""

import re

from PySide6.QtCore import QEvent, QRect, QSize, QStringListModel, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QToolTip, QWidget

from .numbers import NUMBER
from .syntax import DEFAULT_TERMINALS, MAIN, MARKER, OP, SECTION, source_rows

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
INDENT = " " * 4


def _methods(source):
    """Collect method signatures from incomplete editor text for completion."""
    methods = {}
    depth = 0
    for text, _, _ in source_rows(source):
        if depth == 0 and (match := SECTION.fullmatch(text)):
            kind, name, parameters = match.groups()
            if not OP.fullmatch(name):
                pins = (
                    DEFAULT_TERMINALS if parameters is None else parameters.split(",")
                )
                methods.setdefault(
                    name.casefold(),
                    (kind.lower(), name, ", ".join(p.strip() for p in pins)),
                )
        depth = max(0, depth + text.count("{") - text.count("}"))
    return methods


def _accepts_calls(source):
    """Track whether the cursor is inside a block that accepts method calls."""
    blocks = []
    for text, _, _ in source_rows(source):
        if text.endswith("{"):
            section = SECTION.fullmatch(text)
            blocks.append(
                bool(
                    MAIN.fullmatch(text)
                    or section
                    or (
                        blocks
                        and blocks[-1]
                        and re.match(r"(?:series|parallel)\b", text, re.I)
                    )
                )
            )
        elif text == "}" and blocks:
            blocks.pop()
        elif MARKER.fullmatch(text):
            if text[1:].lstrip().lower().startswith("end "):
                if blocks:
                    blocks.pop()
            else:
                blocks.append(False)
    return bool(blocks and blocks[-1])


class CircuitHighlighter(QSyntaxHighlighter):
    """Color source tokens without requiring the circuit to compile."""

    rules = (
        (
            re.compile(
                r"\b(?:series|parallel|circuit|between|battery|opamp|end|main|return|measure|wire|ground|model|port)\b",
                re.I,
            ),
            "#92bfff",
        ),
        (re.compile(r"\b(?:[RCLXVI]\d[\w]*|op\d+)\b"), "#e3b5f5"),
        (re.compile(rf"(?<![\w.])[+-]?{NUMBER}(?:[A-Za-zµμΩ°]+)?"), "#ffd18b"),
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

    def __init__(self):
        super().__init__()
        self.setFont(QFont("Consolas", 11))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(INDENT))
        self.setAccessibleName("Circuit source code")
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
        self.blockCountChanged.connect(self.resize_gutter)
        self.updateRequest.connect(self.update_gutter)
        self.resize_gutter()

    def _before_cursor(self):
        cursor = self.textCursor()
        cursor.setPosition(0, QTextCursor.MoveMode.KeepAnchor)
        return cursor.selectedText().replace("\u2029", "\n")

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
        methods = _methods(self.toPlainText())
        builtin = prefix if OP.fullmatch(prefix) else "op1"
        names = {key: method[1] for key, method in methods.items()}
        names[builtin.casefold()] = builtin
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
        method = _methods(self.toPlainText()).get(name.casefold())
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
        code = cursor.block().text().partition("#")[0]
        column = cursor.positionInBlock()
        for match in IDENTIFIER.finditer(code):
            if match.start() <= column <= match.end():
                name = match[0]
                if OP.fullmatch(name):
                    return (
                        f"{name}(in+, in-, out)\n"
                        "1. Non-inverting input (+)\n"
                        "2. Inverting input (-)\n"
                        "3. Output\n\n"
                        "An op amp drives its output in response to the difference between "
                        "its inputs. With negative feedback, this ideal model keeps the two "
                        "input voltages equal and draws zero input current. Connect out "
                        "back to in- to make a voltage follower."
                    )
                method = _methods(self.toPlainText()).get(name.casefold())
                if method:
                    kind, declared, pins = method
                    return f"{declared}({pins})\nCall this {kind} section with wires in the listed order."
                break
        return ""

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip:
            help_text = self.hover_help(self.cursorForPosition(event.pos()))
            if help_text:
                QToolTip.showText(event.globalPos(), help_text, self.viewport())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().viewportEvent(event)

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
