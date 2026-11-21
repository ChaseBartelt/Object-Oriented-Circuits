"""Compile explicitly included circuit files with named connector pins."""
# Only explicitly included files participate in a compilation; unrelated neighboring files do not.
# Resolve imports once, detect cycles, and preserve per-file identities so connector names can
# be shared without accidentally merging unrelated local nets.

from dataclasses import replace
from pathlib import Path
import re

from circuitlab.language.builder import Compiler, compile_program
from circuitlab.language.document import compile_document, elaborate_document
from circuitlab.model.graph import CircuitError
from circuitlab.math.numbers import set_known
from circuitlab.language.math_blocks import remap_equation
from circuitlab.language.comments import mask_comments
from circuitlab.language.syntax import INCLUDE, NET_METHODS, OVERRIDE, SECTION, analysis_mode, has_chip, source_rows

# Locate the bundled definitions relative to this module in the installed
# package, independently of the editor's current working directory.
CHIP_LIBRARY = Path(__file__).resolve().parents[1] / "libraries" / "chips"


def include_path(name, source_path=None):
    """Resolve a short bundled chip name or an explicit file path."""
    # Normalize library paths before searching or checking containment. Bare
    # chip identifiers search the library; names with an extension or path
    # separator are treated as explicit file references instead.
    library = CHIP_LIBRARY.resolve()
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name):
        # Match a chip's filename stem without case sensitivity across categories.
        # Resolve and deduplicate matches so multiple entries pointing to the same
        # file do not create false ambiguity. Ignore targets outside the library,
        # including a symbolic link whose resolved destination escapes it.
        key = name.casefold()
        matches = sorted({
            resolved for candidate in library.rglob("*.ckt")
            if candidate.stem.casefold() == key and candidate.is_file()
            and (resolved := candidate.resolve()).is_relative_to(library)
        })
        # A short name must identify exactly one bundled file. If categories share
        # a stem, list explicit @chips paths so the caller can choose a definition.
        if not matches:
            raise CircuitError(f'Unknown chip "{name}". Use a library chip name or a relative .ckt or .txt path.')
        if len(matches) > 1:
            choices = ", ".join(f'@chips/{p.relative_to(library).as_posix()}' for p in matches)
            raise CircuitError(f'Ambiguous chip name "{name}". Use one of: {choices}.')
        return matches[0]
    # Accept either slash convention in source text. Explicit includes must
    # be relative .ckt or .txt paths; absolute and drive-qualified paths are
    # rejected before either library or source-relative resolution.
    filename = Path(name.replace("\\", "/"))
    if filename.is_absolute() or filename.drive or filename.suffix.lower() not in {".ckt", ".txt"}:
        raise CircuitError("Use a library chip name or a relative path to a .ckt or .txt file.")
    # @chips is a virtual root for bundled files, so these paths work even
    # before the entry circuit is saved. Check the resolved target to catch
    # .. traversal as well as links that leave the library. Actual reading
    # and missing-file errors are handled later by read_includes().
    if filename.parts[0] == "@chips":
        target = library.joinpath(*filename.parts[1:]).resolve()
        if not target.is_relative_to(library):
            raise CircuitError("Use a path inside the chip library.")
        return target
    # Other relative paths are based on the file containing the include,
    # not on the entry file or the process working directory. They therefore
    # need a saved containing file; unlike @chips paths, they may resolve
    # outside that file's directory through an explicit relative path.
    if source_path is None:
        raise CircuitError("Save this circuit to resolve its relative include paths.")
    return (source_path.parent / filename).resolve()


