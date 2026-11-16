"""Browse library declarations and edit their reusable schematic geometry."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from circuitlab.model.graph import CircuitError
from circuitlab.viewer.schematic import SchematicView
from circuitlab.viewer.widgets import GateLayoutDialog, button, grid_button, publish_defaults


class DeviceLayoutDialog(QDialog):
    """One editable level per device; nested devices have their own defaults."""

    def __init__(self, parent, layouts, initial=None):
        super().__init__(parent)
        self.setWindowTitle("Chip & Circuit Layouts")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint)
        self.setSizeGripEnabled(True)
        available = parent.screen().availableGeometry()
        self.resize(min(1150, available.width() - 48), min(800, available.height() - 64))
        self.layouts = layouts
        layouts.reload_library()
        self.drafts = {}
        self.key = None
        panel = QVBoxLayout(self)
        splitter = QSplitter()
        panel.addWidget(splitter, 1)
        browser = QWidget()
        browse = QVBoxLayout(browser)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a chip or circuit…")
        browse.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Library")
        browse.addWidget(self.tree, 1)
        splitter.addWidget(browser)
        editor = QWidget()
        content = QVBoxLayout(editor)
        self.title = QLabel()
        content.addWidget(self.title)
        self.call = QPlainTextEdit()
        self.call.setReadOnly(True)
        self.call.setToolTip("Include/call template. Replace the signal and supply pin names with your circuit's wires.")
        self.call.setMaximumHeight(95)
        content.addWidget(self.call)
        controls = QHBoxLayout()
        self.preview = SchematicView(layouts)
        self.grid_controls = QWidget()
        grid_controls = QHBoxLayout(self.grid_controls)
        grid_controls.setContentsMargins(0, 0, 0, 0)
        grid_button(self.preview, grid_controls)
        controls.addWidget(self.grid_controls)
        button("Fit", self.preview.fit, controls)
        button("Copy call", lambda: QApplication.clipboard().setText(self.call.toPlainText()), controls)
        controls.addStretch()
        content.addLayout(controls)
        content.addWidget(self.preview, 1)
        splitter.addWidget(editor)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 900])
        actions = QHBoxLayout()
        self.automatic = button("Restore automatic", self.restore_automatic, actions)
        self.saved_button = button("Use saved layout", self.restore_saved, actions)
        actions.addStretch()
        self.apply_button = button("Apply to all instances", self.apply_layout, actions)
        button("Close", self.reject, actions)
        panel.addLayout(actions)
        self.message = QLabel()
        self.message.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.message, 1)
        button("Save defaults to project", lambda: publish_defaults(self, self.layouts), footer)
        panel.addLayout(footer)

        self.items = {}
        categories = {}
        for device in layouts.devices.values():
            if device.category not in categories:
                categories[device.category] = QTreeWidgetItem(self.tree, [device.category.title()])
            item = QTreeWidgetItem(categories[device.category], [device.name])
            item.setData(0, Qt.ItemDataRole.UserRole, device.key)
            item.setToolTip(0, device.file)
            self.items[device.key] = item
        self.tree.expandAll()
        self.search.textChanged.connect(self.filter_devices)
        self.tree.currentItemChanged.connect(self.select_item)
        self.preview.placement_changed.connect(self.record_draft)
        self.preview.move_refused.connect(lambda: self.message.setText(
            "Try an open spot where the existing nets can stay connected."
        ))
        self.preview.package_edit_requested.connect(self.edit_nested)
        self.preview.device_layout_requested.connect(self.select_device)
        self.preview.gate_layout_requested.connect(self.edit_gate)
        self.select_device(initial or next(iter(self.items)))

    def filter_devices(self, text):
        query = text.casefold().strip()
        for key, item in self.items.items():
            device = self.layouts.devices[key]
            item.setHidden(query not in f"{device.name} {device.category}".casefold())
        for index in range(self.tree.topLevelItemCount()):
            category = self.tree.topLevelItem(index)
            category.setHidden(all(category.child(i).isHidden() for i in range(category.childCount())))

    def select_device(self, key):
        item = self.items.get(key.casefold())
        if item is not None:
            self.search.clear()
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def select_item(self, item, previous=None):
        if item is None or not (key := item.data(0, Qt.ItemDataRole.UserRole)):
            return
        self.key = key
        device = self.layouts.devices[key]
        self.title.setText(f"{device.name} · {device.kind} · {len(device.pins)} pins")
        short_file = device.file.rsplit("/", 1)[-1].removesuffix(".ckt")
        self.call.setPlainText(f'include("{short_file}");\nmain {{\n    {device.name}({", ".join(device.arguments)});\n}}')
        try:
            layout = self.drafts[key] if key in self.drafts else self.layouts.get(key)
            self.draw_device(layout)
        except (CircuitError, OSError, ValueError) as error:
            self.preview.clear()
            self.set_editable(False)
            self.message.setText(f"Could not preview {device.name}: {error}")

    def set_editable(self, editable):
        self.preview.setEnabled(editable)
        self.grid_controls.setEnabled(editable)
        self.automatic.setEnabled(editable)
        self.apply_button.setEnabled(editable)
        self.saved_button.setEnabled(editable and self.layouts.has_saved(self.key))

    def draw_device(self, layout):
        circuit, root, _, boundary = self.layouts.preview(self.key)
        self.preview.clear()
        self.preview.draw(circuit, boundary=boundary, layout=layout, detail_root=root)
        self.set_editable(root is not None)
        if root is None:
            self.message.setText("Pinout only: this chip has no modeled internals to arrange yet.")
        else:
            self.message.setText(
                "Drag parts, wires, pin ends and labels. Click a nested block to edit its own default. "
                "Apply saves this device only; layouts saved in individual circuits take priority."
            )

    def edit_nested(self, index):
        instance = self.preview.circuit.instances[index]
        if instance.builtin_gate:
            self.edit_gate(instance.name, len(instance.pins) - 3)
        elif instance.name.casefold() in self.items:
            self.select_device(instance.name)
        else:
            self.message.setText(f"{instance.name} is not a separately declared library device.")

    def edit_gate(self, name, count):
        dialog = GateLayoutDialog(self.parent(), self.layouts, (name, count))
        dialog.exec()
        dialog.deleteLater()

    def record_draft(self):
        self.drafts[self.key] = self.preview.layout
        self.message.setText(f"{self.layouts.devices[self.key].name} layout edited. Apply to save its default.")

    def restore_automatic(self):
        self.drafts[self.key] = None
        self.draw_device(None)
        self.message.setText("Apply to use automatic placement for this device. Its saved design is kept as a backup.")

    def restore_saved(self):
        layout = self.layouts.saved(self.key)
        if layout is None:
            self.message.setText("The saved layout no longer matches this device's wiring.")
            return
        self.drafts[self.key] = layout
        self.draw_device(layout)
        self.message.setText("Apply to use the saved design again.")

    def apply_layout(self):
        if self.key not in self.drafts:
            self.message.setText("There are no changes to apply for this device.")
            return
        layout = self.drafts[self.key]
        if not self.layouts.set(self.key, layout):
            self.message.setText("Could not save the default layout to your app settings.")
            return
        del self.drafts[self.key]
        self.saved_button.setEnabled(self.layouts.has_saved(self.key))
        self.parent().schematic.refresh_gate_layouts()
        self.message.setText(f"Saved the default layout for {self.layouts.devices[self.key].name}.")

    def reject(self):
        if self.drafts and QMessageBox.question(
            self, "Unapplied layouts", "Discard unapplied layout edits? Applied defaults are already saved.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Discard:
            return
        super().reject()
