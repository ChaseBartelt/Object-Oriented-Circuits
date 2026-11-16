"""Compile local source or linked files into an electrical circuit."""

from circuitlab.language.document import compile_document
from circuitlab.language.linked import compile_linked
from circuitlab.language.syntax import INCLUDE, source_rows


def compile_circuit(source, mode="auto", *, render_only=False, path=None, read_source=None):
    """Compile source into a circuit graph or standalone quantity inputs."""
    if not isinstance(source, str):
        source = "\n".join(source)
    if path is not None or any(
        INCLUDE.fullmatch(row)
        for row, _, _ in source_rows(source)
    ):
        return compile_linked(source, path, mode, render_only=render_only, read_source=read_source)
    return compile_document(source, mode, render_only=render_only)
