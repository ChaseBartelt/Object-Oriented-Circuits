"""Elaborate static digital assembly into a hygienic, behavior-only top module.

The electrical compiler is unchanged. Main expands compile-time control flow;
chip calls create independent signal scopes. Behavioral if/clock blocks are
copied intact and compiled by the digital exporter, never executed here.
"""
# Each chip invocation receives private signal names so nested instances cannot capture each
# other's state. Static main assembly is expanded here, while runtime clock/if behavior is
# left for the HDL compiler and simulator.

import re
from dataclasses import dataclass

from circuitlab.language.control_flow import ExpansionBudget, expand_control_flow, expand_digital_loops
from circuitlab.language.digital_arrays import lower_digital_arrays
from circuitlab.language.digital_argument_lists import expand_argument_lists
from circuitlab.language.digital_constants import normalize_integer_constants
from circuitlab.language.digital_clear import expand_clear_blocks
from circuitlab.language.digital_switch import expand_switches, normalize_switch_labels
from circuitlab.language.digital_declarations import normalize_port_blocks, normalize_wire_declarations
from circuitlab.language.pin_banks import expand_pin_banks
from circuitlab.language.math_blocks import digital_math_rows
from circuitlab.language.syntax import CHIP, DIGITAL_MAIN, INCLUDE, source_rows
from circuitlab.language.thumb_rom import lower_thumb_roms
from circuitlab.model.graph import CircuitError, DigitalInstance


DECLARATION = re.compile(r"(.+?)\s*=\s*(input|output|wire)", re.I)
CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)")
IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
# Bound total invocations and recursive chip-call depth independently.
# These limits concern assembly hierarchy; nested behavioral braces have
# their own depth limit while definitions are read.
MAX_INSTANCES = 1024
MAX_DEPTH = 32


# Store a normalized definition once, then instantiate it many times.
# Its body still uses local names; each invocation creates a new mapping
# rather than modifying this shared definition.
@dataclass
class _Chip:
    name: str
    # Formal ports in header order, including separately expanded bank pins.
    # body rows retain (text, original_line, origin) during normalization.
    pins: tuple
    body: list
    line: int
    # signals maps local names to shape: None for scalar, an integer for a
    # sized vector. directions also classifies internal wires/state, not only
    # formal input/output pins.
    signals: dict
    directions: dict


def _normalize_declarations(rows):
    """Expand scalar bank declarations before interpreting expression selectors."""
    result = []
    # Expand only the LHS of explicit bank declarations, preserving row origin.
    # SW[3:0]=input becomes individual scalar declarations; data[4]=input
    # remains one vector because its bracket contents contain no colon.
    for text, line, origin in rows:
        if (match := DECLARATION.fullmatch(text)) and ":" in match[1]:
            text = f"{expand_pin_banks(match[1], line)} = {match[2]}"
        result.append((text, line, origin))
    return result


# Call ranges mean ordered numbered scalar pins, not vector slicing.
# A declared vector must be connected by its bare name so its complete
# width/shape can be checked against one formal port.
def _call_arguments(raw, signals, line):
    for argument in raw.split(","):
        selector = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\[[^]]*:[^]]*\]\s*", argument)
        if selector and selector[1] in signals:
            raise CircuitError("Chip argument ranges list numbered scalar pins; pass a declared vector by name instead of a slice.", line)
    # Expand ranges before matching argument count, keeping their written
    # descending order. An empty argument list remains an empty tuple.
    expanded = expand_pin_banks(raw, line) if raw.strip() else ""
    return tuple(argument.strip() for argument in expanded.split(",")) if expanded else ()


