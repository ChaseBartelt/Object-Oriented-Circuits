"""Public electrical compiler entry point for source and linked circuit files.

Compatibility exports keep existing compiler imports working while document
expansion and graph construction have their own implementation modules.
"""

from circuitlab.language.builder import (
    Battery, Compiler, Group, compile_program, resolve_alias,
    _ac_source, _group_voltage, _source,
)
from circuitlab.language.document import CircuitProgram, compile_document, elaborate_document
from circuitlab.language.linked import compile_linked
from circuitlab.model.graph import (
    Circuit, CircuitError, Component, MethodInstance, Probe, Pulse, WireTap,
    ground_references,
)
from circuitlab.language.syntax import (
    INCLUDE, SourceRow, analysis_mode, has_chip, parse_wires, source_rows,
)


def compile_circuit(source, mode="auto", *, render_only=False, path=None, read_source=None):
    """Compile source into a circuit graph or standalone quantity inputs."""
    if not isinstance(source, str):
        source = "\n".join(source)
    if path is not None or any(
        (match := INCLUDE.fullmatch(row)) and match[1].startswith("@chips/")
        for row, _, _ in source_rows(source)
    ):
        return compile_linked(source, path, mode, render_only=render_only, read_source=read_source)
    return compile_document(source, mode, render_only=render_only)
