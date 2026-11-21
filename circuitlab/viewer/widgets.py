"""Shared viewer panels and dialogs, independent of the optional source editor."""
# Shared widgets format and present existing model data for both window shells.
# File writes and shared-default publishing use explicit helpers so a failed save does not
# leave a partially written replacement or silently change another circuit's source.

from PySide6.QtCore import QIODevice, QSaveFile, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMenu, QMessageBox, QPushButton, QSpinBox, QStyle, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from circuitlab.language.gates import GATE_HELP, gate_accepts
from circuitlab.layout.gates import BUNDLED_LAYOUTS, default_gate_layout, gate_preview
from circuitlab.math.numbers import format_value
from circuitlab.viewer.schematic import SchematicView



MODES = {
    "auto": "Auto",
    "dc": "DC steady state",
    "ac": "AC steady state",
    "mixed": "DC + AC",
    "transient": "Transient",
}


STYLESHEET = """
QMainWindow, QDialog, QWidget { background: #282828; color: #f2f2f2; }
QPlainTextEdit, QLineEdit, QListWidget { background: #191919; }
QPushButton, QComboBox { background: #393939; border: 1px solid #666; border-radius: 4px;
                        padding: 6px 12px; }
QPushButton:hover, QComboBox:hover { background: #4b4b4b; }
QPushButton:checked { background: #31556b; border-color: #72a6c5; }
QPushButton#compile { background: #a92537; font-weight: bold; }
QPushButton#compile:hover { background: #c33449; }
QTabBar::tab { background: #343434; padding: 7px 16px; border: 1px solid #555; }
QTabBar::tab:selected { background: #191919; }
QToolButton#scopeToggle { background: #343434; padding: 7px 16px; border: 1px solid #555; }
QToolButton#scopeToggle:hover { background: #4b4b4b; }
QToolButton#scopeToggle:checked { background: #31556b; border-color: #72a6c5; }
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


def grid_button(view, layout, *, alignment_layout=None):
    control = button("Grid", view.set_grid_enabled, layout)
    control.setCheckable(True)
    control.setChecked(view.grid_enabled)
    control.setToolTip("Show or hide the grid. Components and wire handles always snap; Shift uses diagonal leads.")
    def align():
        if not view.align_to_grid():
            QMessageBox.information(view, "Align to grid",
                "This layout is too tightly spaced to align without joining different wires. "
                "Move the crowded parts or wires apart and try again. Your layout has not changed.")
    align_control = button("Align to grid", align, alignment_layout if alignment_layout is not None else layout)
    align_control.setToolTip("Align existing wires, pins, grounds and parts together. Ctrl+Z undoes the whole change.")
    return control


class SchematicHeader(QWidget):
    """Fill the tab row with schematic tools and the persistent scope toggle."""

    def __init__(self, tabs, scope_toggle):
        super().__init__(tabs)
        self.tabs = tabs
        self.scope_toggle = scope_toggle
        self.tools = QWidget()
        self.tools.setStyleSheet("""
            QPushButton { padding: 3px 6px; }
            QComboBox { padding-top: 3px; padding-bottom: 3px; }
            QToolButton { background: #393939; border: 1px solid #666; border-radius: 4px;
                          padding: 3px 14px 3px 6px; }
            QToolButton:hover { background: #4b4b4b; }
        """)
        self.primary = QHBoxLayout()
        self.secondary = QHBoxLayout()
        tools = QHBoxLayout(self.tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(3)
        tools.addLayout(self.primary)
        tools.addLayout(self.secondary)
        self.more = QToolButton()
        self.more.setText("More")
        self.more.setToolTip("More schematic tools")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more.setMenu(QMenu(self.more))
        tools.addWidget(self.more)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.tools)
        row.addStretch()
        row.addWidget(scope_toggle)
        tabs.setCornerWidget(self, Qt.Corner.TopRightCorner)
        tabs.currentChanged.connect(self.show_tools)
        self.show_tools(tabs.currentIndex())

    def minimumSizeHint(self):
        # Keep the editor/preview split stable as the user switches between pages.
        width = self.scope_toggle.sizeHint().width() + self.primary.sizeHint().width()
        width += self.more.sizeHint().width() + self.layout().spacing() + self.tools.layout().spacing()
        return QSize(width, self.scope_toggle.sizeHint().height())

    def sizeHint(self):
        hint = self.minimumSizeHint()
        # QTabWidget sizes its corner from the widget's hint. Reserve the tab bar
        # first and use the rest of the row for the toolbar and right-side toggle.
        available = self.tabs.width() - self.tabs.tabBar().sizeHint().width() - self.layout().spacing()
        hint.setWidth(max(hint.width(), available))
        return hint

    def show_tools(self, index):
        self.tools.setVisible(index == 0)
        if index == 0:
            self._fit_tools()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.tools.isHidden():
            self._fit_tools()

    def _fit_tools(self):
        spacing = self.tools.layout().spacing()
        widths = []
        for index in range(self.secondary.count()):
            control = self.secondary.itemAt(index).widget()
            widths.append((control, control.sizeHint().width() + spacing))
        available = self.width() - self.scope_toggle.sizeHint().width() - self.layout().spacing()
        available -= self.primary.sizeHint().width()
        menu = self.more.menu()
        menu.clear()
        if sum(width for _, width in widths) > available:
            available -= self.more.sizeHint().width() + spacing
        for control, width in widths:
            visible = width <= available
            control.setVisible(visible)
            if visible:
                available -= width
            else:
                menu.addAction(control.text(), control.click)
        self.more.setVisible(not menu.isEmpty())


def search_text(value):
    return " ".join(value.casefold().replace("_", " ").split())


# Report the selected model identity from each available operating point, rather than
# deriving values from scene coordinates or a stale display caption.
def probe_text(result, kind, name, *, separator=" · "):
    """Describe a selected node or component in each solved analysis mode."""
    if not result or not result.network:
        return "Compile to get probe readings."
    readings = []
    for network, ac in ((result.dc_network, False), (result.network, result.circuit.mode != "dc")):
        if not network:
            continue
        prefix = "AC RMS" + separator if ac else "DC" + separator if result.dc_network else ""
        if kind == "node" and name in network.nodes:
            value = network.voltage[network.nodes.index(name)]
            reference = network.references.get(name, "0")
            readings.append(prefix + f"V({name}) relative to {reference} = " + format_value(value, "V", ac, False))
        elif kind == "component" and name in network.names:
            index = network.names.index(name)
            voltage = format_value(network.branch_voltage[index], "V", ac, False)
            part = next((part for part in result.circuit.components if part.name == name), None)
            if part and part.kind == "P":
                currents = "   |   ".join(
                    f"current into {terminal} = {format_value(amps, 'A', ac, False)}"
                    for terminal, amps in zip(("high", "wiper", "low"), network.terminal_current[name])
                )
                readings.append(prefix + f"{name} high-low voltage = {voltage}   |   {currents}")
            else:
                current = format_value(network.branch_current[index], "A", ac, False)
                readings.append(prefix + f"{name} voltage = {voltage}   |   current = {current}")
    return "\n".join(readings) or "No result for this selection."


def publish_defaults(parent, layouts):
    path, _ = QFileDialog.getSaveFileName(parent, "Save shared defaults to project", str(BUNDLED_LAYOUTS), "JSON (*.json)")
    if not path:
        return
    try:
        count = layouts.publish(path)
        QMessageBox.information(parent, "Shared defaults", f"Saved {count} defaults. Include this file in the project's libraries/layouts/defaults.json when distributing OOC.")
    except (OSError, ValueError, TypeError) as error:
        QMessageBox.warning(parent, "Shared defaults", str(error))


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
        grid_button(self.preview, controls)
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
        button("Save defaults to project", lambda: publish_defaults(self, self.layouts), footer)
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


class DiagnosticsList(QListWidget):
    """Keep the status to one line and leave room to scroll compiler diagnostics."""

    def fit_rows(self, *, status=False):
        self.ensurePolished()
        margins = self.contentsMargins()
        height = margins.top() + margins.bottom()
        if not status:
            # A long diagnostic can add a horizontal scrollbar when the window narrows.
            scrollbar = self.horizontalScrollBar()
            height += scrollbar.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, widget=scrollbar)
        for row in range(self.count()):
            height += self.sizeHintForRow(row)
            if height >= 100:
                break
        self.setFixedHeight(min(height, 100))


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


# Write a complete temporary file before replacing the destination so a failed save
# cannot truncate the previously usable file.
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