def _declarations(rows):
    # Import lazily: the exporter invokes this module after defining validators.
    from circuitlab.exporters.verilog import _signal_list, _state
    # Collect explicit top-level directions first, then identify remaining
    # state/constant declarations. This lets assignments refer to a wire or
    # port declared later without being mistaken for storage.
    signals, kinds, states = {}, {}, {}
    depth, state_rows = 0, []
    for text, line, _ in rows:
        if depth == 0:
            if match := DECLARATION.fullmatch(text):
                for name, width in _signal_list(match[1], line).items():
                    if name in signals:
                        raise CircuitError(f'Signal "{name}" is already declared.', line)
                    signals[name], kinds[name] = width, match[2].lower()
            elif not text.endswith("{") and not CALL.fullmatch(text):
                state_rows.append((text, line))
        depth += int(text.endswith("{")) - int(text == "}")
    # These candidates can include ordinary output assignments. Consult the
    # already collected directions before asking the exporter's state validator
    # to interpret a name/width/initializer as stored state.
    for text, line in state_rows:
        declaration = text.removeprefix("const ")
        left = declaration.partition("=")[0].strip()
        name = left.partition("[")[0].strip()
        # Output/wire assignments are behavior, not state declarations.
        # An assignment to a numbered scalar bank is behavior too, even though
        # the bank prefix is not itself a declared signal name.
        bank = "[" in left and any(re.fullmatch(re.escape(name) + r"\d+", signal) for signal in signals)
        if not bank and kinds.get(name) not in {"input", "output", "wire"} and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\[[^]]+\])?", left):
            _state(declaration, (), states, line)
            width, _, vector = states[name]
            # Keep a scalar distinct from an explicitly sized one-bit vector.
            # Constants and registers both count as local state names for assembly;
            # the behavioral exporter retains their actual declaration semantics.
            signals[name], kinds[name] = width if vector else None, "state"
    return signals, kinds


def _normalize_banks(body, signals):
    """Give numbered pin banks explicit names before instance port renaming."""
    from circuitlab.exporters.clocked import SELECTOR, selected_bits
    # Bridge multi-pin bank expressions through ordinary vector wires. Helper
    # names must avoid every existing local name and will later receive the
    # same per-instance renaming as other internal signals.
    helpers, aliases, names, output = [], {}, set(signals), []
    for text, line, origin in body:
        # Expand a child call's bank arguments into explicit scalar identifiers
        # before any port-name substitution can hide the original bank spelling.
        if (call := CALL.fullmatch(text)) and ":" in call[2]:
            text = f"{call[1]}({', '.join(_call_arguments(call[2], signals, line))})"
        # Identify a selected assignment destination, so a bank on the LHS is
        # unpacked from a helper while a bank read on the RHS is packed into one.
        destination = re.match(r"\s*(\w+\s*\[[^]]+\])\s*=(?!=)", text)
        def replace(match):
            # A genuine declared vector keeps its own selector. Only an undeclared
            # prefix whose numbered scalar members exist needs a bank helper.
            if match[1] in signals:
                return match[0]
            # selected_bits validates the range and returns low bit first, which makes
            # helper[0] correspond to the lowest selected numbered pin. A one-pin
            # selection can be replaced directly without a vector helper.
            bits = selected_bits(match[0], signals, line)
            if len(bits) == 1:
                return bits[0][0]
            target = bool(destination and match.start() == destination.start(1))
            # Cache by both selected pins and direction. Reading and writing the same
            # bank need opposite wiring, so they must not share one bidirectional helper.
            key = tuple(bits), target
            if key not in aliases:
                name = f"ooc_bank_{len(aliases)}"
                # A user may already own ooc_bank_N. Add an ooc_ prefix until this helper
                # is unique, including against helpers allocated earlier in this pass.
                while name in names:
                    name = "ooc_" + name
                names.add(name)
                aliases[key] = name
                helpers.append((f"{name}[{len(bits)}] = wire", line, origin))
                # A read drives helper[bit] from the scalar pin; a write drives the pin
                # from helper[bit]. The original selected expression then uses the helper
                # as one vector, with the original source line attached to these new rows.
                for bit, (pin, _) in enumerate(bits):
                    assignment = f"{pin} = {name}[{bit}]" if target else f"{name}[{bit}] = {pin}"
                    helpers.append((assignment, line, origin))
            return aliases[key]
        output.append((SELECTOR.sub(replace, text), line, origin))
    # Prepend normal declarations/assignments for the helper network. Later
    # declaration analysis and module compilation treat them as ordinary wires.
    return helpers + output


