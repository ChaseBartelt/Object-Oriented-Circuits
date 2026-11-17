"""Compile explicitly included circuit files with named connector pins."""

from dataclasses import replace
from pathlib import Path
import re

from circuitlab.language.builder import Compiler, compile_program
from circuitlab.language.document import compile_document, elaborate_document
from circuitlab.model.graph import CircuitError
from circuitlab.math.numbers import set_known
from circuitlab.language.syntax import INCLUDE, NET_METHODS, OVERRIDE, SECTION, analysis_mode, has_chip, source_rows

CHIP_LIBRARY = Path(__file__).resolve().parents[1] / "libraries" / "chips"


def include_path(name, source_path=None):
    """Resolve a short bundled chip name or an explicit file path."""
    library = CHIP_LIBRARY.resolve()
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name):
        key = name.casefold()
        matches = sorted({
            resolved for candidate in library.rglob("*.ckt")
            if candidate.stem.casefold() == key and candidate.is_file()
            and (resolved := candidate.resolve()).is_relative_to(library)
        })
        if not matches:
            raise CircuitError(f'Unknown chip "{name}". Use a library chip name or a relative .ckt or .txt path.')
        if len(matches) > 1:
            choices = ", ".join(f'@chips/{p.relative_to(library).as_posix()}' for p in matches)
            raise CircuitError(f'Ambiguous chip name "{name}". Use one of: {choices}.')
        return matches[0]
    filename = Path(name.replace("\\", "/"))
    if filename.is_absolute() or filename.drive or filename.suffix.lower() not in {".ckt", ".txt"}:
        raise CircuitError("Use a library chip name or a relative path to a .ckt or .txt file.")
    if filename.parts[0] == "@chips":
        target = library.joinpath(*filename.parts[1:]).resolve()
        if not target.is_relative_to(library):
            raise CircuitError("Use a path inside the chip library.")
        return target
    if source_path is None:
        raise CircuitError("Save this circuit to resolve its relative include paths.")
    return (source_path.parent / filename).resolve()


def read_includes(source, path, read_source=None):
    """Collect file text and direct dependencies in discovery order."""
    entry = Path(path).resolve() if path is not None else None
    if read_source is None:
        read_source = lambda path: path.read_text(encoding="utf-8-sig")
    files = {}
    imports = {}
    visiting = set()

    def visit(path, text):
        visiting.add(path)
        lines = text.splitlines()
        files[path] = text
        imports[path] = []
        depth = 0
        for i, line in enumerate(lines):
            include = INCLUDE.fullmatch(line.strip()) or INCLUDE.fullmatch(
                line.partition("#")[0].strip()
            )
            if include:
                if depth:
                    raise CircuitError("Put include(...) at the top level.", i + 1, path=path)
                try:
                    target = include_path(include[1], path)
                except CircuitError as error:
                    raise CircuitError(error.message, i + 1, path=path) from None
                if target in visiting:
                    raise CircuitError(f'Include cycle through "{include[1]}".', i + 1, path=path)
                if target not in files:
                    try:
                        child = read_source(target)
                    except (OSError, UnicodeError) as error:
                        raise CircuitError(f'Cannot open "{include[1]}": {error}', i + 1, path=path) from None
                    visit(target, child)
                imports[path].append(target)
                lines[i] = ""
            else:
                code = line.partition("#")[0]
                depth += code.count("{") - code.count("}")
        files[path] = "\n".join(lines)
        visiting.remove(path)

    visit(entry, source)
    return files, imports


def definition_file(source):
    """Recognize include files whose top level contains only reusable definitions."""
    depth = 0
    for row, _, _ in source_rows(source):
        if depth == 0 and not OVERRIDE.fullmatch(row):
            header = SECTION.fullmatch(row)
            if not header or header[1].lower() not in NET_METHODS:
                return False
        depth += row.count("{") - row.count("}")
    return True


def imported_definitions(path, files, imports):
    definitions = {}

    def collect(parent):
        for child in imports[parent]:
            if child not in definitions and definition_file(files[child]):
                definitions[child] = files[child]
                collect(child)

    collect(path)
    return definitions


