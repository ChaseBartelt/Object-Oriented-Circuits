"""A source document and its compiled views, without any editor or Qt objects.

Source remains authoritative for electrical connections. A session can build a
render graph even when its operating point cannot be solved. Geometry and user
interface state belong to the caller, not to the electrical graph.
"""
# The accepted source revision owns compilation results, diagnostics, and a separate drawing.
# Changing source invalidates derived data immediately; a renderable package may still be
# shown when its numerical model or operating point is unavailable.

from dataclasses import dataclass, field
from pathlib import Path

from circuitlab.language.comments import mask_comments
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.digital_structure import assemble_digital
from circuitlab.language.linked import CHIP_LIBRARY, imported_definitions, read_includes
from circuitlab.language.syntax import INCLUDE, MAIN, SECTION, has_chip, source_rows
from circuitlab.math.analysis import Diagnostic, analyze, diagnostics
from circuitlab.model.graph import Circuit, CircuitError, MethodInstance
from circuitlab.exporters.spice import export_spice
from circuitlab.exporters.verilog import compile_chips, export_verilog
from circuitlab.tooling.pins import apply_pin_overrides, read_pins


def issue_for(error):
    """Use the same actionable solver diagnostics in every presentation."""
    if error.code == "NonlinearConvergence":
        return Diagnostic(
            "error", None,
            error.message + " Try a transient in Results or export to SPICE.",
            error.path,
        )
    return Diagnostic("error", error.line or 1, error.message, error.path)


def includes_files(source):
    return any(INCLUDE.fullmatch(text) for text, _, _ in source_rows(source))


@dataclass
class CircuitSession:
    """Own one accepted source revision and results derived from that revision."""

    path: Path | None = None
    source: str = ""
    disk_bytes: bytes | None = None
    revision: int = 0
    solution: object = None
    drawing: Circuit | None = None
    issues: list = field(default_factory=list)
    pin_overrides: dict = field(default_factory=dict)
    read_source: object = None
    preview_only: bool = False

    @property
    def pins(self):
        return apply_pin_overrides(read_pins(self.source), self.pin_overrides)

    def load(self, filename):
        """Accept a saved file only after its bytes have decoded successfully."""
        path = Path(filename).resolve()
        data = path.read_bytes()
        source = data.decode("utf-8-sig")
        self.path, self.disk_bytes = path, data
        self.set_source(source)
        self.pin_overrides = {}

    # A new source revision invalidates both the numeric solution and the drawing immediately;
    # later compilation must not accidentally present results from the previous document.
    def set_source(self, source):
        """Invalidate old results immediately when source is replaced."""
        self.source = source
        self.revision += 1
        self.solution = None
        self.drawing = None
        self.issues = []
        self.preview_only = False

    def read_linked_source(self, path):
        if self.read_source is not None:
            return self.read_source(path)
        return path.read_text(encoding="utf-8-sig")

    def build_circuit(self, mode="auto", *, render_only=False, source=None):
        return compile_circuit(
            self.source if source is None else source, mode,
            render_only=render_only, path=self.path,
            read_source=self.read_linked_source,
        )

    def library_preview(self):
        """Instantiate a library chip for inspection without an editor widget."""
        if self.path is None or not self.path.is_relative_to(CHIP_LIBRARY):
            return None
        if any(MAIN.fullmatch(row) for row, _, _ in source_rows(self.source)):
            return None
        definitions = []
        if includes_files(self.source):
            files, imports = read_includes(self.source, self.path, self.read_linked_source)
            definitions.extend(imported_definitions(next(iter(files)), files, imports).values())
        definitions.append(self.source)
        signatures = {}
        for text in definitions:
            for row, _, _ in source_rows(text):
                match = SECTION.fullmatch(row)
                if match and match[1].casefold() == "chip":
                    signatures[match[2].casefold()] = (match[2], match[3] or "")
        stem = self.path.stem.casefold()
        signature = signatures.get(stem) or signatures.get("sn" + stem)
        if signature:
            name, pins = signature
            return f"\nmain {{ {name}({pins}); }}\n"
        return None

    # Choose a digital package preview or an electrical drawing independently of numerical
    # analysis, so a missing analog model does not hide a useful pinout.
    def render(self, mode="auto"):
        """Produce the electrical or digital package view without solving it."""
        source = self.source
        if has_chip(source) and not includes_files(source):
            chips = compile_chips(source)
            assembled_instances = []
            assembly = assemble_digital(source, instances=assembled_instances)
            self.drawing = Circuit(
                "dc",
                instances=assembled_instances if assembly else [
                    MethodInstance(
                        "chip", name, None,
                        tuple((pin, pin if len(chips) == 1 else f"@{name}.{pin}") for pin, _ in pins),
                        body,
                    )
                    for name, pins, body, _ in chips
                ],
            )
            if assembly:
                self.drawing.named_wires.update(assembly[1])
        else:
            preview = self.library_preview()
            self.drawing = self.build_circuit(mode, render_only=True, source=source + (preview or ""))
        return self.drawing

    # Try solving and rendering as separate operations. Keep useful geometry when solving fails,
    # and avoid reporting a missing drawing as an error for quantity-only calculations.
    def compile(self, mode="auto", *, solve=True):
        """Refresh diagnostics, solution and drawing for the current source.

        Analysis and drawing intentionally remain distinct: legacy reduced-group
        calculations do not contain the same graph used for schematic geometry.
        """
        self.solution = None
        self.drawing = None
        self.issues = []
        self.preview_only = False
        if not mask_comments(self.source).strip():
            return
        try:
            self.preview_only = bool(self.library_preview()) or (
                has_chip(self.source) and not includes_files(self.source)
            )
        except CircuitError as error:
            self.issues = [issue_for(error)]
            return
        if solve and not self.preview_only:
            try:
                self.solution = analyze(self.build_circuit(mode))
                self.issues = diagnostics(self.source, self.solution)
            except CircuitError as error:
                self.issues = [issue_for(error)]
        try:
            self.render("dc" if self.preview_only else mode)
        except CircuitError as error:
            # A valid quantity-only calculation has no schematic to draw.
            quantities_only = self.solution is not None and not self.solution.circuit.components
            if not self.issues and not quantities_only:
                self.issues = [issue_for(error)]

    def spice(self, mode="auto"):
        return export_spice(
            self.source, mode, path=self.path, read_source=self.read_linked_source,
        )

    def verilog(self):
        return export_verilog(self.source)