def _definitions(source, *, require_main=True):
    from circuitlab.exporters.verilog import _name, _pin_list
    rows = list(source_rows(source, legacy_markers=False))
    # Leave old chip-only programs entirely on their existing compiler path.
    # Only an assembly main opts into this elaborator by default. Interface
    # previews can parse declarations without resolving their dependencies.
    if require_main and not any(DIGITAL_MAIN.fullmatch(text) for text, _, _ in rows):
        return None
    chips, main = {}, None
    index = 0
    while index < len(rows):
        text, line, _ = rows[index]
        if not require_main and INCLUDE.fullmatch(text):
            index += 1
            continue
        chip, assembly = CHIP.fullmatch(text), DIGITAL_MAIN.fullmatch(text)
        # In this digital assembly path, top-level content is chip definitions plus
        # one main. Electrical components are handled by the separate electrical
        # compiler rather than being mixed into generated behavioral HDL.
        if not chip and not assembly:
            raise CircuitError("Digital assembly contains chip definitions and one main block; electrical parts cannot be mixed in.", line)
        index += 1
        body, depth = [], 1
        # Extract the block body without its outer closing brace. Preserve nested
        # if/clock/switch rows and their line numbers while bounding brace depth.
        while index < len(rows) and depth:
            row = rows[index]
            depth += int(row[0].endswith("{")) - int(row[0] == "}")
            if depth > 64:
                raise CircuitError("Digital blocks may nest at most 64 levels.", row[1])
            if depth:
                body.append(row)
            index += 1
        if depth:
            raise CircuitError("Close the digital assembly block with }.", line)
        # Main may have an explicit top-module name; otherwise use main. Save its
        # raw body for static generation after all chip definitions are available.
        if assembly:
            if main is not None:
                raise CircuitError("Use one main block.", line)
            main = (_name(assembly[1] or "main", line), body, line)
        else:
            name = _name(chip[1], line)
            if name in chips:
                raise CircuitError(f'Chip "{name}" is already defined.', line)
            pins = _pin_list(chip[2], line)
            # Loop/switch lowering uses text/line pairs. Restore origin fields by the
            # retained source line when converting those generated rows back to triples.
            body = normalize_integer_constants(lower_thumb_roms(digital_math_rows(body)))
            origins = {number: origin for _, number, origin in body}
            # First unroll chip-local static for loops and lower digital switches.
            # Signal-dependent if/clock behavior remains for the behavioral compiler;
            # it is not executed using main's static variable environment.
            body = [(text, number, origins[number]) for text, number in expand_switches(expand_digital_loops(
                normalize_switch_labels([(text, number) for text, number, _ in body])))]
            body = normalize_port_blocks(body)
            body = expand_argument_lists(body, pins)
            body = expand_clear_blocks(body)
            body = lower_digital_arrays(body, pins)
            # Normalize explicit bank declarations and inferred sized wires before
            # building local signal tables. Bank expression helpers add declarations,
            # so collect the tables again after their insertion.
            body = normalize_wire_declarations(_normalize_declarations(body), pins)
            signals, kinds = _declarations(body)
            body = _normalize_banks(body, signals)
            signals, kinds = _declarations(body)
            # Header and declared I/O must match exactly; internal wires/state are
            # separate local identities and cannot silently become extra formal ports.
            declared = {p for p, kind in kinds.items() if kind in {"input", "output"}}
            if declared != set(pins):
                raise CircuitError(f'Chip "{name}" must declare every header pin as input or output, with no extra ports.', line)
            chips[name] = _Chip(name, pins, body, line, signals, kinds)
    return chips, main


