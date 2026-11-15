"""Expand one document into scoped statements before electrical graph construction."""

from dataclasses import dataclass, replace

from circuitlab.language.control_flow import is_control_statement
from circuitlab.model.graph import CircuitError, MethodInstance
from circuitlab.language.methods import expand_main
from circuitlab.language.syntax import (
    CONNECTOR,
    INCLUDE,
    SourceRow,
    has_chip,
    parse_wires,
    source_rows,
)
from circuitlab.language.builder import compile_program


def compile_document(source, mode, *, render_only=False, path=None, definitions=None):
    """Elaborate one document after its includes have been read."""
    program = elaborate_document(source, path=path, definitions=definitions)
    return compile_program(program, mode, render_only=render_only)


@dataclass
class CircuitProgram:
    rows: list[SourceRow]
    connectors: dict[str, int]
    instances: list[MethodInstance]
    mode_source: str


def elaborate_document(source, *, path=None, definitions=None):
    """Expand the executable statements before automatic mode selection."""
    if has_chip(source):
        raise CircuitError(
            "Use File > Export Verilog for this chip."
        )
    rows = []
    connectors = {}
    depth = 0
    for text, line, binding in source_rows(source):
        declaration = CONNECTOR.fullmatch(text)
        if declaration:
            if depth:
                raise CircuitError("Put connector(...) at the top level.", line)
            try:
                pins = parse_wires(declaration[1])
            except CircuitError:
                raise CircuitError("List wire names in connector(pin, ...).", line) from None
            for pin in pins:
                if pin in connectors:
                    raise CircuitError(f'Connector pin "{pin}" is already declared.', line)
                connectors[pin] = line
        elif INCLUDE.fullmatch(text):
            raise CircuitError("Save this circuit to resolve its relative include paths.", line)
        else:
            rows.append((text, line, replace(binding, path=path) if path else binding))
            depth += text.count("{") - text.count("}")
    imports = [
        (text, line, replace(binding, path=filename))
        for filename, content in (definitions or {}).items()
        for text, line, binding in source_rows(content)
    ]
    instances = []
    expanded = expand_main(rows, instances, imports=imports)
    mode_source = source
    if any(is_control_statement(text) for section in (rows, imports) for text, _, _ in section):
        mode_source = "\n".join(text for text, _, _ in expanded)
    return CircuitProgram(expanded, connectors, instances, mode_source)
