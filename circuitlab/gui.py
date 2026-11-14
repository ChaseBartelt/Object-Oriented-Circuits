"""Native editor, schematic preview, and analysis results."""

import argparse
import re
import sys
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile, Qt
from PySide6.QtGui import QColor, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .analysis import Diagnostic, analyze, diagnostics
from .compiler import analysis_mode, compile_circuit
from .editor import CircuitEditor
from .graph import CircuitError
from .ltspice import export_schematic
from .numbers import format_value
from .pins import circuit_text, pin_comment, pin_entries, read_pins
from .quantities import QUANTITIES
from .report import pin_rows, result_rows
from .schematic import SchematicView
from .spice import export_spice

MODES = {
    "auto": "Auto",
    "dc": "DC steady state",
    "ac": "AC steady state",
    "mixed": "DC + AC",
    "transient": "Transient",
}
MIXED_TEMPLATES = {
    "V": ("Voltage source (DC | AC)", "in 0 0 V | 0 Vpk"),
    "I": ("Current source (DC | AC)", "0 out 0 A | 0 Apk"),
    "R": ("Resistor", "in out 1k"),
    "C": ("Capacitor", "out 0 1u"),
    "L": ("Inductor", "out 0 1m"),
    "port": ("Equivalent at two nodes", "port = out 0"),
}
STYLESHEET = """
QMainWindow, QDialog, QWidget { background: #282828; color: #f2f2f2; }
QPlainTextEdit, QLineEdit, QListWidget { background: #191919; }
QPushButton, QComboBox { background: #393939; border: 1px solid #666; border-radius: 4px;
                        padding: 6px 12px; }
QPushButton:hover, QComboBox:hover { background: #4b4b4b; }
QPushButton#compile { background: #a92537; font-weight: bold; }
QPushButton#compile:hover { background: #c33449; }
QTabBar::tab { background: #343434; padding: 7px 16px; border: 1px solid #555; }
QTabBar::tab:selected { background: #191919; }
QHeaderView::section { background: #363636; color: #fff; border: 1px solid #565656; padding: 5px; }
QTableWidget { gridline-color: #6b6b6b; selection-background-color: #365777; }
QTableWidget::item:selected { color: white; }
QSplitter::handle { background: #5b5b5b; }
QMenu::item:selected, QMenuBar::item:selected { background: #505050; }
"""


def button(label, callback, layout):
    control = QPushButton(label)
    control.clicked.connect(callback)
    layout.addWidget(control)
    return control


def search_text(value):
    return " ".join(value.casefold().replace("_", " ").split())


