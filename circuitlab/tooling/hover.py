"""Render editor hover references as PNGs over a private JSON-lines pipe."""

import base64
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter, QTextCursor
from PySide6.QtWidgets import QApplication

from circuitlab.language.compiler import compile_circuit
from circuitlab.viewer.device_reference import DeviceReference
from circuitlab.viewer.editor import CircuitEditor
from circuitlab.model.graph import CircuitError
from circuitlab.language.linked import imported_definitions, read_includes
from circuitlab.math.report import component_labels
from circuitlab.layout.routing import fast_layout
from circuitlab.viewer.schematic import SchematicView
from circuitlab.tooling.trace import wire_trace


def png(image):
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return base64.b64encode(bytes(buffer.data())).decode("ascii")


class HoverRenderer:
    def __init__(self):
        self.editor = CircuitEditor()
        self.device = DeviceReference()
        self.view = SchematicView()
        self.view.setInteractive(False)

    def preview(self, request):
        source = request["source"]
        path = Path(request["file"]).resolve()
        if not isinstance(source, str) or len(source) > 2 * 1024 * 1024:
            return None
        if self.editor.toPlainText() != source:
            self.editor.setPlainText(source)

        def imports(text):
            try:
                files, dependencies = read_includes(text, path)
                return imported_definitions(next(iter(files)), files, dependencies).values()
            except CircuitError:
                return ()

        self.editor.read_imports = imports
        block = self.editor.document().findBlockByNumber(int(request["line"]))
        if not block.isValid():
            return None
        cursor = QTextCursor(block)
        column = int(request["column"])
        if column < 0 or column >= block.length():
            return None
        cursor.setPosition(block.position() + column)
        if column >= block.text().find("#") >= 0:
            return None
        reference = self.editor.device_at(cursor)
        help_text = self.editor.hover_help(cursor)
        if reference:
            kind, name, pins = reference
            self.device.reference = reference
            height = 180 if kind == "builtin" else max(180, 65 + 20 * ((len(pins) + 1) // 2))
            self.device.resize(340, height)
            # QWidget.grab renders without showing a native window.
            return {"text": help_text or name, "image": png(self.device.grab())}
        location = self.editor.wire_at(cursor)
        if not location:
            return {"text": help_text} if help_text else None
        _, name, terminal, argument = location
        if name.lower().startswith("trans") and terminal >= 3:
            return None
        text = f"Wire: {argument}\nConnection {terminal + 1} on {name}"
        try:
            circuit = compile_circuit(source, render_only=True, path=path)
            if len(circuit.components) > 2000:
                return {"text": text + "\nThis circuit is too large for a quick hover trace."}
            names, nets = wire_trace(circuit, location, path)
        except CircuitError as error:
            return {"text": text + f"\nPreview unavailable: {error.message}"}
        if not names:
            return {"text": text + "\nNo instantiated electrical path found in this file."}
        text += "\nHighlighted net: " + ", ".join(sorted(nets))
        text += "\nRepresentative connected path (not a simulation)."
        if len(names) > 24:
            return {"text": text + f"\n{len(names)} connected components; open the schematic to inspect this path."}
        traced = replace(
            circuit, components=[part for part in circuit.components if part.name in names],
            groups=[], instances=[], owners={},
            wire_taps=[tap for tap in circuit.wire_taps if tap.component in names],
        )
        self.view.clear()
        self.view.circuit = traced
        self.view.trace_nets = nets
        self.view.level_layouts[None, "auto"] = fast_layout(traced, component_labels(traced))
        self.view.redraw({})
        image = QImage(420, 420, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.scene().render(painter, QRectF(12, 12, 396, 396), self.view.scene().itemsBoundingRect())
        painter.end()
        return {"text": text, "image": png(image), "nets": sorted(nets), "components": sorted(names)}


def main():
    app = QApplication([])
    # Offscreen Qt may not discover Windows fonts in an isolated process.
    if sys.platform == "win32":
        fontdir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "arial.ttf", "consola.ttf"):
            if (fontdir / name).is_file():
                QFontDatabase.addApplicationFont(str(fontdir / name))
        app.setFont(QFont("Segoe UI", 10))
    renderer = HoverRenderer()
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            result = renderer.preview(request)
        except (CircuitError, ValueError, TypeError, KeyError, OSError) as error:
            result = {"text": f"OOC preview unavailable: {error}"}
        print(json.dumps({"id": request.get("id"), "result": result}), flush=True)
        app.processEvents()


if __name__ == "__main__":
    main()