# Track direct dependencies separately from loaded text so cycles and error origins remain
# visible even when several include paths reach the same file.
def read_includes(source, path, read_source=None):
    """Collect file text and direct dependencies in discovery order."""
    # The supplied entry text is authoritative, allowing an unsaved editor
    # buffer to differ from disk. None represents an unnamed entry document;
    # its bundled-library includes can still be resolved.
    entry = Path(path).resolve() if path is not None else None
    # Callers may supply a reader for open editor buffers or virtual sources.
    # Otherwise read UTF-8 and tolerate a leading byte-order mark.
    if read_source is None:
        read_source = lambda path: path.read_text(encoding="utf-8-sig")
    # files stores include-stripped text in depth-first discovery order.
    # imports stores each file's direct include edges, including repeats.
    # visiting contains only the active recursion path, distinguishing a
    # cycle from a harmless second reference to an already loaded file.
    files = {}
    imports = {}
    visiting = set()

    # Register a file before descending so recursive references are visible.
    # Each include is removed from its source after traversal; blanking its
    # line keeps the line positions of the remaining statements unchanged.
    def visit(path, text):
        visiting.add(path)
        lines = text.splitlines()
        files[path] = text
        imports[path] = []
        depth = 0
        for i, code in enumerate(mask_comments(text).splitlines()):
            include = INCLUDE.fullmatch(code.strip())
            if include:
                # Imports must be top-level directives. Report the including file and
                # its one-based physical line, rather than a later expanded row number.
                if depth:
                    raise CircuitError("Put include(...) at the top level.", i + 1, path=path)
                # Path-resolution errors also belong to the directive that requested
                # the file. Errors found while visiting a child keep that child's origin.
                try:
                    target = include_path(include[1], path)
                except CircuitError as error:
                    raise CircuitError(error.message, i + 1, path=path) from None
                # Test the active recursion path before the loaded-file cache: an active
                # ancestor is already in files, but including it would create a cycle.
                if target in visiting:
                    raise CircuitError(f'Include cycle through "{include[1]}".', i + 1, path=path)
                # Read and expand a resolved target only once, even when different path
                # spellings or multiple parents reach it. Convert filesystem/decoding
                # failures into compiler diagnostics at the requesting include line.
                if target not in files:
                    try:
                        child = read_source(target)
                    except (OSError, UnicodeError) as error:
                        raise CircuitError(f'Cannot open "{include[1]}": {error}', i + 1, path=path) from None
                    visit(target, child)
                # Retain the direct edge even for a cached target. Definition visibility
                # depends on which file includes which, not merely on the loaded set.
                imports[path].append(target)
                # Preserve any block-comment delimiters beside the directive.
                start, end = len(code) - len(code.lstrip()), len(code.rstrip())
                lines[i] = lines[i][:start] + " " * (end - start) + lines[i][end:]
            else:
                # Track brace nesting on non-include lines after stripping comments.
                # This is an include-placement check; the language parser performs
                # the later statement and block validation.
                depth += code.count("{") - code.count("}")
        files[path] = "\n".join(lines)
        # Only completed files leave the active path. They stay cached in files
        # for later references, with include lines replaced by empty lines.
        visiting.remove(path)

    # Start with caller-provided source instead of re-reading the entry.
    # Dictionary insertion order then makes the entry the first file.
    visit(entry, source)
    return files, imports


# Classify a file by its emitted source rows, not its extension. This is
# a structural eligibility check for imports, not full syntax validation;
# empty/comment-only files also contain no executable top-level rows.
def definition_file(source):
    """Recognize include files whose top level contains only reusable definitions."""
    depth = 0
    for row, _, _ in source_rows(source):
        # At top level allow reusable circuit/gate/chip headers and @override.
        # Their bodies are skipped by depth. A main block, component placement,
        # connector declaration, or other top-level statement makes the file
        # executable rather than a collection of reusable definitions.
        if depth == 0 and not OVERRIDE.fullmatch(row):
            header = SECTION.fullmatch(row)
            if not header or header[1].lower() not in NET_METHODS:
                return False
        depth += row.count("{") - row.count("}")
    return True


