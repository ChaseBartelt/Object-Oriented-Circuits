"""The native circuit source editor and its compiler annotations."""
# Editing, coloring, completion, and hover remain usable before the source compiles.
# SourceInfo supplies tolerant token/signature knowledge, while compilation remains the
# authority for whether the text describes a valid electrical or digital design.

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
from circuitlab.language.syntax import OP, TRANS_CALL
from circuitlab.language.wire_counts import COUNT_REFERENCE, is_count_reference
from circuitlab.tooling.source_info import (
    IDENTIFIER, WIRE_COUNT_DECLARATION, SourceInfo, accepts_calls as _accepts_calls,
)

INDENT = " " * 4
BUILTIN_CALL = re.compile(
    rf"\b(?:{OP.pattern}|{TRANS_CALL.pattern}|{'|'.join(GATE_HELP)})\b(?=\s*\()",
    re.I,
)
NUMBER_TOKEN = re.compile(
    r"(?<![\w.])([+−-]?(?:[0-9]+(?:_[0-9]+)*(?:\.[0-9_]*)?|\.[0-9][0-9_]*)"
    r"(?:[eE][+-]?[0-9][0-9_]*)?[ij]?)"
    r"(?:[ \t]*([pnuµμmkMG]?(?:V/(?:[munµμ]?s)|rad/s|ohms?|Hz|VA|var|Nm|deg|dB|"
    r"V(?:rms|pk|pp)?|A(?:rms|pk|pp)?|Ω|F|H|s|W|J|C|°)|[pnuµμmkMG]|%))?"
    r"(?=cos\s*\(|[^A-Za-z0-9_.]|$)"
)
PROTECTED_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|"(?:\\.|[^"\\])*$|#.*')


