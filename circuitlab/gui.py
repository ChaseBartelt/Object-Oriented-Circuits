"""Native editor, schematic preview, and analysis results."""

import argparse
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QDir, QIODevice, QPoint, QSaveFile, QSettings, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFileSystemModel,
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
    QSpinBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .analysis import Diagnostic, analyze, diagnostics
from .compiler import analysis_mode, compile_circuit
from .device_reference import DeviceReference
from .editor import CircuitEditor
from .gate_layouts import GateLayouts, default_gate_layout, gate_preview
from .gates import GATE_HELP, gate_accepts
from .graph import Circuit, CircuitError, MethodInstance
from .linked import CHIP_LIBRARY, imported_definitions, read_includes
from .ltspice import export_hierarchy, export_schematic
from .numbers import format_value
from .pins import circuit_text, pin_comment, pin_entries, read_pins
from .placement import placement_path, read_placement, write_placement
from .quantities import QUANTITIES
from .report import component_labels, pin_rows, result_rows
from .routing import fast_layout
from .schematic import SchematicView
from .scope import Oscilloscope
from .spice import export_spice
from .syntax import INCLUDE, MAIN, has_chip, source_rows
from .trace import wire_trace
from .verilog import compile_chips, export_verilog

MODES = {
    "auto": "Auto",
    "dc": "DC steady state",
    "ac": "AC steady state",
    "mixed": "DC + AC",
    "transient": "Transient",
}
MIXED_TEMPLATES = {
    "V": ("Voltage source (DC | AC)", "(0 V | 0 Vpk) between(in, 0);"),
    "I": ("Current source (DC | AC)", "(0 A | 0 Apk) between(0, out);"),
    "R": ("Resistor", "(1k) between(in, out);"),
    "C": ("Capacitor", "(1u) between(out, 0);"),
    "L": ("Inductor", "(1m) between(out, 0);"),
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


class GateLayoutDialog(QDialog):
    """Edit the schematic used by every instance of a built-in gate."""

    def __init__(self, parent, layouts, initial=None):
        super().__init__(parent)
        self.setWindowTitle("Gate Layouts")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint)
        self.setSizeGripEnabled(True)
        available = parent.screen().availableGeometry()
        self.resize(min(900, available.width() - 48), min(620, available.height() - 64))
        self.layouts = layouts
        self.drafts = {}
        self.key = None
        self.max_inputs = max(32, initial[1] if initial else 0)
        panel = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.gate = QComboBox()
        self.gate.setAccessibleName("Gate")
        for name in GATE_HELP:
            self.gate.addItem(name.upper(), name)
        self.inputs = QSpinBox()
        self.inputs.setAccessibleName("Input count")
        controls.addWidget(QLabel("Gate"))
        controls.addWidget(self.gate)
        controls.addWidget(QLabel("Inputs"))
        controls.addWidget(self.inputs)
        controls.addStretch()
        self.preview = SchematicView()
        button("Fit", self.preview.fit, controls)
        panel.addLayout(controls)
        panel.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        button("Restore automatic", self.restore_automatic, actions)
        button("Apply to all", self.apply_layout, actions)
        button("Close", self.reject, actions)
        panel.addLayout(actions)
        footer = QHBoxLayout()
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setContentsMargins(0, 0, 16, 0)
        footer.addWidget(self.message, 1)
        self.saved_button = button("Use saved layout", self.restore_saved, footer)
        panel.addLayout(footer)
        self.preview.placement_changed.connect(self.record_draft)
        self.preview.move_refused.connect(
            lambda: self.message.setText("Try an open spot where the existing nets can stay connected.")
        )
        if initial:
            name, count = initial
            self.gate.setCurrentIndex(self.gate.findData(name))
            self.update_input_range()
            self.inputs.setValue(count)
        self.gate.currentIndexChanged.connect(self.change_gate)
        self.inputs.valueChanged.connect(self.load_gate)
        self.change_gate()

    def update_input_range(self):
        name = self.gate.currentData()
        choices = [count for count in range(1, self.max_inputs + 1) if gate_accepts(name, count)]
        with QSignalBlocker(self.inputs):
            self.inputs.setRange(choices[0], choices[-1])
            self.inputs.setEnabled(len(choices) > 1)

    def change_gate(self):
        self.update_input_range()
        self.load_gate()

    def load_gate(self):
        self.key = self.gate.currentData(), self.inputs.value()
        self.saved_button.setEnabled(self.layouts.has_saved(self.key))
        self.message.setText("Drag parts, wires, and pin ends. Click Apply to all to save.")
        record = self.drafts.get(self.key, self.layouts.get(self.key))
        self.draw_gate(record)

    def draw_gate(self, layout):
        circuit, boundary = gate_preview(*self.key)
        self.base_layout = layout
        self.preview.clear()
        self.preview.draw(circuit, boundary=boundary, layout=layout or default_gate_layout(*self.key))

    def record_draft(self):
        self.drafts[self.key] = self.preview.manual_layouts.get((None, "auto"), self.base_layout)
        self.message.setText("Layout edited. Click Apply to all to save it.")

    def restore_automatic(self):
        self.drafts[self.key] = None
        self.draw_gate(None)
        name, inputs = self.key
        self.message.setText(f"Apply the automatic layout for {inputs}-input {name.upper()} gates. Your saved layout is kept.")

    def restore_saved(self):
        layout = self.layouts.saved(self.key)
        if layout is None:
            self.message.setText("The saved layout could not be loaded.")
            return
        self.drafts[self.key] = layout
        self.draw_gate(layout)
        self.message.setText("Click Apply to all to use this saved layout again.")

    def apply_layout(self):
        if self.key not in self.drafts:
            self.message.setText("There are no layout changes to apply for this gate.")
            return
        layout = self.drafts[self.key]
        if self.layouts.set(self.key, layout):
            del self.drafts[self.key]
            self.saved_button.setEnabled(self.layouts.has_saved(self.key))
            self.parent().schematic.refresh_gate_layouts()
            name, inputs = self.key
            self.message.setText(f"Saved the layout for {inputs}-input {name.upper()} gates.")
        else:
            self.message.setText("Could not save the gate layout to your app settings.")


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