def digital_library_interfaces(source):
    """Describe structural chip ports without inventing a runnable assembly.

    Return the exporter's module metadata shape, with empty HDL, only for a
    chip-only document containing structural calls. Dependencies are left for
    an including main document to resolve; the preview describes interfaces.
    """
    rows = list(source_rows(source, legacy_markers=False))
    if any(DIGITAL_MAIN.fullmatch(text) for text, _, _ in rows):
        return None
    containers, in_chip, structural = [], False, False
    for text, _, _ in rows:
        if not containers:
            in_chip = CHIP.fullmatch(text) is not None
        elif (in_chip and CALL.fullmatch(text)
              and all(container == "for" for container in containers[1:])):
            structural = True
        if text.endswith("{"):
            containers.append("for" if re.match(r"for\s*\(", text) else "block")
        elif text == "}" and containers:
            containers.pop()
    if not structural:
        return None
    chips, _ = _definitions(source, require_main=False)
    # A zero-iteration generation loop does not leave a structural child.
    # Runtime if/clock calls remain excluded after ordinary loop expansion.
    structural = False
    for chip in chips.values():
        depth = 0
        for text, _, _ in chip.body:
            structural |= depth == 0 and CALL.fullmatch(text) is not None
            depth += int(text.endswith("{")) - int(text == "}")
    if not structural:
        return None
    return tuple(
        (chip.name, tuple((pin, chip.directions[pin]) for pin in chip.pins),
         tuple(text for text, _, _ in chip.body), "")
        for chip in chips.values()
    )


