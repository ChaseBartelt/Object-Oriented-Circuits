"""Compile explicitly included circuit files with named connector pins."""

from dataclasses import replace
from pathlib import Path

from .compiler import Compiler, compile_circuit
from .graph import CircuitError
from .numbers import set_known
from .syntax import INCLUDE, analysis_mode, has_chip


def compile_linked(source, path, mode, *, render_only=False, read_source=None):
    """Build the entry file and each include once, resolving paths from the caller."""
    entry = Path(path).resolve()
    if read_source is None:
        read_source = lambda path: path.read_text(encoding="utf-8-sig")
    files = {}
    visiting = set()

    def visit(path, text):
        visiting.add(path)
        lines = text.splitlines()
        files[path] = text
        depth = 0
        for i, line in enumerate(lines):
            include = INCLUDE.fullmatch(line.strip()) or INCLUDE.fullmatch(
                line.partition("#")[0].strip()
            )
            if include:
                if depth:
                    raise CircuitError("Put include(...) at the top level.", i + 1, path=path)
                filename = Path(include[1])
                if filename.is_absolute() or filename.drive or filename.suffix.lower() not in {".ckt", ".txt"}:
                    raise CircuitError("Use a relative path to a .ckt or .txt file.", i + 1, path=path)
                target = (path.parent / filename).resolve()
                if target in visiting:
                    raise CircuitError(f'Include cycle through "{include[1]}".', i + 1, path=path)
                if target not in files:
                    try:
                        child = read_source(target)
                    except (OSError, UnicodeError) as error:
                        raise CircuitError(f'Cannot open "{include[1]}": {error}', i + 1, path=path) from None
                    visit(target, child)
                lines[i] = ""
            else:
                code = line.partition("#")[0]
                depth += code.count("{") - code.count("}")
        files[path] = "\n".join(lines)
        visiting.remove(path)

    visit(entry, source)
    if len(files) == 1:
        return compile_circuit(source, mode, render_only=render_only)
    mode = analysis_mode("\n".join(files.values())) if mode.lower() == "auto" else mode.lower()
    if mode == "transient":
        raise CircuitError("Linked files use a steady-state analysis mode.")

    circuits = []
    for path, text in files.items():
        try:
            if has_chip(text):
                raise CircuitError("File connectors link electrical circuits. Export digital chips to Verilog.")
            circuit = compile_circuit(text, mode)
            if circuit.reduce_groups:
                circuit = compile_circuit(text, mode, render_only=True)
        except CircuitError as error:
            raise CircuitError(error.message, error.line, error.code, path=path) from None
        circuits.append((path, circuit))

    root = circuits[0][1]
    shared = {pin for _, circuit in circuits for pin in circuit.connector_pins}
    private = set(root.nodes) - {root.resolve_wire(pin) for pin in root.connector_pins}
    shared_names = {pin: f"@link.{pin}" if pin in private and pin != "0" else pin for pin in shared}
    for _, circuit in circuits:
        for pin in circuit.connector_pins:
            if circuit.resolve_wire(pin) == "0":
                shared_names[pin] = "0"
    if any(circuit.mode == "mixed" for _, circuit in circuits):
        combined_mode = "mixed"
    else:
        combined_mode = mode
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
            result.component_files[name] = path
            if part.name in circuit.component_lines:
                result.component_lines[name] = circuit.component_lines[part.name]
            if part.name in circuit.owners:
                result.owners[name] = base + circuit.owners[part.name]
        result.instances.extend(replace(
            instance, parent=None if instance.parent is None else base + instance.parent,
            pins=tuple((pin, net(circuit.resolve_wire(value))) for pin, value in instance.pins),
            arguments=tuple((raw, net(circuit.resolve_wire(value))) for raw, value in instance.arguments),
            path=path,
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
    if render_only and not result.components:
        raise CircuitError("Add components to the entry file or an included circuit.")
    return result