# Collect definitions visible to one executable file. Visibility follows
# only chains of definition-only includes; definitions included by another
# executable circuit do not automatically become visible here.
def imported_definitions(path, files, imports):
    # Use an ordered dictionary both as the collected source mapping and as
    # a visited set, so repeated includes contribute their definitions once.
    definitions = {}

    def collect(parent):
        for child in imports[parent]:
            if child not in definitions and definition_file(files[child]):
                # Insert before descending to retain discovery order and deduplicate
                # dependencies shared by several definition files. read_includes()
                # has already rejected cycles in the complete dependency graph.
                definitions[child] = files[child]
                collect(child)

    collect(path)
    return definitions


# Import reusable definitions, compile executable files with private scopes, and join only
# the connector nets explicitly shared by name.
def compile_linked(source, path, mode, *, render_only=False, read_source=None):
    """Build electrical files and make imported definitions available to their callers."""
    # Load the dependency graph first; no unrelated neighboring files are
    # scanned for user definitions or automatically added as circuit parts.
    files, imports = read_includes(source, path, read_source)
    entry = next(iter(files))
    # Without dependencies, preserve normal single-document compilation,
    # including its mode handling and render-only behavior.
    if len(files) == 1:
        return compile_document(source, mode, render_only=render_only, path=entry)
    # Always compile the entry. Other files either supply reusable definitions
    # or contribute their own circuit graph. A definition-only import creates
    # components only when one of its methods is actually instantiated.
    executable = {
        path: text for path, text in files.items() if path == entry or not definition_file(text)
    }
    # Elaborate every executable file before selecting a shared analysis mode.
    # Keep each file's source bindings and imported definitions attached to
    # its program so expansion diagnostics can name the actual origin.
    programs = {}
    for path, text in executable.items():
        try:
            # Connector linking builds electrical graphs. Digital behavioral chip
            # documents instead use the separate Verilog export path; ordinary
            # electrical library definitions are still available to elaboration.
            if has_chip(text):
                raise CircuitError("File connectors link electrical circuits. Export digital chips to Verilog.")
            programs[path] = elaborate_document(
                text, path=path, definitions=imported_definitions(path, files, imports)
            )
        # Preserve an error's explicit origin, such as an imported definition.
        # Only add the current executable path when the diagnostic has none.
        except CircuitError as error:
            raise CircuitError(error.message, error.line, error.code, path=error.path or path) from None
    mode = mode.lower()
    # Infer one mode from all elaborated executable programs, including
    # instantiated imports. Avoid choosing DC just because the entry omits
    # the AC quantities or sources that appear in a dependency.
    if mode == "auto":
        mode = analysis_mode("\n".join(program.mode_source for program in programs.values()))
    # This linker accepts steady-state graph modes. The language's separate
    # quantity-based transient analysis is not supported for linked files.
    if mode == "transient":
        raise CircuitError("Linked files use a steady-state analysis mode.")

    # Compile each program in the selected mode before renaming its nets.
    # Per-file validation and provenance are established by compile_program().
    circuits = []
    for path, program in programs.items():
        try:
            # An included executable may be empty before the merge, so defer
            # numerical model validation without requiring a drawable graph
            # in each individual file.
            circuit = compile_program(program, mode, model_preview=render_only)
            # If symbolic group reduction would hide graph structure, rebuild without
            # that reduction for rendering or a multi-circuit merge. Connector wiring
            # must be applied to the explicit component topology.
            if circuit.reduce_groups and (render_only or len(programs) > 1):
                circuit = compile_program(program, mode, render_only=True)
        except CircuitError as error:
            raise CircuitError(error.message, error.line, error.code, path=error.path or path) from None
        circuits.append((path, circuit))

    # Definition imports may leave only the entry executable. Its existing
    # component and net identities then need no file-level merge.
    if len(circuits) == 1:
        return circuits[0][1]

    # The entry is the root and keeps its ordinary local names. All declared
    # connector names form a common interface, independent of declaration
    # order; the same connector spelling links across executable files.
    root = circuits[0][1]
    shared = {pin for _, circuit in circuits for pin in circuit.connector_pins}
    # A connector declared only in a child must not capture an identically
    # named private root node. Give that shared interface an @link name when
    # it would collide, leaving the root's private node untouched.
    private = set(root.nodes) - {root.resolve_wire(pin) for pin in root.connector_pins}
    shared_names = {pin: f"@link.{pin}" if pin in private and pin != "0" else pin for pin in shared}
    # Resolve connector aliases before testing ground. If any occurrence of
    # a connector is wired to global node 0, its shared interface is node 0
    # for every participating file.
    for _, circuit in circuits:
        for pin in circuit.connector_pins:
            if circuit.resolve_wire(pin) == "0":
                shared_names[pin] = "0"
    # Keep mixed analysis if any compiled circuit requires it. Start from
    # root metadata, replacing the collections rebuilt by the merge rather
    # than appending into the entry circuit's existing mutable containers.
    combined_mode = "mixed" if any(circuit.mode == "mixed" for _, circuit in circuits) else mode
    result = replace(
        root, mode=combined_mode, components=[], known={}, probes=[], port=None, grounds=set(), named_wires=set(),
        wire_taps=[], instances=[], owners={}, component_lines={}, component_files={}, groups=[],
        grouped=False, reduce_groups=False, connector_pins=tuple(sorted(shared)), behavior=[], properties=[], model_issues=[], comparators=[],
    )
    # Process circuits in discovery order. Only declared connector nets are
    # exposed; remaining names in each child receive a unique file prefix.
    for index, (path, circuit) in enumerate(circuits):
        # Map resolved local nets to shared names, so connector aliases expose
        # the underlying electrical net. Reject two different shared identities
        # for one local net instead of silently choosing one connection.
        exposed = {}
        for pin in circuit.connector_pins:
            local = circuit.resolve_wire(pin)
            if local in exposed and exposed[local] != shared_names[pin]:
                raise CircuitError("Declare one connector name per local net.", path=path)
            exposed[local] = shared_names[pin]
        # The file index qualifies child parts and private nets consistently.
        # The root uses its original names except where a connector remaps a net.
        prefix = f"@file{index}."

        # Empty optional terminals and node 0 remain unchanged. Other terminals
        # use an exposed connector mapping first, then the local file namespace.
        # This helper expects a resolved local net where aliases are possible.
        def net(name):
            if name == "0" or not name:
                return name
            return exposed.get(name, prefix + name if index else name)

        # Part names occupy their own namespace: they are qualified per file,
        # but are never replaced by connector-net mappings.
        def part_name(name):
            return prefix + name if index else name

        # Instance ownership indexes are local to each compiled file. Save the
        # current combined length so parents and component owners can be shifted
        # into the merged instance list without pointing into another file.
        base = len(result.instances)
        result.comparators.extend(replace(
            cmp,name=part_name(cmp.name),owner=base+cmp.owner,output_name=part_name(cmp.output_name),
            plus=net(cmp.plus),minus=net(cmp.minus),collector=net(cmp.collector),emitter=net(cmp.emitter),
            vplus=net(cmp.vplus),vminus=net(cmp.vminus),members=tuple(map(part_name,cmp.members)),
            internal_nodes=frozenset(map(net,cmp.internal_nodes))) for cmp in circuit.comparators)
        for issue in circuit.model_issues:
            issue = replace(issue, owner=base + issue.owner if issue.owner is not None else None)
            if issue not in result.model_issues:
                result.model_issues.append(issue)
        result.properties.extend(replace(prop, scope=part_name(prop.scope) if index else prop.scope,
                                         owner=base + prop.owner if prop.owner is not None else None)
                                 for prop in circuit.properties)
        result.behavior.extend(remap_equation(
            equation, net, name=part_name(equation.name),
            owner=base + equation.owner if equation.owner is not None else None,
            state_key=part_name,
        ) for equation in circuit.behavior)
        for part in circuit.components:
            name = part_name(part.name)
            # Copy the component with qualified identity and the terminal fields
            # listed here remapped. Preserve its model/value parameters; child labels
            # also include the source file stem for readable schematic identification.
            result.components.append(replace(
                part, name=name, label=f"{path.stem}.{part.label}" if index else part.label,
                p=net(part.p), n=net(part.n), base=net(part.base),
                wiper=net(part.wiper),
                in_plus=net(part.in_plus), in_minus=net(part.in_minus),
                vplus=net(part.vplus), vminus=net(part.vminus),
                gain1=net(part.gain1), gain8=net(part.gain8), bypass=net(part.bypass),
            ))
            # Carry source maps under the new part name. Prefer the original source
            # path, which may name a reusable definition instead of this executable.
            # Offset any owner index by the combined instance-list base.
            result.component_files[name] = circuit.component_files.get(part.name, path)
            if part.name in circuit.component_lines:
                result.component_lines[name] = circuit.component_lines[part.name]
            if part.name in circuit.owners:
                result.owners[name] = base + circuit.owners[part.name]
        # Remap hierarchy parents, formal-to-actual pin bindings, and argument
        # bindings together. Resolve local wire aliases before translating nets;
        # retain an existing definition path instead of replacing its provenance.
        result.instances.extend(replace(
            instance, parent=None if instance.parent is None else base + instance.parent,
            pins=tuple((pin, net(circuit.resolve_wire(value))) for pin, value in instance.pins),
            arguments=tuple((raw, net(circuit.resolve_wire(value))) for raw, value in instance.arguments),
            path=instance.path or path,
        ) for instance in circuit.instances)
        # Move drawing/reference metadata into the same namespace as components.
        # Wire taps need both their alias/net translated and their owning part
        # renamed so schematic connections still point to the correct terminal.
        result.grounds.update(map(net, circuit.grounds))
        result.named_wires.update(map(net, circuit.named_wires))
        result.wire_taps.extend(replace(
            tap, name=net(tap.name), net=net(tap.net), component=part_name(tap.component),
        ) for tap in circuit.wire_taps)
        for probe in circuit.probes:
            # Current/power probes target components. Accept their stored name or
            # display label without case sensitivity, then qualify the actual part
            # identity. Leave an unmatched target for downstream validation.
            if probe.kind in {"I", "P"}:
                targets = tuple(part_name(next(
                    (part.name for part in circuit.components if target.casefold() in (
                        part.name.casefold(), part.label.casefold()
                    )), target
                )) for target in probe.targets)
            # Other probes target nets, so resolve wire aliases and translate each
            # target through connector/private-net rules instead of part-name rules.
            else:
                targets = tuple(net(circuit.resolve_wire(target)) for target in probe.targets)
            result.probes.append(replace(probe, targets=targets))
        # The entry alone supplies the combined analysis port. Child ports do
        # not replace that interface; translate the root port's aliases too.
        if index == 0 and circuit.port:
            result.port = tuple(net(circuit.resolve_wire(pin)) for pin in circuit.port)
        # Known quantity keys are merged as shared analysis inputs, without file
        # prefixes. set_known accepts consistent repeats and rejects conflicting
        # values; attribute a conflict to the file currently contributing it.
        for key, value in circuit.known.items():
            try:
                set_known(result.known, key, value)
            except CircuitError as error:
                raise CircuitError(error.message, path=path) from None
    # After connector linking, named return domains may connect differently.
    # Recompute ideal op-amp output returns on the combined graph when
    # non-global grounds exist, rather than retaining a per-file guess.
    if any(part.kind == "O" for part in result.components) and result.grounds != {"0"}:
        compiler = Compiler(result.mode)
        compiler.circuit = result
        compiler.connect_amp_returns()
    # A render-only merge still needs something drawable: a component or
    # a retained method/package instance. Definitions alone create no drawing.
    if render_only and not (result.components or result.instances):
        raise CircuitError("Add components to the entry file or an included circuit.")
    return result