class Results(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search quantities")
        self.search.textChanged.connect(self.filter)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Quantity", "Equation", "Value"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(1, 240)
        layout.addWidget(self.table)

    def update_rows(self, rows):
        self.rows = rows
        self.filter()

    def filter(self):
        query = search_text(self.search.text())
        visible = [row for row in self.rows if query in search_text(row[0])]
        self.table.clearContents()
        self.table.setRowCount(len(visible))
        for i, row in enumerate(visible):
            for j, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setBackground(QColor("#363636" if i % 2 == 0 else "#d5d5d5"))
                item.setForeground(QColor("#f2f2f2" if i % 2 == 0 else "#191919"))
                self.table.setItem(i, j, item)


class MainWindow(QMainWindow):
    """Coordinate the source document, analysis results, and schematic view."""

    def __init__(self):
        super().__init__()
        self.path = None
        self.disk_bytes = None
        self.solution = None
        self.pins = {}
        self.circuit_text = ""
        self.resize(1280, 780)
        self.setStyleSheet(STYLESHEET)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.addLayout(self._create_toolbar())

        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        layout.addWidget(self.split, 1)
        self.split.addWidget(self._create_editor_panel())
        self.views = QTabWidget()
        self.split.addWidget(self.views)
        self.split.setSizes([530, 750])
        self.views.addTab(self._create_schematic_page(), "Schematic")

        self.results = Results()
        self.views.addTab(self.results, "Results")
        self.status = QLabel("Open a circuit or enter known quantities.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status)

        self.analysis_window = QDialog(self)
        self.analysis_window.setWindowTitle("Circuit analysis")
        self.analysis_window.resize(900, 560)
        self.analysis_results = Results()
        QVBoxLayout(self.analysis_window).addWidget(self.analysis_results)

        self._create_menus()
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.editor.textChanged.connect(self.source_changed)
        self.editor.document().modificationChanged.connect(self.setWindowModified)
        self.update_title()
        self.refresh_picker()
        self.show_issues([])

    def _create_toolbar(self):
        """Build the analysis controls above the split view."""
        toolbar = QHBoxLayout()
        self.mode = QComboBox()
        for key, label in MODES.items():
            self.mode.addItem(label, key)
        toolbar.addWidget(self.mode)
        self.picker = QComboBox()
        self.picker.activated.connect(self.insert_quantity)
        toolbar.addWidget(self.picker, 1)
        button("Compile / Solve", self.compile_source, toolbar).setObjectName("compile")
        button("Render schematic", self.render_source, toolbar)
        button("Analysis window", self.show_analysis, toolbar)
        button("Export to SPICE", self.export_netlist, toolbar).setToolTip(
            "Save the current circuit as an LTspice netlist (.cir)."
        )
        return toolbar

    def _create_editor_panel(self):
        """Pair the source editor with its line-linked diagnostics."""
        source_panel = QWidget()
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.editor = CircuitEditor()
        source_layout.addWidget(self.editor, 1)
        self.issues = QListWidget()
        self.issues.setMaximumHeight(95)
        self.issues.itemClicked.connect(self.select_diagnostic)
        source_layout.addWidget(self.issues)
        return source_panel

    def _create_schematic_page(self):
        """Build the diagram view, wire display controls, and pin tools."""
        page = QWidget()
        canvas = QVBoxLayout(page)
        canvas.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        controls.setContentsMargins(4, 4, 4, 0)
        self.schematic = SchematicView()
        self.schematic.probed.connect(self.probe)
        self.schematic.pin_requested.connect(self.name_pin)
        self.schematic.pin_renamed.connect(self.name_pin)
        self.schematic.pin_removed.connect(lambda net: self.write_pin(net, None))
        self.wire_style = QComboBox()
        for label, mode, tip in (
            ("Auto layout", "auto", "Use matching net labels when wires get crowded."),
            (
                "Labeled connectors",
                "labeled",
                "Use matching names to connect distant terminals.",
            ),
            (
                "All wires",
                "routed",
                "Draw each non-ground connection as a continuous wire.",
            ),
        ):
            self.wire_style.addItem(label, mode)
            self.wire_style.setItemData(
                self.wire_style.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
            )
        self.wire_style.setAccessibleName("Schematic wiring style")
        self.wire_style.setToolTip(
            "Choose how connections are drawn. Ground symbols all connect to node 0."
        )
        self.wire_style.currentIndexChanged.connect(self.change_wiring)
        controls.addWidget(self.wire_style)
        self.pin_button = button("Add pin", self.toggle_pin_tool, controls)
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip(
            "Place a named probe on a wire, connector, or component terminal."
        )
        button("Zoom in", lambda: self.schematic.zoom(1.4), controls)
        button("Zoom out", lambda: self.schematic.zoom(1 / 1.4), controls)
        button("Fit", self.schematic.fit, controls)
        button("Export LTspice .asc", self.export_ltspice, controls).setToolTip(
            "Save this layout as an editable LTspice schematic."
        )
        controls.addStretch()
        canvas.addLayout(controls)
        canvas.addWidget(self.schematic)
        return page

    def _create_menus(self):
        files = self.menuBar().addMenu("&File")
        files.addAction("&New", self.new_file, QKeySequence.StandardKey.New)
        files.addAction("&Open…", self.pick_file, QKeySequence.StandardKey.Open)
        files.addAction("&Save", self.save, QKeySequence.StandardKey.Save)
        files.addAction("Save &As…", self.save_as, QKeySequence.StandardKey.SaveAs)
        files.addAction("&Reload from disk", self.reload_file)
        files.addSeparator()
        files.addAction("Export to &SPICE...", self.export_netlist)
        files.addAction("Export LTspice &Schematic...", self.export_ltspice)
        files.addSeparator()
        files.addAction("&Quit", self.close, QKeySequence.StandardKey.Quit)
        compile_action = (
            self.menuBar()
            .addMenu("&Circuit")
            .addAction("&Compile / Solve", self.compile_source)
        )
        compile_action.setShortcuts(
            [QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")]
        )

    def effective_mode(self):
        mode = self.mode.currentData()
        return analysis_mode(self.editor.toPlainText()) if mode == "auto" else mode

    def refresh_picker(self):
        self.picker.clear()
        self.picker.addItem("Add a quantity")
        mode = self.effective_mode()
        info = QUANTITIES["ac" if mode == "mixed" else mode]
        for key, (label, _) in info.items():
            if mode != "mixed" or key in {"f", "T", "w"}:
                self.picker.addItem(f"{key} · {label}", (key, False))
        if mode in {"ac", "mixed"}:
            self.picker.addItem("Vdc · DC offset in a battery", ("Vdc", False))
        if mode == "mixed":
            for key, (label, _) in MIXED_TEMPLATES.items():
                self.picker.addItem(f"{key} · {label}", (key, True))
        self.picker_mode = mode

    def insert_quantity(self, index):
        data = self.picker.itemData(index)
        if not data:
            return
        key, mixed = data
        source = self.editor.toPlainText()
        entry = f"{key} = "
        if mixed:
            entry = MIXED_TEMPLATES[key][1]
            if key != "port":
                names = set(re.findall(r"^[ \t]*([A-Za-z]\w*)[ \t]", source, re.M))
                names = {name.casefold() for name in names}
                count = 1
                while f"{key}{count}".casefold() in names:
                    count += 1
                entry = f"{key}{count} {entry}"
        cursor = self.editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(
            ("\n" if source and not source.endswith("\n") else "") + entry
        )
        self.editor.setTextCursor(cursor)
        self.picker.setCurrentIndex(0)
        self.editor.setFocus()

    def show_issues(self, issues):
        self.editor.annotate(issues)
        self.issues.clear()
        if not issues:
            item = QListWidgetItem("No compiler diagnostics.")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.issues.addItem(item)
        for issue in issues:
            item = QListWidgetItem(
                f"Line {issue.line} · {issue.severity} · {issue.message}"
            )
            item.setForeground(
                QColor("#f87171" if issue.severity == "error" else "#ffd18b")
            )
            item.setData(Qt.ItemDataRole.UserRole, issue.line)
            item.setToolTip(issue.message)
            self.issues.addItem(item)

    def select_diagnostic(self, item):
        if line := item.data(Qt.ItemDataRole.UserRole):
            self.editor.go_to_line(line)

    def set_solution(self, result):
        self.solution = result
        rows = result_rows(result) if result else []
        rows = pin_rows(result, self.pins) + rows
        self.results.update_rows(rows)
        self.analysis_results.update_rows(rows)

    def source_changed(self):
        source = self.editor.toPlainText()
        previous_pins, self.pins = self.pins, read_pins(source)
        code = circuit_text(source)
        # Pin metadata is a comment. Renaming a pin does not invalidate a solve.
        if self.pins != previous_pins and code == self.circuit_text:
            self.schematic.update_pins(self.pins)
            self.set_solution(self.solution)
            return
        self.circuit_text = code
        if self.effective_mode() != self.picker_mode:
            self.refresh_picker()
        self.set_solution(None)
        self.schematic.clear()
        self.pin_button.setChecked(False)
        self.schematic.set_pin_tool(False)
        self.show_issues([])
        self.status.setText("Compile to refresh the schematic and results.")

    def mode_changed(self):
        self.refresh_picker()
        self.compile_source()

    def compile_source(self):
        source = self.editor.toPlainText()
        mode = self.effective_mode()
        self.set_solution(None)
        self.schematic.clear()
        issues = []
        if not any(row.partition("#")[0].strip() for row in source.splitlines()):
            self.status.setText("Open a circuit or enter known quantities.")
        else:
            try:
                result = analyze(compile_circuit(source, mode))
                self.set_solution(result)
                issues = diagnostics(source, result)
            except CircuitError as error:
                issues = [self.issue(error)]
            try:
                self._draw_source(source, mode)
            except CircuitError:
                pass
            if issues:
                self.status.setText(issues[0].message)
            elif self.solution and self.solution.network:
                self.status.setText(
                    f"{MODES[self.solution.circuit.mode]} · Click a wire to trace it or add a named pin."
                )
            else:
                self.status.setText("Results updated.")
                self.views.setCurrentWidget(self.results)
        self.show_issues(issues)

    def _draw_source(self, source, mode):
        """Render a compiled graph even when electrical analysis cannot solve it."""
        circuit = compile_circuit(source, mode, render_only=True)
        self.schematic.draw(circuit, self.pins)

    def render_source(self):
        try:
            self._draw_source(self.editor.toPlainText(), self.mode.currentData())
        except CircuitError as error:
            self.schematic.clear()
            issue = self.issue(error)
            self.show_issues([issue])
            self.status.setText(issue.message)
        else:
            self.views.setCurrentIndex(0)
            self.status.setText("Schematic rendered.")

    def change_wiring(self):
        self.schematic.layout_mode = self.wire_style.currentData()
        if self.schematic.circuit:
            selected = self.schematic.highlight
            self.schematic.draw(self.schematic.circuit, self.pins)
            if selected:
                self.schematic.highlight_net(selected)

    def toggle_pin_tool(self, active):
        if active and not self.schematic.circuit:
            self.pin_button.setChecked(False)
            self.status.setText("Compile or render a circuit before adding a pin.")
            return
        self.schematic.set_pin_tool(active)
        if active:
            self.status.setText(
                "Click a wire, connector, or component terminal to place a pin."
            )
        else:
            self.status.setText("Click a wire to trace it or add a named pin.")

    def name_pin(self, net):
        self.pin_button.setChecked(False)
        self.schematic.set_pin_tool(False)
        existing = self.pins.get(net)
        name, accepted = QInputDialog.getText(
            self,
            "Rename pin" if existing else "Add pin",
            f"Name for the pin on {net}",
            text=existing or net,
        )
        name = " ".join(name.split())
        if not accepted or not name:
            return
        if any(
            key != net and search_text(other) == search_text(name)
            for key, other in self.pins.items()
        ):
            QMessageBox.warning(
                self, "Pin name in use", "Choose a different name for this pin."
            )
            return
        self.write_pin(net, name)
        self.probe("pin", net)

    def write_pin(self, net, name):
        source = self.editor.toPlainText()
        entries = [line for line, node, _ in pin_entries(source) if node == net]
        document = self.editor.document()
        edit = QTextCursor(document)
        edit.beginEditBlock()
        for line in reversed(entries):
            edit.setPosition(document.findBlockByNumber(line).position())
            edit.movePosition(
                QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            if name is not None and line == entries[0]:
                edit.insertText(pin_comment(net, name))
            else:
                edit.movePosition(
                    QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor
                )
                edit.removeSelectedText()
        if name is not None and not entries:
            edit.movePosition(QTextCursor.MoveOperation.End)
            edit.insertText(
                ("\n" if source and not source.endswith("\n") else "")
                + pin_comment(net, name)
                + "\n"
            )
        edit.endEditBlock()
        if name is None:
            if search_text(self.results.search.text()) == search_text(
                read_pins(source).get(net, "")
            ):
                self.results.search.clear()
            self.status.setText("Pin removed.")

    @staticmethod
    def issue(error):
        message = re.sub(r"^Line \d+\.\s*", "", str(error))
        return Diagnostic("error", error.line or 1, message)

    def show_analysis(self):
        self.analysis_window.show()
        self.analysis_window.raise_()
        self.analysis_window.activateWindow()

    def probe(self, kind, name):
        if kind == "pin" and name in self.pins:
            self.results.search.setText(self.pins[name])
            self.views.setCurrentWidget(self.results)
            kind = "node"
        result = self.solution
        if not result or not result.network:
            self.status.setText("Compile to get probe readings.")
            return
        readings = []
        for network, ac in (
            (result.dc_network, False),
            (result.network, result.circuit.mode != "dc"),
        ):
            if not network:
                continue
            prefix = "AC RMS · " if ac else "DC · " if result.dc_network else ""
            if kind == "node" and name in network.nodes:
                value = network.voltage[network.nodes.index(name)]
                reference = network.references.get(name, "0")
                readings.append(
                    prefix
                    + f"V({name}) relative to {reference} = "
                    + format_value(value, "V", ac, False)
                )
            elif kind == "component" and name in network.names:
                i = network.names.index(name)
                v = format_value(network.branch_voltage[i], "V", ac, False)
                amps = format_value(network.branch_current[i], "A", ac, False)
                readings.append(prefix + f"{name} voltage = {v}   |   current = {amps}")
        self.status.setText("\n".join(readings) or "No result for this selection.")

    def update_title(self):
        name = self.path.name if self.path else "Untitled.ckt"
        self.setWindowTitle(f"{name}[*] — Electric Circuits")

    def new_file(self):
        if self.confirm_edits():
            self.path, self.disk_bytes = None, None
            self.editor.setPlainText("")
            self.update_title()
            self.compile_source()

    def pick_file(self):
        if self.confirm_edits():
            name, _ = QFileDialog.getOpenFileName(
                self, "Open circuit", "", "Circuit files (*.ckt *.txt);;All files (*)"
            )
            if name:
                self.open_path(name)

    def open_path(self, path):
        path = Path(path).absolute()
        try:
            data = path.read_bytes()
            source = data.decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            QMessageBox.warning(self, "Open circuit", str(error))
            return False
        self.path, self.disk_bytes = path, data
        self.editor.setPlainText(source)
        self.editor.document().setModified(False)
        self.update_title()
        self.compile_source()
        return True

    def reload_file(self):
        if self.path and self.confirm_edits():
            self.open_path(self.path)

    def save_as(self):
        name, _ = QFileDialog.getSaveFileName(
            self,
            "Save circuit",
            str(self.path or "Untitled.ckt"),
            "Circuit files (*.ckt);;Text files (*.txt);;All files (*)",
        )
        if not name:
            return False
        path = Path(name)
        return self.write_path(path if path.suffix else path.with_suffix(".ckt"))

    def export_netlist(self):
        return self._export_file(
            lambda: export_spice(self.editor.toPlainText(), self.mode.currentData()),
            "Export to SPICE",
            ".cir",
            "SPICE netlists (*.cir)",
        )

    def export_ltspice(self):
        used_labels = False

        def schematic_text():
            nonlocal used_labels
            if self.schematic.layout is None:
                self._draw_source(self.editor.toPlainText(), self.mode.currentData())
                self.views.setCurrentIndex(0)
            text, used_labels = export_schematic(
                self.schematic.circuit, self.schematic.layout
            )
            return text

        exported = self._export_file(
            schematic_text,
            "Export LTspice Schematic",
            ".asc",
            "LTspice schematics (*.asc)",
        )
        if exported and used_labels:
            self.status.setText(
                self.status.text() + " Matching net labels connect the crowded wires."
            )
        return exported

    def _export_file(self, generate, title, suffix, file_filter):
        try:
            data = generate().encode("ascii")
        except CircuitError as error:
            self.status.setText(str(error))
            if error.line is not None:
                self.show_issues([self.issue(error)])
            QMessageBox.warning(self, title, str(error))
            return False
        suggested = (self.path or Path("Untitled.ckt")).with_suffix(suffix)
        name, _ = QFileDialog.getSaveFileName(self, title, str(suggested), file_filter)
        if not name:
            return False
        path = Path(name)
        if path.suffix.lower() != suffix:
            path = path.with_name(path.name + suffix)
            if (
                path.exists()
                and QMessageBox.question(
                    self,
                    title,
                    f"Replace {path.name}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return False
        if not write_bytes(self, path, data, title):
            return False
        self.status.setText(f"Exported {path.name}. Open it in LTspice and click Run.")
        return True

    def save(self):
        if not self.path:
            return self.save_as()
        try:
            external_change = self.path.read_bytes() != self.disk_bytes
        except OSError:
            external_change = True
        if external_change:
            answer = QMessageBox.question(
                self,
                "Circuit file changed",
                "The file changed on disk. Save this editor version over it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        return self.write_path(self.path)

    def write_path(self, path):
        path = Path(path).absolute()
        source = self.editor.toPlainText()
        old = self.disk_bytes or b""
        if not self.editor.document().isModified() and self.disk_bytes is not None:
            data = self.disk_bytes
        else:
            if b"\r\n" in old:
                source = source.replace("\n", "\r\n")
            elif b"\r" in old:
                source = source.replace("\n", "\r")
            data = source.encode(
                "utf-8-sig" if old.startswith(b"\xef\xbb\xbf") else "utf-8"
            )
        if not write_bytes(self, path, data, "Save circuit"):
            return False
        self.path, self.disk_bytes = path, data
        self.editor.document().setModified(False)
        self.update_title()
        self.status.setText(f"Saved {path.name}.")
        return True

    def confirm_edits(self):
        if not self.editor.document().isModified():
            return True
        options = QMessageBox.StandardButton
        choice = QMessageBox.question(
            self,
            "Save changes",
            "Save your changes to the circuit?",
            options.Save | options.Discard | options.Cancel,
            options.Save,
        )
        return choice == options.Discard or (choice == options.Save and self.save())

    def closeEvent(self, event):
        if self.confirm_edits():
            event.accept()
        else:
            event.ignore()


def write_bytes(parent, path, data, title):
    """Commit a complete file or leave the previous file intact."""
    file = QSaveFile(str(path))
    if (
        not file.open(QIODevice.OpenModeFlag.WriteOnly)
        or file.write(data) != len(data)
        or not file.commit()
    ):
        file.cancelWriting()
        QMessageBox.warning(parent, title, file.errorString())
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Edit, compile, and analyze an Electric Circuits file."
    )
    parser.add_argument("file", nargs="?", help="Circuit source file")
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    window = MainWindow()
    if args.file:
        window.open_path(args.file)
    window.show()
    return app.exec()
