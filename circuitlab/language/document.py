"""Expand one document into scoped statements before electrical graph construction."""
# Expansion happens before electrical construction: reusable definitions and static control flow
# become ordered statements whose original locations remain available for diagnostics.

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
    # Separate document expansion from electrical construction. The first step
    # returns bound statements/metadata; compile_program then interprets parts,
    # groups, aliases, and analysis mode. Neither step runs a numerical solver.
    program = elaborate_document(source, path=path, definitions=definitions)
    # Forward render_only to graph construction without changing how source
    # locations, imports, or method expansion are represented in the program.
    return compile_program(program, mode, render_only=render_only)


# Intermediate contract shared with the graph builder and file linker.
# Keeping expanded rows separate from mode-detection text avoids losing
# source provenance just to classify the resulting circuit.
@dataclass
class CircuitProgram:
    # Each row is (text, original_line, Binding). Binding carries local-to-caller
    # net/part scope and optional file/instance origin after method expansion.
    rows: list[SourceRow]
    # File-level connector name -> declaration line. This collects declarations;
    # compile_program later verifies that each connector resolves to a real net.
    connectors: dict[str, int]
    # Expanded invocation metadata for hierarchy/diagram display. Row bindings
    # refer to these instances without inserting diagram objects into equations.
    instances: list[MethodInstance]
    # Text specifically prepared for automatic mode detection, not a replacement
    # source document. The builder or linker performs classification afterward.
    mode_source: str


# Prepare automatic-mode input after expansion. Static control flow uses emitted rows alone;
# ordinary documents retain their legacy source cues as well as expanded statements.
def elaborate_document(source, *, path=None, definitions=None):
    """Expand the executable statements before automatic mode selection."""
    # Behavioral/digital chips belong to the HDL exporter. Electrical reusable
    # chip definitions without digital port behavior still use this pipeline.
    if has_chip(source):
        raise CircuitError(
            "Use File > Export Verilog for this chip."
        )
    rows = []
    connectors = {}
    depth = 0
    # Tokenize logical source rows while retaining physical line numbers.
    # Peel off file-level connector metadata before expanding executable code;
    # depth tracks whether a connector appears inside a braced structure.
    for text, line, binding in source_rows(source):
        declaration = CONNECTOR.fullmatch(text)
        if declaration:
            # Connectors belong to the file interface, not a method/loop/conditional
            # body. Their declarations are checked before static branch selection.
            if depth:
                raise CircuitError("Put connector(...) at the top level.", line)
            try:
                pins = parse_wires(declaration[1])
            # Translate a general wire-list error into connector-specific guidance
            # at the declaration line; suppress an unhelpful nested exception chain.
            except CircuitError:
                raise CircuitError("List wire names in connector(pin, ...).", line) from None
            # Keep a single declaration location for every exposed name. Duplicate
            # connectors are an error even if their spelling repeats consistently.
            for pin in pins:
                if pin in connectors:
                    raise CircuitError(f'Connector pin "{pin}" is already declared.', line)
                connectors[pin] = line
        # An unresolved include means this function has not received a fully linked
        # document. File loading, relative-path resolution, and cycle checking
        # belong to the linker; elaboration does no filesystem traversal.
        elif INCLUDE.fullmatch(text):
            raise CircuitError("Save this circuit to resolve its relative include paths.", line)
        else:
            # Attach the entry document's supplied path using a new Binding. Do not
            # renumber the row or mutate the binding returned by the source tokenizer.
            rows.append((text, line, replace(binding, path=path) if path else binding))
            depth += text.count("{") - text.count("}")
    # definitions maps already loaded definition-file identities to text.
    # Give each imported row its own file origin and pass it separately as
    # reusable definitions, rather than treating it as another executable body.
    imports = [
        (text, line, replace(binding, path=filename))
        for filename, content in (definitions or {}).items()
        for text, line, binding in source_rows(content)
    ]
    # expand_main fills this invocation list while producing rows with bound
    # instance identities. Passing one list keeps those indexes consistent
    # between the expanded program and its eventual graphical hierarchy.
    instances = []
    expanded = expand_main(rows, instances, imports=imports)
    # Typed and imported methods may introduce AC impedances or source units.
    # Typed/imported method calls can emit an AC component or source spelling
    # that was not visible at the call site. Include the actual emitted text
    # when preparing automatic mode detection.
    expanded_source = "\n".join(text for text, _, _ in expanded)
    # Without static control flow, retain legacy source-based mode cues as
    # well as newly emitted ones. This preserves existing interpretation of
    # ordinary documents while recognizing types introduced by expansion.
    mode_source = source + "\n" + expanded_source
    # If local or imported rows opt into static control flow, classify only
    # what was emitted. An unused AC branch, for example, must not turn an
    # actually generated DC network into AC analysis.
    if any(is_control_statement(text) for section in (rows, imports) for text, _, _ in section):
        mode_source = expanded_source
    # Return the expanded program and metadata for the next stage. The original
    # source text and any saved layout comments have not been rewritten in place.
    return CircuitProgram(expanded, connectors, instances, mode_source)
