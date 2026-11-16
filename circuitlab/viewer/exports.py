"""File dialogs and safe companion-file writes shared by viewer shells."""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from circuitlab.model.graph import CircuitError
from circuitlab.layout.hierarchy import METHOD_PREFIX, hood_levels
from circuitlab.layout.persistence import restore_layout
from circuitlab.tooling.session import issue_for
from circuitlab.viewer.widgets import write_bytes


def hierarchy_layouts(schematic):
    """Recover edited child geometry even while its package is collapsed.

    Redrawing moves expanded manual layouts into serialized placement records.
    Resolve those records against the current graph instead of expanding the UI
    or silently replacing a saved child sheet with automatic routing.
    """
    circuit = schematic.circuit
    if circuit is None:
        return {}
    expanded = {f"{METHOD_PREFIX}{index}" for index in range(len(circuit.instances))}
    details = {}
    for name, view, _, boundary in hood_levels(circuit, expanded):
        if name is None:
            continue
        key = name, schematic.layout_mode
        layout = schematic.manual_layouts.get(key)
        if layout is None and key in schematic.placement_records:
            layout = restore_layout(schematic.placement_records[key], view, boundary)
        if layout is None and schematic.gate_layouts is not None:
            instance = circuit.instances[int(name.removeprefix(METHOD_PREFIX))]
            layout = schematic.gate_layouts.for_instance(
                instance, view, boundary, use_default=schematic.layout_mode != "labeled",
            )
        if layout is not None:
            details[name] = layout
    return details


def write_companions(parent, path, files, title):
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
            QMessageBox.warning(parent, title, str(error))
            return False
        if existing != data:
            QMessageBox.warning(parent, title, f"An existing block file uses the name {name}. Choose another folder.")
            return False
    if any(target == path for target in (path.with_name(name) for name in files)):
        QMessageBox.warning(parent, title, "Choose a different name for the top-level schematic.")
        return False
    return all(write_bytes(parent, target, data, title) for target, data in pending)


def export_file(parent, generate, title, suffix, file_filter, *,
                next_step="Open it in LTspice and click Run.", companions=None):
    """Serialize first, then save without overwriting differing shared blocks."""
    try:
        data = generate().encode("ascii")
    except CircuitError as error:
        parent.status.setText(str(error))
        parent.show_issues([issue_for(error)])
        QMessageBox.warning(parent, title, str(error))
        return False
    suggested = (parent.path or Path("Untitled.ckt")).with_suffix(suffix)
    name, _ = QFileDialog.getSaveFileName(parent, title, str(suggested), file_filter)
    if not name:
        return False
    path = Path(name)
    if path.suffix.lower() != suffix:
        path = path.with_name(path.name + suffix)
        if path.exists() and QMessageBox.question(
            parent, title, f"Replace {path.name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return False
    if companions is not None and not write_companions(parent, path, companions(), title):
        return False
    if not write_bytes(parent, path, data, title):
        return False
    parent.status.setText(f"Exported {path.name}. {next_step}")
    return True