@dataclass
class CircuitTab:
    editor: CircuitEditor
    path: Path | None = None
    disk_bytes: bytes | None = None
    placements: dict | None = None
    placement_dirty: bool = False
    wiring: str = "auto"
    mode: int = 0


class MainWindow(QMainWindow):
    """Coordinate the source document, analysis results, and schematic view."""

    def __init__(self):
        super().__init__()
        self.path = None
        self.disk_bytes = None
        self.solution = None
        self.pins = {}
        self.placement_dirty = False
        self.circuit_text = ""
        self.documents = {}
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
        self.scope = Oscilloscope()
        self.results.layout().insertWidget(0, self.scope)
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

        self.trace_popup = QWidget(self, Qt.WindowType.ToolTip)
        self.trace_popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.trace_popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.trace_view = SchematicView()
        self.trace_view.setInteractive(False)
        self.trace_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        popup_layout = QVBoxLayout(self.trace_popup)
        popup_layout.setContentsMargins(2, 2, 2, 2)
        popup_layout.addWidget(self.trace_view)
        self.trace_popup.setFixedSize(300, 300)
        self.device_popup = QWidget(self, Qt.WindowType.ToolTip)
        self.device_popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.device_popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.device_reference = DeviceReference()
        self.device_help = QLabel()
        self.device_help.setTextFormat(Qt.TextFormat.PlainText)
        self.device_help.setWordWrap(True)
        self.device_help.setMaximumWidth(340)
        device_layout = QVBoxLayout(self.device_popup)
        device_layout.setContentsMargins(4, 4, 4, 4)
        device_layout.addWidget(self.device_reference)
        device_layout.addWidget(self.device_help)
        self.create_explorer()
        self._create_menus()
        self.mode.currentIndexChanged.connect(self.mode_changed)
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
        self.preview_button = button("Hide preview", self.toggle_preview, toolbar)
        self.preview_button.setToolTip("Hide or show the schematic and results panel.")
        button("Analysis window", self.show_analysis, toolbar)
        button("Export to SPICE", self.export_netlist, toolbar).setToolTip(
            "Save the current circuit as an LTspice netlist (.cir)."
        )
        return toolbar

    def toggle_preview(self):
        hidden = self.views.isHidden()
        self.views.setVisible(hidden)
        self.preview_button.setText("Hide preview" if hidden else "Show preview")
        if not hidden:
            self.editor.setFocus()

    def _create_editor_panel(self):
        """Pair the source editor with its line-linked diagnostics."""
        source_panel = QWidget()
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.editor_tabs = QTabWidget()
        self.editor_tabs.setTabsClosable(True)
        self.editor_tabs.setMovable(True)
        self.editor_tabs.currentChanged.connect(self.switch_tab)
        self.editor_tabs.tabCloseRequested.connect(self.close_tab)
        source_layout.addWidget(self.editor_tabs, 1)
        self.editor = self.add_tab()
        self.issues = QListWidget()
        self.issues.setMaximumHeight(95)
        self.issues.itemClicked.connect(self.select_diagnostic)
        source_layout.addWidget(self.issues)
        return source_panel

    def create_explorer(self):
        self.explorer = QDockWidget("Files", self)
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(4, 4, 4, 4)
        controls = QHBoxLayout()
        self.folder_back = self.folder_button(
            controls, "Back", QStyle.StandardPixmap.SP_ArrowBack, lambda: self.navigate_folder(-1)
        )
        self.folder_forward = self.folder_button(
            controls, "Forward", QStyle.StandardPixmap.SP_ArrowForward, lambda: self.navigate_folder(1)
        )
        self.folder_up = self.folder_button(
            controls, "Up", QStyle.StandardPixmap.SP_ArrowUp, self.parent_folder
        )
        controls.addStretch()
        panel_layout.addLayout(controls)
        self.file_tree = QTreeView()
        self.file_model = QFileSystemModel(self.file_tree)
        self.file_model.setReadOnly(True)
        self.file_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.file_model.setNameFilters(["*.ckt", "*.txt"])
        self.file_model.setNameFilterDisables(False)
        self.file_tree.setModel(self.file_model)
        self.file_tree.setHeaderHidden(True)
        for column in (1, 2, 3):
            self.file_tree.hideColumn(column)
        self.file_tree.doubleClicked.connect(self.open_tree_file)
        panel_layout.addWidget(self.file_tree)
        self.explorer.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.explorer)
        self.folder = None
        self.folder_history = []
        self.folder_index = -1
        self.set_folder(Path.cwd())
        self.explorer.hide()

    def folder_button(self, layout, name, icon, action):
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon))
        button.setToolTip(name)
        button.setAccessibleName(name)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def set_folder(self, folder):
        folder = folder.resolve()
        if self.folder_index >= 0 and folder == self.folder_history[self.folder_index]:
            return
        self.folder_history[self.folder_index + 1:] = [folder]
        self.folder_index += 1
        self.show_folder()

    def show_folder(self):
        folder = self.folder_history[self.folder_index]
        self.file_tree.setRootIndex(self.file_model.setRootPath(str(folder)))
        self.file_tree.setToolTip(str(folder))
        self.folder_back.setEnabled(self.folder_index > 0)
        self.folder_forward.setEnabled(self.folder_index + 1 < len(self.folder_history))
        self.folder_up.setEnabled(folder != folder.parent)

    def navigate_folder(self, direction):
        for index in range(self.folder_index + direction,
                           len(self.folder_history) if direction > 0 else -1, direction):
            if self.folder_history[index].is_dir():
                self.folder_index = index
                self.folder = self.folder_history[index]
                self.show_folder()
                return

    def parent_folder(self):
        self.folder = self.folder_history[self.folder_index].parent
        self.set_folder(self.folder)

    def pick_folder(self):
        name = QFileDialog.getExistingDirectory(self, "Open circuit folder", self.file_model.rootPath())
        if name:
            self.folder = Path(name)
            self.set_folder(self.folder)
            self.explorer.show()

    def open_tree_file(self, index):
        if not index.isValid():
            return
        path = Path(self.file_model.filePath(index))
        if self.file_model.isDir(index):
            self.folder = path
            self.set_folder(path)
        else:
            self.open_path(path)

    def add_tab(self, path=None, data=None, source=""):
        editor = CircuitEditor()
        editor.read_imports = lambda source: self.editor_imports(editor, source)
        editor.setPlainText(source)
        state = CircuitTab(editor, path, data)
        if path is not None:
            state.wiring, state.placements = read_placement(path)
        self.documents[editor] = state
        editor.textChanged.connect(lambda: self.source_changed() if editor is self.editor else None)
        editor.document().modificationChanged.connect(lambda: self.update_tab_title(editor))
        editor.wire_hovered.connect(self.trace_editor_wire)
        editor.device_hovered.connect(self.show_device_reference)
        with QSignalBlocker(self.editor_tabs):
            self.editor_tabs.addTab(editor, path.name if path else "Untitled.ckt")
        return editor

    def store_tab(self):
        state = self.documents[self.editor]
        state.path, state.disk_bytes = self.path, self.disk_bytes
        state.placement_dirty = self.placement_dirty
        state.placements = self.schematic.placements()
        state.wiring = self.schematic.layout_mode
        state.mode = self.mode.currentIndex()

    def switch_tab(self, index):
        editor = self.editor_tabs.widget(index)
        if editor is None or editor is self.editor:
            return
        self.trace_popup.hide()
        self.device_popup.hide()
        self.store_tab()
        self.editor = editor
        state = self.documents[editor]
        self.path, self.disk_bytes = state.path, state.disk_bytes
        source = editor.toPlainText()
        self.pins, self.circuit_text = read_pins(source), circuit_text(source)
        self.load_placement(state.wiring, dict(state.placements or {}))
        self.placement_dirty = state.placement_dirty
        with QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(state.mode)
        self.update_title()
        self.update_modified()
        self.refresh_picker()
        self.compile_source()

    def close_tab(self, index):
        active = self.editor_tabs.currentIndex()
        self.editor_tabs.setCurrentIndex(index)
        if not self.confirm_edits():
            self.editor_tabs.setCurrentIndex(active)
            return
        editor = self.editor
        if self.editor_tabs.count() == 1:
            self.add_tab()
        self.editor_tabs.removeTab(index)
        del self.documents[editor]
        editor.deleteLater()

    def _create_schematic_page(self):
        """Build the diagram view, wire display controls, and pin tools."""
        page = QWidget()
        canvas = QVBoxLayout(page)
        canvas.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        controls.setContentsMargins(4, 4, 4, 0)
        settings = QSettings("Electric Circuits", "Object-Oriented Circuits")
        self.schematic = SchematicView(GateLayouts(settings))
        self.schematic.gate_layout_requested.connect(
            lambda name, inputs: self.edit_gate_layouts((name, inputs))
        )
        self.schematic.placement_changed.connect(self.placement_changed)
        self.schematic.probed.connect(self.probe)
        self.schematic.pin_requested.connect(self.name_pin)
        self.schematic.pin_renamed.connect(self.name_pin)
        self.schematic.pin_removed.connect(lambda net: self.write_pin(net, None))
        self.schematic.move_refused.connect(
            lambda: self.status.setText("Try an open spot where the existing nets can stay connected.")
        )
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
                "Start with continuous wires.",
            ),
        ):
            self.wire_style.addItem(label, mode)
            self.wire_style.setItemData(
                self.wire_style.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
            )
        self.wire_style.setAccessibleName("Schematic wiring style")
        self.wire_style.setToolTip("Choose the starting layout for connections.")
        self.wire_style.currentIndexChanged.connect(self.change_wiring)
        controls.addWidget(self.wire_style)
        button("Reset placement", self.schematic.reset_placement, controls).setToolTip(
            "Restore the automatic layout. Press Ctrl+Z in the schematic to undo a move."
        )
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
        files.addAction("Open &Folder…", self.pick_folder)
        files.addAction("Close &Tab", lambda: self.close_tab(self.editor_tabs.currentIndex()),
                        QKeySequence.StandardKey.Close)
        files.addAction("&Save", self.save, QKeySequence.StandardKey.Save)
        files.addAction("Save &As…", self.save_as, QKeySequence.StandardKey.SaveAs)
        files.addAction("&Reload from disk", self.reload_file)
        files.addSeparator()
        files.addAction("Export to &SPICE...", self.export_netlist)
        files.addAction("Export LTspice &Schematic...", self.export_ltspice)
        files.addAction("Export &Verilog…", self.export_verilog)
        files.addSeparator()
        files.addAction("&Quit", self.close, QKeySequence.StandardKey.Quit)
        view = self.menuBar().addMenu("&View")
        view.addAction(self.explorer.toggleViewAction())
        view.addAction("Chip Library", self.show_chip_library)
        view.addAction("Gate Layouts…", lambda: self.edit_gate_layouts())
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
        if mode != "auto":
            return mode
        if self.solution is not None:
            return self.solution.circuit.mode
        return analysis_mode(self.editor.toPlainText())

    def read_linked_source(self, path):
        for editor, state in self.documents.items():
            if (self.path if editor is self.editor else state.path) == path:
                return editor.toPlainText()
        return path.read_text(encoding="utf-8-sig")

    def show_chip_library(self):
        self.folder = CHIP_LIBRARY
        self.set_folder(self.folder)
        self.explorer.show()

    def edit_gate_layouts(self, initial=None):
        dialog = GateLayoutDialog(self, self.schematic.gate_layouts, initial)
        dialog.exec()
        dialog.deleteLater()

    def editor_imports(self, editor, source):
        path = self.path if editor is self.editor else self.documents[editor].path
        try:
            files, imports = read_includes(source, path, self.read_linked_source)
        except CircuitError:
            return ()
        return imported_definitions(next(iter(files)), files, imports).values()

    def library_preview(self, source):
        if self.path is None or not self.path.is_relative_to(CHIP_LIBRARY):
            return None
        if any(MAIN.fullmatch(row) for row, _, _ in source_rows(source)):
            return None
        signatures = self.editor.method_signatures()
        stem = self.path.stem.casefold()
        signature = signatures.get(stem) or signatures.get("sn" + stem)
        if signature and signature[0] == "chip":
            return f"\nmain {{ {signature[1]}({signature[2]}); }}\n"
        return None

    def build_circuit(self, source, mode, *, render_only=False):
        return compile_circuit(
            source, mode, render_only=render_only,
            path=self.path, read_source=self.read_linked_source,
        )

    @staticmethod
    def includes_files(source):
        return any(INCLUDE.fullmatch(text) for text, _, _ in source_rows(source))

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
                names = re.findall(r"(?:^|[;{}])[ \t]*([A-Za-z]\w*)[ \t(]", source, re.M)
                names = {name.casefold() for name in names}
                count = 1
                while f"{key}{count}".casefold() in names:
                    count += 1
                entry = f"{key}{count}{entry}"
        cursor = self.editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(
            ("\n" if source and not source.endswith("\n") else "") + entry
        )
        self.editor.setTextCursor(cursor)
        self.picker.setCurrentIndex(0)
        self.editor.setFocus()

    def show_issues(self, issues):
        self.editor.annotate([issue for issue in issues if issue.path in (None, self.path)])
        self.issues.clear()
        if not issues:
            item = QListWidgetItem("No compiler diagnostics.")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.issues.addItem(item)
        for issue in issues:
            location = f"{issue.path.name} · " if issue.path and issue.path != self.path else ""
            label = f"Line {issue.line}" if issue.line is not None else "Simulation"
            item = QListWidgetItem(f"{location}{label} · {issue.severity} · {issue.message}")
            item.setForeground(
                QColor("#f87171" if issue.severity == "error" else "#ffd18b")
            )
            item.setData(Qt.ItemDataRole.UserRole, issue.line)
            item.setData(Qt.ItemDataRole.UserRole + 1, str(issue.path) if issue.path else None)
            item.setToolTip(issue.message)
            self.issues.addItem(item)

    def select_diagnostic(self, item):
        path = item.data(Qt.ItemDataRole.UserRole + 1)
        if path and not self.open_path(path):
            return
        if line := item.data(Qt.ItemDataRole.UserRole):
            self.editor.go_to_line(line)

    def set_solution(self, result):
        self.solution = result
        rows = result_rows(result) if result else []
        rows = pin_rows(result, self.pins) + rows
        self.results.update_rows(rows)
        self.analysis_results.update_rows(rows)

    def source_changed(self):
        self.trace_popup.hide()
        self.device_popup.hide()
        source = self.editor.toPlainText()
        previous_pins, self.pins = self.pins, read_pins(source)
        code = circuit_text(source)
        # Pin metadata is a comment. Renaming a pin does not invalidate a solve.
        if self.pins != previous_pins and code == self.circuit_text:
            self.schematic.update_pins(self.pins)
            self.set_solution(self.solution)
            return
        self.circuit_text = code
        self.scope.set_circuit(None)
        self.set_solution(None)
        if self.effective_mode() != self.picker_mode:
            self.refresh_picker()
        self.schematic.clear(preserve_placement=True)
        self.pin_button.setChecked(False)
        self.schematic.set_pin_tool(False)
        self.show_issues([])
        self.status.setText(
            "Compile to refresh the schematic."
            if has_chip(source)
            else "Compile to refresh the schematic and results."
        )

    def mode_changed(self):
        self.refresh_picker()
        self.compile_source()

    def compile_source(self):
        source = self.editor.toPlainText()
        self.scope.set_circuit(None)
        self.set_solution(None)
        self.schematic.clear(preserve_placement=True)
        issues = []
        if not any(row.partition("#")[0].strip() for row in source.splitlines()):
            self.status.setText("Open a circuit or enter known quantities.")
        elif self.library_preview(source) or (has_chip(source) and not self.includes_files(source)):
            try:
                self._draw_source(source, "dc")
            except CircuitError as error:
                issues = [self.issue(error)]
                self.status.setText(issues[0].message)
            else:
                self.views.setCurrentIndex(0)
                self.status.setText("Schematic rendered.")
        else:
            mode = self.mode.currentData()
            try:
                result = analyze(self.build_circuit(source, mode))
                self.set_solution(result)
                if self.effective_mode() != self.picker_mode:
                    self.refresh_picker()
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
        if has_chip(source) and not self.includes_files(source):
            chips = compile_chips(source)
            circuit = Circuit(
                "dc",
                instances=[
                    MethodInstance(
                        "chip", name, None,
                        tuple(
                            (pin, pin if len(chips) == 1 else f"@{name}.{pin}")
                            for pin, _ in pins
                        ),
                        body,
                    )
                    for name, pins, body, _ in chips
                ],
            )
        else:
            preview = self.library_preview(source)
            if preview:
                source += preview
            circuit = self.build_circuit(source, mode, render_only=True)
        self.schematic.draw(circuit, self.pins)
        self.scope.set_circuit(circuit)

    def render_source(self):
        try:
            self._draw_source(self.editor.toPlainText(), self.mode.currentData())
        except CircuitError as error:
            self.schematic.clear(preserve_placement=True)
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
            self.schematic.redraw(self.pins)
            if selected:
                self.schematic.highlight_net(selected)
        if self.schematic.placements():
            self.placement_changed()

    def trace_editor_wire(self, location, position):
        circuit = self.schematic.circuit
        if circuit is None or location is None or position is None:
            self.trace_popup.hide()
            return
        names, nets = wire_trace(circuit, location, self.path)
        if not names:
            self.trace_popup.hide()
            return
        circuit = replace(
            circuit, components=[part for part in circuit.components if part.name in names],
            groups=[], instances=[], owners={},
            wire_taps=[tap for tap in circuit.wire_taps if tap.component in names],
        )
        self.trace_view.clear()
        self.trace_view.circuit = circuit
        self.trace_view.trace_nets = nets
        self.trace_view.level_layouts[None, "auto"] = fast_layout(circuit, component_labels(circuit))
        self.trace_view.redraw({})
        self.show_hover_popup(self.trace_popup, position)
        self.trace_view.fit()

    def show_device_reference(self, reference, position, help_text):
        if reference is None or position is None:
            self.device_popup.hide()
            return
        self.device_reference.reference = reference
        kind, _, pins = reference
        self.device_reference.setFixedHeight(160 if kind == "builtin" else min(640, 65 + 19 * ((len(pins) + 1) // 2)))
        self.device_reference.update()
        self.device_help.setText(help_text)
        self.device_help.setVisible(bool(help_text))
        self.device_popup.adjustSize()
        self.show_hover_popup(self.device_popup, position)

    def show_hover_popup(self, popup, position):
        screen = QApplication.screenAt(position) or self.screen()
        bounds = screen.availableGeometry()
        x = position.x() + 16
        if x + popup.width() > bounds.right():
            x = position.x() - popup.width() - 16
        y = max(bounds.top(), position.y() - popup.height() - 12)
        popup.move(QPoint(max(bounds.left(), x), y))
        popup.show()

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
        if error.code == "NonlinearConvergence":
            return Diagnostic(
                "error", None, error.message + " Try a transient in Results or export to SPICE.",
                error.path,
            )
        return Diagnostic("error", error.line or 1, error.message, error.path)

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

    def update_modified(self):
        self.update_tab_title(self.editor)

    def update_tab_title(self, editor):
        if editor not in self.documents:
            return
        state = self.documents[editor]
        path, layout_dirty = (self.path, self.placement_dirty) if editor is self.editor else (
            state.path, state.placement_dirty,
        )
        dirty = editor.document().isModified() or layout_dirty
        index = self.editor_tabs.indexOf(editor)
        self.editor_tabs.setTabText(index, (path.name if path else "Untitled.ckt") + (" *" if dirty else ""))
        self.editor_tabs.setTabToolTip(index, str(path) if path else "Unsaved circuit")
        if editor is self.editor:
            self.setWindowModified(dirty)

    def placement_changed(self):
        self.placement_dirty = True
        self.update_modified()

    def load_placement(self, mode, records):
        self.schematic.clear()
        index = self.wire_style.findData(mode if isinstance(mode, str) else "auto")
        self.wire_style.setCurrentIndex(max(0, index))
        self.schematic.layout_mode = self.wire_style.currentData()
        self.schematic.placement_records = records
        self.placement_dirty = False
        self.update_modified()

    def new_file(self):
        if self.path or self.editor.toPlainText() or self.editor.document().isModified() or self.placement_dirty:
            self.editor_tabs.setCurrentWidget(self.add_tab())
        self.editor.setFocus()

    def pick_file(self):
        name, _ = QFileDialog.getOpenFileName(
            self, "Open circuit", self.file_model.rootPath(), "Circuit files (*.ckt *.txt);;All files (*)"
        )
        if name:
            self.open_path(name)

    def open_path(self, path, *, reload=False):
        path = Path(path).resolve()
        if not reload:
            for state in self.documents.values():
                existing = self.path if state.editor is self.editor else state.path
                if existing == path:
                    self.editor_tabs.setCurrentWidget(state.editor)
                    return True
        try:
            data = path.read_bytes()
            source = data.decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            QMessageBox.warning(self, "Open circuit", str(error))
            return False
        if reload or (
            self.path is None and not self.editor.toPlainText()
            and not self.editor.document().isModified() and not self.placement_dirty
        ):
            self.path, self.disk_bytes = path, data
            self.editor.setPlainText(source)
            self.load_placement(*read_placement(path))
            self.editor.document().setModified(False)
            self.update_title()
            self.compile_source()
        else:
            self.editor_tabs.setCurrentWidget(self.add_tab(path, data, source))
        self.update_modified()
        if self.folder is None:
            self.set_folder(path.parent)
        self.editor.setFocus()
        return True

    def reload_file(self):
        if self.path and self.confirm_edits():
            self.open_path(self.path, reload=True)

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
            lambda: export_spice(
                self.editor.toPlainText(), self.mode.currentData(),
                path=self.path, read_source=self.read_linked_source,
            ),
            "Export to SPICE",
            ".cir",
            "SPICE netlists (*.cir)",
        )

    def export_verilog(self):
        return self._export_file(
            lambda: export_verilog(self.editor.toPlainText()),
            "Export Verilog",
            ".v",
            "Verilog source (*.v)",
            next_step="Add it to your FPGA project.",
        )

    def export_ltspice(self):
        used_labels = False
        block_files = {}

        def schematic_text():
            nonlocal used_labels, block_files
            source = self.editor.toPlainText()
            if has_chip(source):
                raise CircuitError("Use File > Export Verilog for this chip.")
            if self.schematic.layout is None or self.includes_files(source):
                self._draw_source(source, self.mode.currentData())
                self.views.setCurrentIndex(0)
            circuit = self.schematic.circuit
            drawing = self.schematic.layout
            if drawing is None:
                raise CircuitError("Enter components before exporting an LTspice schematic.")
            if drawing.uses_packages:
                mode = self.schematic.layout_mode
                details = {
                    name: layout for (name, style), layout in self.schematic.manual_layouts.items()
                    if name is not None and style == mode
                }
                text, block_files, used_labels = export_hierarchy(circuit, drawing, details)
            else:
                text, used_labels = export_schematic(circuit, drawing)
            return text

        exported = self._export_file(
            schematic_text,
            "Export LTspice Schematic",
            ".asc",
            "LTspice schematics (*.asc)",
            companions=lambda: block_files,
        )
        if exported:
            notes = []
            if block_files:
                notes.append("Keep the exported block sheets and symbols beside the main schematic.")
            if used_labels:
                notes.append("Matching net labels connect the crowded wires.")
            if notes:
                self.status.setText(" ".join((self.status.text(), *notes)))
        return exported

    def _export_file(
        self,
        generate,
        title,
        suffix,
        file_filter,
        *,
        next_step="Open it in LTspice and click Run.",
        companions=None,
    ):
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
        if companions is not None and not self.write_export_companions(path, companions(), title):
            return False
        if not write_bytes(self, path, data, title):
            return False
        self.status.setText(f"Exported {path.name}. {next_step}")
        return True

    def write_export_companions(self, path, files, title):
        pending = []
        for name, text in files.items():
            target = path.with_name(name)
            data = text.encode("ascii")
            try:
                existing = target.read_bytes()
            except FileNotFoundError:
                pending.append((target, data))
                continue
            except OSError as error:
                QMessageBox.warning(self, title, str(error))
                return False
            if existing != data:
                QMessageBox.warning(self, title, f"An existing block file uses the name {name}. Choose another folder.")
                return False
        if any(target == path for target in (path.with_name(name) for name in files)):
            QMessageBox.warning(self, title, "Choose a different name for the top-level schematic.")
            return False
        return all(write_bytes(self, target, data, title) for target, data in pending)

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
        path = Path(path).resolve()
        if any(state.editor is not self.editor and state.path == path for state in self.documents.values()):
            QMessageBox.warning(self, "Save circuit", "This file is already open in another tab.")
            return False
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
        placements = self.schematic.placements()
        sidecar = placement_path(path)
        if placements or sidecar.exists():
            layout_data = write_placement(self.schematic.layout_mode, placements)
            if not write_bytes(self, sidecar, layout_data, "Save schematic layout"):
                return False
        self.placement_dirty = False
        self.update_modified()
        if self.folder is None:
            self.set_folder(path.parent)
        self.status.setText(f"Saved {path.name}.")
        return True

    def confirm_edits(self):
        if not self.editor.document().isModified() and not self.placement_dirty:
            return True
        options = QMessageBox.StandardButton
        choice = QMessageBox.question(
            self,
            "Save changes",
            f"Save changes to {self.path.name if self.path else 'Untitled.ckt'}?",
            options.Save | options.Discard | options.Cancel,
            options.Save,
        )
        return choice == options.Discard or (choice == options.Save and self.save())

    def closeEvent(self, event):
        for editor, state in self.documents.items():
            if editor.document().isModified() or (
                self.placement_dirty if editor is self.editor else state.placement_dirty
            ):
                self.editor_tabs.setCurrentWidget(editor)
                if not self.confirm_edits():
                    event.ignore()
                    return
        self.scope.shutdown()
        event.accept()


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