class CircuitHighlighter(QSyntaxHighlighter):
    """Color source tokens without requiring the circuit to compile."""

    # Broad categories go first; context-specific names override them below.
    # Capture indexes let declarations color the name without repainting its
    # keyword, and let SI units differ from their numeric magnitude.
    rules = (
        (re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b"), "#c7dce8", 0),
        (re.compile(r"<<=?|>>=?|==|!=|<=|>=|&&|\|\||\+\+|--|[+*/%=!?&|^<>∠.\-]"), "#bac3ce", 0),
        (re.compile(r"[{}(),;:\[\]]"), "#98a3b3", 0),
        (
            re.compile(
                r"\b(?:series|parallel|circuit|chip|gate|between|battery|opamp|transistor|"
                r"end|main|return|wire|ground|model|port|input|output|connector|include|"
                r"const|let|int|bool|float|Vneg)\b",
                re.I,
            ),
            "#92bfff", 0,
        ),
        (re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?=\s*\()"), "#e6dfa2", 0),
        (re.compile(r"\b(?:chip|circuit|gate|series|parallel)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I), "#e6dfa2", 1),
        (re.compile(r"\b(?:for|while|do|if|else|clock|every|switch|case|default|break|continue)\b", re.I), "#d995d9", 0),
        (re.compile(r"\banalog\b(?=\s*(?::\s*math\b|\{|\(|[A-Za-z_]|$))", re.I), "#659cd7", 0),
        (re.compile(r"\bdigital\b(?=\s*(?::\s*math\b|\{|\(|[A-Za-z_]|$))", re.I), "#659cd7", 0),
        (re.compile(r"\b(?:property|properties)\b", re.I), "#64d8cb", 0),
        (re.compile(r"\b(?:true|false|open|closed|on|off|ideal|npn|pnp|open_collector|power_amplifier|unavailable)\b", re.I), "#b5d49d", 0),
        (re.compile(r"(?<![\w.])(?:pi|e|time|t)\b"), "#b5d49d", 0),
        (re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?![\w.])"), "#7fd5e6", 0),
        (re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?=\s*=(?!=))"), "#7fd5e6", 1),
        (re.compile(r"\bproperty\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)", re.I), "#7fd5e6", 1),
        (re.compile(r"(?:^|(?<=[;{}:]))[ \t]*(measure)(?=[ \t]*(?:=(?!=)|$))", re.I), "#aed6ff", 1),
        (re.compile(r"\b(?:V|I)(?=\s*\()"), "#e6dfa2", 0),
        (NUMBER_TOKEN, "#ffd18b", 1),
        (NUMBER_TOKEN, "#dfb76c", 2),
        (re.compile(r"\b(?:[RCLXZVIQPS]\d[\w]*|op\d+)\b", re.I), "#e3b5f5", 0),
        (re.compile(r"\b[RCLZVIPS]\w*(?=\s*(?:\([^()]*\)\s*)?between\s*\()", re.I), "#e3b5f5", 0),
        (BUILTIN_CALL, "#e3b5f5", 0),
        (re.compile(r"@override\b", re.I), "#f6a652", 0),
    )

    # Color recognized tokens even in incomplete text; highlighting must not require a successful
    # compile or consume comments as executable code.
    def highlightBlock(self, text):
        # Qt ranges count UTF-16 code units, while regex spans use Python
        # characters. A quoted emoji must not shift every following color.
        offsets = [0]
        for character in text:
            offsets.append(offsets[-1] + (2 if ord(character) > 0xFFFF else 1))

        def paint(start, end, style):
            self.setFormat(offsets[start], offsets[end] - offsets[start], style)

        protected = list(PROTECTED_TOKEN.finditer(text))
        masked = list(text)
        for token in protected:
            masked[token.start():token.end()] = " " * len(token[0])
        code = "".join(masked)
        for pattern, color, group in self.rules:
            for match in pattern.finditer(code):
                start, end = match.span(group)
                if start >= 0:
                    paint(start, end, QColor(color))
        for declaration in WIRE_COUNT_DECLARATION.finditer(code):
            paint(declaration.start(2), declaration.end(2), QColor("#659cd7"))
        info = SourceInfo(code)
        for token in COUNT_REFERENCE.finditer(code) if any(mark in code for mark in "*^$") else ():
            if not is_count_reference(token[0]):
                continue
            column = len(code[:token.start()].encode("utf-16-le")) // 2
            location = info.wire_at(0, column)
            # Match a complete connection slot. Arithmetic in assignments,
            # analog equations, values and control headers stays arithmetic.
            if location and location[3] == token[0].strip():
                paint(token.start(), token.end(), QColor("#e3b5f5"))
        for token in protected:
            comment = token[0].startswith("#")
            paint(token.start(), token.end(), QColor("#9bc68b" if comment else "#d6b88c"))
            if not comment:
                continue
            for match in re.finditer(r"@[A-Za-z]+\b", token[0]):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor("#b5d49d"))
                fmt.setFontWeight(QFont.Weight.Bold)
                paint(token.start() + match.start(), token.start() + match.end(), fmt)


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
        names.setdefault("s1", "S1")
        self.completions.setStringList([names[key] for key in sorted(names)])
        self.completer.setCompletionPrefix(prefix)
        if not self.completer.setCurrentRow(0):
            popup.hide()
            return
        rect = self.cursorRect()
        rect.setWidth(max(130, popup.sizeHintForColumn(0) + 24))
        self.completer.complete(rect)
        popup.setCurrentIndex(self.completer.currentIndex())

    # Respect an already-present argument list and select only the newly inserted placeholder
    # so accepting a completion does not overwrite the user's existing connections.
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
        elif name.casefold() == "s1" and method is None:
            start = cursor.selectionStart()
            cursor.insertText(name + "(open) between(a, b)" + ("" if suffix.startswith(";") else ";"))
            cursor.setPosition(start + len(name) + 1)
            cursor.setPosition(start + len(name) + 5, QTextCursor.MoveMode.KeepAnchor)
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

    # Translate the block-local editor cursor into the shared source-inspection identity.
    # The caller adds the document line before requesting a compiler-backed trace.
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