def compile_linked(source, path, mode, *, render_only=False, read_source=None):
    """Build electrical files and make imported definitions available to their callers."""
    files, imports = read_includes(source, path, read_source)
    entry = next(iter(files))
    if len(files) == 1:
        return compile_document(source, mode, render_only=render_only, path=entry)
    executable = {
        path: text for path, text in files.items() if path == entry or not definition_file(text)
    }
    programs = {}
    for path, text in executable.items():
        try:
            if has_chip(text):
                raise CircuitError("File connectors link electrical circuits. Export digital chips to Verilog.")
            programs[path] = elaborate_document(
                text, path=path, definitions=imported_definitions(path, files, imports)
            )
        except CircuitError as error:
            raise CircuitError(error.message, error.line, error.code, path=error.path or path) from None
    mode = mode.lower()
    if mode == "auto":
        mode = analysis_mode("\n".join(program.mode_source for program in programs.values()))
    if mode == "transient":
        raise CircuitError("Linked files use a steady-state analysis mode.")

    circuits = []
    for path, program in programs.items():
        try:
            circuit = compile_program(program, mode)
            if circuit.reduce_groups and (render_only or len(programs) > 1):
                circuit = compile_program(program, mode, render_only=True)
        except CircuitError as error:
            raise CircuitError(error.message, error.line, error.code, path=error.path or path) from None
        circuits.append((path, circuit))

    if len(circuits) == 1:
        return circuits[0][1]

    root = circuits[0][1]
    shared = {pin for _, circuit in circuits for pin in circuit.connector_pins}
    private = set(root.nodes) - {root.resolve_wire(pin) for pin in root.connector_pins}
    shared_names = {pin: f"@link.{pin}" if pin in private and pin != "0" else pin for pin in shared}
    for _, circuit in circuits:
        for pin in circuit.connector_pins:
            if circuit.resolve_wire(pin) == "0":
                shared_names[pin] = "0"
    combined_mode = "mixed" if any(circuit.mode == "mixed" for _, circuit in circuits) else mode
    result = replace(
        root, mode=combined_mode, components=[], known={}, probes=[], port=None, grounds=set(), named_wires=set(),
        wire_taps=[], instances=[], owners={}, component_lines={}, component_files={}, groups=[],
        grouped=False, reduce_groups=False, connector_pins=tuple(sorted(shared)),
    )
    for index, (path, circuit) in enumerate(circuits):
        exposed = {}
        for pin in circuit.connector_pins:
            local = circuit.resolve_wire(pin)
            if local in exposed and exposed[local] != shared_names[pin]:
                raise CircuitError("Declare one connector name per local net.", path=path)
            exposed[local] = shared_names[pin]
        prefix = f"@file{index}."

        def net(name):
            if name == "0" or not name:
                return name
            return exposed.get(name, prefix + name if index else name)

        def part_name(name):
            return prefix + name if index else name

        base = len(result.instances)
        for part in circuit.components:
            name = part_name(part.name)
            result.components.append(replace(
                part, name=name, label=f"{path.stem}.{part.label}" if index else part.label,
                p=net(part.p), n=net(part.n), base=net(part.base),
                in_plus=net(part.in_plus), in_minus=net(part.in_minus),
            ))
            result.component_files[name] = circuit.component_files.get(part.name, path)
            if part.name in circuit.component_lines:
                result.component_lines[name] = circuit.component_lines[part.name]
            if part.name in circuit.owners:
                result.owners[name] = base + circuit.owners[part.name]
        result.instances.extend(replace(
            instance, parent=None if instance.parent is None else base + instance.parent,
            pins=tuple((pin, net(circuit.resolve_wire(value))) for pin, value in instance.pins),
            arguments=tuple((raw, net(circuit.resolve_wire(value))) for raw, value in instance.arguments),
            path=instance.path or path,
        ) for instance in circuit.instances)
        result.grounds.update(map(net, circuit.grounds))
        result.named_wires.update(map(net, circuit.named_wires))
        result.wire_taps.extend(replace(
            tap, name=net(tap.name), net=net(tap.net), component=part_name(tap.component),
        ) for tap in circuit.wire_taps)
        for probe in circuit.probes:
            if probe.kind in {"I", "P"}:
                targets = tuple(part_name(next(
                    (part.name for part in circuit.components if target.casefold() in (
                        part.name.casefold(), part.label.casefold()
                    )), target
                )) for target in probe.targets)
            else:
                targets = tuple(net(circuit.resolve_wire(target)) for target in probe.targets)
            result.probes.append(replace(probe, targets=targets))
        if index == 0 and circuit.port:
            result.port = tuple(net(circuit.resolve_wire(pin)) for pin in circuit.port)
        for key, value in circuit.known.items():
            try:
                set_known(result.known, key, value)
            except CircuitError as error:
                raise CircuitError(error.message, path=path) from None
    if any(part.kind == "O" for part in result.components) and result.grounds != {"0"}:
        compiler = Compiler(result.mode)
        compiler.circuit = result
        compiler.connect_amp_returns()
    if render_only and not (result.components or result.instances):
        raise CircuitError("Add components to the entry file or an included circuit.")
    return result