# Flatten chip calls only after validating their external bindings. Retain instance metadata
# alongside the flattened behavior for live diagrams and internal-signal inspection.
def assemble_digital(source, *, instances=None):
    """Return (top, pins, flattened body rows, line), or None without main.

Optional instance metadata lets the schematic display actual assembled chips.
Source text and saved layouts are never rewritten by elaboration.
"""
    # Import validators lazily: the exporter calls this assembler after its
    # own helpers are defined. Keeping the dependency here avoids an eager
    # module-import cycle between assembly and behavioral compilation.
    from circuitlab.exporters.verilog import _module, _name, _signal_list
    parsed = _definitions(source)
    if parsed is None:
        return None
    chips, (top, main, line) = parsed
    # The flattened top is one new module, not an instance of a same-named
    # definition. Keep its identity distinct from reusable chip types.
    if top in chips:
        raise CircuitError(f'Main name "{top}" conflicts with a chip definition; choose another top name.', line)
    budget = ExpansionBudget()
    # Execute main's generation-time variables/loops/branches first. Its
    # remaining statements must only declare ports/wires or instantiate chips;
    # runtime assignments and clocked behavior belong inside a chip.
    main = _normalize_declarations(expand_argument_lists(normalize_port_blocks(
        expand_control_flow(main, budget=budget, allow_wire_counts=True))))
    widths, kinds, pins, output = {}, {}, [], []
    # Collect all main declarations before connecting calls, preserving I/O
    # declaration order in pins. Main wires are internal nets, so they stay
    # out of the generated module header.
    for text, number, _ in main:
        if match := DECLARATION.fullmatch(text):
            kind = match[2].lower()
            for name, width in _signal_list(match[1], number).items():
                if name in widths:
                    raise CircuitError(f'Signal "{name}" is already declared in main.', number)
                widths[name], kinds[name] = width, kind
                if kind != "wire":
                    pins.append(name)
            output.append((text, number))
        elif not CALL.fullmatch(text):
            raise CircuitError("Main builds hardware: declare input/output/wire signals and call chips. Put signal-dependent if/clock behavior inside a chip.", number)
    if not pins:
        raise CircuitError("Declare main's external input/output signals.", line)
    # Global flattened names start with all main nets. Reserve every generated
    # name against this set so private state cannot collide with an explicit
    # root signal or with another invocation's locals.
    occupied = set(widths)
    count = 0
    validated = set()

    # Validate a definition in its own lexical scope before actual bindings
    # are substituted. Otherwise an undeclared child name could accidentally
    # resolve to a main net, or a local input could become writable merely
    # because it happens to bind to a parent wire.
    def validate_chip(chip):
        # This check depends on the reusable definition, not the caller's actual
        # net names; cache it across repeated instances.
        if chip.name in validated:
            return
        # Validate each definition in its own scope. A missing child name must
        # never accidentally bind to a main signal after flattening. Nested
        # instance outputs act as declared drivers for this local check; their
        # real behavior is checked when that definition is instantiated.
        local_body, depth = [], 0
        for text, number, _ in chip.body:
            call = CALL.fullmatch(text) if depth == 0 else None
            if call:
                if call[1] not in chips:
                    raise CircuitError(f'Unknown behavioral chip "{call[1]}".', number)
                child = chips[call[1]]
                arguments = _call_arguments(call[2], chip.signals, number)
                if len(arguments) != len(child.pins):
                    raise CircuitError(f'{child.name} expects {len(child.pins)} signal arguments.', number)
                for formal, argument in zip(child.pins, arguments):
                    if argument not in chip.signals:
                        raise CircuitError(f'{child.name}: undeclared local signal "{argument}".', number)
                    # For this local validation only, a nested child output acts as a driver
                    # of its parent-local destination. _module checks assignment legality and
                    # completeness with this stub; zero is not emitted as the real child logic.
                    if child.directions[formal] == "output":
                        local_body.append((f"{argument} = 0", number))
            else:
                local_body.append((text, number))
            depth += int(text.endswith("{")) - int(text == "}")
        # Run the same behavioral validator used for HDL generation. Actual child
        # connections/widths and implementation are checked during instantiation;
        # the final flattened module is compiled by the exporter afterward.
        _module(chip.name, chip.pins, local_body, chip.line)
        validated.add(chip.name)

    # Rewrite identifier tokens through the local-to-global map rather than
    # replacing substrings. Keep syntax markers and numeric unit suffixes in
    # their recognized positions so legal keyword-like state names can still
    # be renamed when used as signal references.
    def rename(row, mapping):
        def substitute(token):
            word = token[0]
            # A state named clock/every/const remains legal. Rename references,
            # while preserving the same words in their syntax positions.
            prefix, suffix = row[:token.start()], row[token.end():]
            # sevenseg(...) is a built-in expression form; its function spelling is
            # not a signal reference even if a local name has that spelling.
            if word == "sevenseg" and suffix.lstrip().startswith("("):
                return word
            # Preserve leading clock/every call markers and a leading const followed
            # by whitespace. In other recognized reference positions, mapping can
            # rename signals whose names use these same words.
            if not prefix.strip() and (
                (word in {"clock", "every"} and suffix.lstrip().startswith("("))
                or (word == "const" and suffix[:1].isspace())
            ):
                return word
            # A physical-unit word immediately after a numeric value is literal syntax,
            # such as 10 Hz or 5 ms. A same-named signal elsewhere still needs mapping.
            if word in {"Hz", "kHz", "MHz", "GHz", "s", "ms", "us", "ns"} and re.search(r"[0-9]\s*$", prefix):
                return word
            return mapping.get(word, word)
        return IDENTIFIER.sub(substitute, row)

    # Use the main expansion budget for flattened chip rows too. Unrolling
    # many calls cannot bypass the row cap just because each chip is small.
    # Final body rows use the exporter's (text, original_line) format.
    def emit(text, number):
        # Count expanded chip bodies as well as main's generated statements.
        budget.emitted((text, number, None))
        output.append((text, number))

    # context maps names visible in this caller to global flattened nets.
    # A child gets bindings only for its formal ports, plus newly allocated
    # private names for all its other signals. ancestors detects recursive
    # chip types; parent records the optional diagram hierarchy.
    def instantiate(text, number, context, parent=None, ancestors=()):
        nonlocal count
        call = CALL.fullmatch(text)
        name = call[1]
        if name not in chips:
            raise CircuitError(f'Unknown behavioral chip "{name}".', number)
        if name in ancestors or len(ancestors) >= MAX_DEPTH:
            raise CircuitError("Digital chip instances cannot recurse or nest beyond 32 levels.", number)
        chip = chips[name]
        validate_chip(chip)
        # Normalize bank arguments in the caller's scope, then bind positionally
        # against the child's ordered formal pins. Never resolve an undeclared
        # argument by searching unrelated global/private names.
        actual = _call_arguments(call[2], context, number)
        if len(actual) != len(chip.pins):
            raise CircuitError(f'{name} expects {len(chip.pins)} signal arguments.', number)
        mapping = {}
        for formal, argument in zip(chip.pins, actual):
            if argument not in context:
                raise CircuitError(f'{name}: declare signal "{argument}" before connecting it.', number)
            net = context[argument]
            # Compare exact shapes: None denotes scalar and 1 denotes an explicitly
            # sized one-bit vector, so equal bit counts alone do not make them interchangeable.
            if widths[net] != chip.signals[formal]:
                raise CircuitError(f'{name}.{formal}: connection "{argument}" has a different width or scalar/vector shape.', number)
            # A child output may drive only a parent output/wire, not an external
            # input or stored state. The earlier definition-local check also protects
            # a parent's formal input even when its actual root net is a wire.
            if chip.directions[formal] == "output" and kinds[net] not in {"output", "wire"}:
                raise CircuitError(f'{name}.{formal}: connect an output to an output or wire, not "{argument}".', number)
            mapping[formal] = net
        count += 1
        if count > MAX_INSTANCES:
            raise CircuitError("Digital assembly exceeds 1024 chip instances.", number)
        # Use chip type plus the global invocation number for private names.
        # The count follows depth-first instance creation; hierarchy relationships
        # are recorded separately rather than encoded by a full textual path.
        prefix = f"{name}_{count}_"
        # Formal ports already alias caller nets. Allocate each internal wire,
        # constant, state register, and generated helper once for this invocation;
        # repeated calls therefore hold independent internal state.
        for local, width in chip.signals.items():
            if local in mapping:
                continue
            full = prefix + local
            # An explicit main signal may already look like a generated name. Prefix
            # again until unique rather than capturing or overwriting that signal.
            while full in occupied:
                full = "ooc_" + full
            _name(full, number)
            occupied.add(full)
            widths[full], kinds[full] = width, chip.directions[local]
            mapping[local] = full
        own_index = None
        # Keep diagram metadata alongside, not inside, the flattened behavior.
        # Appending the parent before children gives descendants a usable index.
        # signal_bindings exposes every local-to-global identity for live probing;
        # pins/arguments retain the external formal/actual connections.
        if instances is not None:
            own_index = len(instances)
            instances.append(DigitalInstance(
                "chip", name, parent, tuple((p, mapping[p]) for p in chip.pins),
                tuple(row[0] for row in chip.body), line=number,
                arguments=tuple(zip(actual, (mapping[p] for p in chip.pins))),
                signal_bindings=tuple(mapping.items()),
                port_directions=tuple((pin, chip.directions[pin]) for pin in chip.pins),
            ))
        depth = 0
        for row, source_line, _ in chip.body:
            declaration = DECLARATION.fullmatch(row) if depth == 0 else None
            # Child I/O declarations disappear because their ports now alias nets
            # declared in an enclosing scope. Local wire/state declarations remain
            # and are emitted with their allocated private names.
            if declaration and declaration[2].lower() in {"input", "output"}:
                continue
            # Expand only structural calls directly in the chip body. Ordinary nested
            # if/clock contents are copied for behavioral compilation, not conditionally
            # instantiated using signal values.
            if depth == 0 and CALL.fullmatch(row):
                instantiate(row, source_line, mapping, own_index, (*ancestors, name))
                continue
            # Chip-local static for loops were already unrolled. while/do generate
            # hardware in main; they are not runtime behavior supported by this chip path.
            if re.match(r"(?:while|do)\b", row):
                raise CircuitError("Loops generate hardware in main; chip behavior uses if/else and clock/every blocks.", source_line)
            # Preserve the source row number while renaming its signal references.
            # No evaluation of clocked state or signal-dependent conditions occurs here.
            emit(rename(row, mapping), source_line)
            depth += int(row.endswith("{")) - int(row == "}")

    # Main's root context is an identity map of its declared nets, taken before
    # any private instance names are allocated. Calls run in their original
    # expanded-main order while all main declarations are already known.
    context = {name: name for name in widths}
    for text, number, _ in main:
        if CALL.fullmatch(text):
            instantiate(text, number, context)
    if not count:
        raise CircuitError("Main must instantiate at least one behavioral chip.", line)
    # Return behavior for the exporter's final _module compilation, where
    # combined driver coverage and cross-instance combinational feedback can
    # be checked. Source text, saved layouts, and reusable definitions were
    # not rewritten in place to produce this assembled view.
    return top, tuple(pins), output, line
