"""Expand reusable circuit methods before assigning electrical connections."""
# Bindings distinguish electrical node names from component/value parameters at each call.
# Instance-local names and source origins survive expansion, allowing repeated methods to
# simulate independently and still support navigation back to their shared definition.

import re
from dataclasses import dataclass, replace
from circuitlab.language.opamps import MODEL_ASSIGNMENT
from circuitlab.language.properties import PROPERTIES_BRACE, PROPERTY_DECLARATION
from itertools import chain

from circuitlab.language.control_flow import (
    MAX_EXPANDED_ROWS,
    ExpansionBudget,
    expand_control_flow,
    is_control_block,
    is_control_statement,
)
from circuitlab.language.gates import GATE_HELP, gate_accepts, gate_body, gate_call_help, gate_signature
from circuitlab.model.graph import CircuitError, MethodInstance
from circuitlab.math.numbers import scalar
from circuitlab.language.syntax import (
    ASSIGNMENT,
    BATTERY_BRACE,
    CALL,
    DEFAULT_TERMINALS,
    INLINE,
    MAIN,
    MATH_BRACE,
    MATH_INLINE,
    NET_METHODS,
    OP,
    OPAMP_BRACE,
    OVERRIDE,
    PARAMETER_TARGET,
    PART_ARGUMENT,
    PART_PLACEMENT,
    SECTION,
    SWITCH_CALL,
    TRANSISTOR,
    TRANS_CALL,
    UNAVAILABLE_MODEL,
    UNITS,
    Binding,
    SourceRow,
    parse_potentiometer,
    parse_switch,
    parse_wires,
)


# A Method stores a reusable declaration, not an instantiated circuit.
# Each call gets a separate Binding and MethodInstance later; the shared
# body still carries its definition's original lines and source paths.
@dataclass(frozen=True)
class Method:
    """A declaration with its source rows and component or wire bindings."""

    # kind selects flattened circuit/gate/chip expansion or series/parallel
    # group wrapping. parameters preserves positional argument order.
    # targets identifies typed value/component slots; absent entries in a
    # net method are ordinary wire parameters.
    kind: str
    name: str
    parameters: tuple[str, ...]
    body: list[SourceRow]
    targets: dict[str, str]


# Temporarily hide ${...} while recognizing declaration shapes. Braces in
# an interpolation must not prevent a battery/group header or a formal
# component placement from being recognized before static evaluation.
_INTERPOLATION_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


def _bind_gate_call(name, raw, parent):
    """Resolve the gate's full pin list, preserving existing positional calls."""
    # Grouped shorthand is vcc, (input1, input2, ...), output. Flatten that
    # input list for wire parsing, but remember that this syntax explicitly
    # omits ground so it cannot be mistaken for the full positional form.
    grouped = re.fullmatch(r"([^(),]+),\s*\(([^()]*)\)\s*,\s*([^(),]+)", raw)
    try:
        arguments = parse_wires(",".join(grouped.groups()) if grouped else raw)
    except CircuitError:
        raise CircuitError(gate_call_help(name)) from None
    # Resolve every supplied wire in the caller, before creating the callee's
    # private scope. Keep raw-to-resolved pairs for instance argument metadata.
    actual = tuple(map(parent.wire, arguments))
    passed = tuple(zip(arguments, actual))
    # Prefer an existing valid full call: vcc, gnd, inputs..., y. Subtract
    # the three non-input pins when checking the logical input count.
    if not grouped and gate_accepts(name, len(actual) - 3):
        return actual, passed
    # Otherwise try vcc, inputs..., y. An enclosing formal gnd supplies the
    # return when available; absent that binding, use global node 0. The
    # inferred ground is not added to the list of explicitly passed arguments.
    if gate_accepts(name, len(actual) - 2):
        ground = parent.mapping.get("gnd", "0")
        return (actual[0], ground, *actual[1:]), passed
    raise CircuitError(gate_call_help(name))


# Check typed component/value arguments before substituting them. Wire arguments are
# resolved in the caller, while undeclared internal names remain private to the callee.
def _bind_arguments(method, raw, parent):
    """Validate typed arguments and resolve wire arguments in the caller."""
    name, formal, targets = method.name, method.parameters, method.targets
    parts = {}
    # Split positional arguments without separating commas inside component
    # parentheses, such as P(10k,25%). Forward a caller's named value/part
    # parameter by replacing an exact matching name from parent.values.
    arguments = (argument.strip() for argument in re.split(r",(?![^()]*\))", raw))
    actual = tuple(parent.values.get(value, value) for value in arguments)
    # Validate the complete positional count before zipping so a missing or
    # extra argument is not silently dropped during parameter binding.
    if len(actual) != len(formal):
        raise CircuitError(f"{name} expects {len(formal)} arguments.")
    # mapping holds only resolved wire bindings; parts holds parsed component
    # name/value pairs. Typed scalar arguments are validated here and remain
    # in actual for later substitution into the declaration body.
    mapping = {}
    for parameter, value in zip(formal, actual):
        # Net-method parameters default to wires. A declared part slot instead
        # lets the caller choose a component type; a named target such as R1
        # fixes that component identity and accepts its value as the argument.
        target = targets.get(parameter, "wire")
        if target.lower() == "part":
            # A part argument must use a supported component name with an optional
            # parenthesized value, rather than a bare scalar. Keep an omitted value
            # as empty text for downstream component handling.
            component = PART_ARGUMENT.fullmatch(value)
            if not component:
                raise CircuitError(
                    f'{name}: argument "{parameter}" expects a resistor, capacitor, inductor, AC impedance, potentiometer, or switch such as R(1k), Z(2-j2), P(10k,25%), or S(closed).'
                )
            part_name, part_value = component.groups()
            part_value = part_value or ""
            try:
                # Validate potentiometer and switch syntax with their specialized parsers.
                # For other supplied component values, use the type's SI unit parser;
                # these checks do not replace the source spelling used during expansion.
                if part_name[0].upper() == "P":
                    parse_potentiometer(part_value)
                elif part_name[0].upper() == "S":
                    parse_switch(part_value)
                elif part_value.strip():
                    scalar(part_value, UNITS[part_name[0].upper()])
            except CircuitError as error:
                raise CircuitError(
                    f'{name}: argument "{parameter}" for {part_name}. {error}'
                ) from None
            # Give anonymous type-only choices a name derived from their formal slot:
            # R(1k) passed to first becomes R_first. Later invocation scoping keeps
            # that generated component distinct across repeated method calls.
            if len(part_name) == 1:
                part_name = f"{part_name}_{parameter}"
            parts[parameter] = part_name, part_value
        # Require exactly one wire name and resolve it in the parent binding.
        # This keeps an explicitly passed local caller net connected to the
        # callee instead of accidentally creating a new private callee net.
        elif target.lower() == "wire":
            mapping[parameter] = parent.wire(parse_wires(value, 1)[0])
        else:
            try:
                # A fixed switch target accepts a state expression; other fixed targets
                # accept a scalar in their component type's unit. Wrap parse failures
                # with the method, formal parameter, and target name for useful errors.
                if target[0].upper() == "S":
                    parse_switch(value)
                else:
                    scalar(value, UNITS[target[0].upper()])
            except CircuitError as error:
                raise CircuitError(
                    f'{name}: argument "{parameter}" for {target}. {error}'
                ) from None
    return actual, mapping, parts


# Expand calls in execution order and retain instance ancestry, allowing nested packages to
# be drawn independently without changing their flattened electrical behavior.
def expand_main(rows: list[SourceRow], instances: list[MethodInstance], *, imports=()) -> list[SourceRow]:
    """Replace method calls with bound statements in execution order."""
    # Imported definition rows lead the local source, but remain identifiable
    # by this boundary. Collect declarations first so their file position
    # does not restrict forward calls from main or from another method.
    import_count = len(imports)
    rows = [*imports, *rows]
    # Remember whether any input rows use static control flow. The final
    # expanded-row guard is enabled for that path; legacy sources without
    # these features retain their existing expansion behavior.
    uses_control_flow = any(is_control_statement(text) for text, _, _ in rows)
    # There is at most one executable main block in this document expansion.
    # Definition-only imports have already been selected by the include layer.
    starts = [i for i, (text, _, _) in enumerate(rows) if MAIN.fullmatch(text)]
    if len(starts) > 1:
        raise CircuitError("Use one main block.", rows[starts[1]][1])

    # Find a section's matching closing row while checking permitted nested
    # blocks. Keep opener indexes so an unclosed block can report its own
    # source line rather than the end of the document.
    def end(start, name):
        openings = [start]
        for i in range(start + 1, len(rows)):
            text, line, _ = rows[i]
            # Inside a method, annotations may patch behavior/settings only;
            # replacement gate declarations remain a top-level construct.
            if OVERRIDE.fullmatch(text):
                following = rows[i + 1][0] if i + 1 < len(rows) else ""
                if not (OPAMP_BRACE.fullmatch(following) or MATH_BRACE.fullmatch(following)
                        or MATH_INLINE.fullmatch(following)):
                    raise CircuitError("Put @Override above a top-level gate definition or a model block.", line)
            # An exact closing row ends the innermost accepted block. Return only
            # when the original section opener has also been closed.
            if text == "}":
                openings.pop()
                if not openings:
                    return i
            elif text.endswith("{"):
                # Allow static-control blocks and anonymous groups/battery/transistor
                # bodies inside a method. Named section declarations must remain top-level;
                # replace interpolation placeholders only for this shape recognition.
                shape = _INTERPOLATION_PLACEHOLDER.sub("_", text)
                if PROPERTIES_BRACE.fullmatch(shape) and MATH_BRACE.fullmatch(rows[openings[-1]][0]):
                    openings.append(i)
                    continue
                if is_control_block(text) or any(
                    header.fullmatch(shape) for header in (INLINE, BATTERY_BRACE, TRANSISTOR, OPAMP_BRACE, MATH_BRACE)
                ):
                    openings.append(i)
                    continue
                raise CircuitError(
                    "Define named sections at the top level. Call them from inside main or a circuit method.",
                    line,
                )
        # End-of-input means the innermost remaining opener was never closed.
        # Use a recognized block label where available, or the section name.
        opener = rows[openings[-1]]
        block = (INLINE.fullmatch(opener[0]) or BATTERY_BRACE.fullmatch(opener[0])
                 or TRANSISTOR.fullmatch(opener[0]) or OPAMP_BRACE.fullmatch(opener[0]))
        label = block[1] if block else name
        raise CircuitError(f'Close "{label}" with }}.', opener[1])

    # Collect definitions before expanding any call so forward references work.
    # With no main, place the boundary at end-of-input: all ordinary source
    # rows belong to the before section and the main slice will be empty.
    start = starts[0] if starts else len(rows)
    stop = end(start, "main") if starts else start
    # definitions uses case-insensitive method names. overrides additionally
    # keys by full gate pin count so one arity can be replaced independently.
    # counts is populated during calls to give sibling instances unique names.
    definitions, overrides, counts = {}, {}, {}
    chip_patches = {}

    def model_patch(body):
        """A chip override may modify models, never its physical connections."""
        depth = 0
        for text, line, origin in body:
            if depth:
                if text == "}":
                    depth -= 1
                elif text.endswith("{") and depth == 1 and PROPERTIES_BRACE.fullmatch(text):
                    depth += 1
                elif text.endswith("{"):
                    raise CircuitError("Chip property overrides cannot add topology or nested blocks.", line, path=origin.path)
                elif (not MODEL_ASSIGNMENT.fullmatch(text) and not PROPERTY_DECLARATION.fullmatch(text)
                      and not re.match(r"(?:V\s*\(.*\)|let\s+[A-Za-z][A-Za-z0-9_]*)\s*=", text)):
                    raise CircuitError("Use model property/equation assignments in a chip override.", line, path=origin.path)
            elif OPAMP_BRACE.fullmatch(text) or MATH_BRACE.fullmatch(text):
                depth = 1
            elif not (OVERRIDE.fullmatch(text) or MATH_INLINE.fullmatch(text)):
                raise CircuitError("A chip override may change analog/digital models, not pins or connections.", line, path=origin.path)
        return [(text, line, replace(origin, model_patch=True)) for text, line, origin in body]
    # Remove reusable definitions from executable rows, while retaining the
    # ordinary statements on either side of main in their original order.
    before, after = [], []
    i = 0
    while i < len(rows):
        # Skip main's whole body during declaration collection; its statements
        # are expanded only after all definitions have been registered.
        if i == start:
            i = stop + 1
            continue
        text, line, origin = rows[i]
        annotation = None
        # Consume the annotation and inspect the immediately following row.
        # Remember the annotation's line for errors such as a missing declaration.
        if OVERRIDE.fullmatch(text):
            annotation = line
            i += 1
            if i < len(rows):
                text, line, origin = rows[i]
            else:
                text = ""
        match = SECTION.fullmatch(text)
        if annotation and (OPAMP_BRACE.fullmatch(text) or MATH_BRACE.fullmatch(text) or MATH_INLINE.fullmatch(text)):
            j = end(i, "model") if text.endswith("{") else i
            (before if i < start else after).extend(rows[i - 1:j + 1])
            i = j + 1
            continue
        if annotation and (not match or match[1].lower() not in {"gate", "chip"}):
            raise CircuitError("Put @Override directly above a gate definition, chip property patch, or model block.", annotation)
        if annotation and match[1].lower() == "chip":
            if match[3] is not None:
                raise CircuitError("A chip override inherits the original pins; omit its parameter list.", line, path=origin.path)
            j = end(i, match[2])
            chip_patches.setdefault(match[2].casefold(), []).extend(model_patch(rows[i + 1:j]))
            i = j + 1
            continue
        # Locally declared reusable net methods and implicit-terminal named groups
        # need a main block. Imported definitions and annotated gate overrides
        # are exempt; explicit-terminal legacy groups may remain ordinary source.
        if match and not starts and not annotation and i >= import_count and (
            match[1].lower() in NET_METHODS or match[3] is None
        ):
            raise CircuitError(f'Call "{match[2]}" from a main block.', line)
        # Keep non-definition source for the later compiler. In the no-main legacy
        # path this also preserves explicit-terminal local group declarations
        # instead of reinterpreting them as callable methods.
        if not match or (not starts and not annotation and i >= import_count):
            (before if i < start else after).append(rows[i])
            i += 1
            continue
        kind, name, raw = match.groups()
        kind = kind.lower()
        # Circuit/gate/chip bodies flatten directly into statements. Series and
        # parallel declarations instead become structural group wrappers on calls.
        # Lookup is case-insensitive even though displayed names retain spelling.
        net_method = kind in NET_METHODS
        key = name.casefold()
        # Reserve built-in op-number names for amplifier calls and reject duplicate
        # ordinary definitions. Separate checks below govern gate override conflicts.
        if OP.fullmatch(name) or (not annotation and key in definitions):
            raise CircuitError(
                f'Section name "{name}" is reserved or already defined.', line, path=origin.path
            )
        # An omitted parameter list uses conventional a,b terminals. An explicitly
        # empty list remains empty, allowing a zero-argument circuit method.
        if raw is None:
            pins = DEFAULT_TERMINALS
        else:
            pins = tuple(part.strip() for part in raw.split(",")) if raw.strip() else ()
        # An override must name a real built-in gate and use a supported full
        # wire-pin count: supply, ground, logical inputs, output. Multiple valid
        # arities may coexist, but not duplicate arities or an ordinary definition
        # of that same gate name.
        if annotation:
            if key not in GATE_HELP:
                raise CircuitError(f'No built-in gate named "{name}" to override.', line)
            if not gate_accepts(key, len(pins) - 3):
                raise CircuitError(f"Use gate {key}({gate_signature(key)}).", line)
            if (key, len(pins)) in overrides:
                raise CircuitError(f'Gate "{name}" already has an override for these pins.', line)
            if key in definitions:
                raise CircuitError(f'Gate "{name}" already has an unannotated definition.', line)
        # Check the reverse declaration order too: an ordinary definition cannot
        # be added after an override for that name has already been registered.
        elif any(gate == key for gate, _ in overrides):
            raise CircuitError(f'Gate "{name}" already has an @Override definition.', line)
        # Retain only body rows, excluding the reusable declaration's outer braces.
        # Their source locations remain unchanged when the method is later called.
        j = end(i, name)
        child = rows[i + 1 : j]
        targets = {}
        # In a net method, a formal used as the component before between(...) is
        # a part slot. Other formals remain wires. Hide interpolation placeholders
        # during this scan so generated endpoint/value syntax does not obscure
        # an otherwise recognizable formal component placement.
        if net_method:
            targets = {
                placement[1]: "part"
                for statement, _, _ in child
                if (placement := PART_PLACEMENT.fullmatch(_INTERPOLATION_PLACEHOLDER.sub("_", statement)))
                and placement[1] in pins
            }
        # Parameterized series/parallel methods declare their slots with assignments
        # such as resistance=R1, tap=wire, or element=part. Record only assignments
        # whose left side is a header parameter and whose target is supported.
        elif raw is not None:
            for statement, body_line, _ in child:
                assignment = ASSIGNMENT.fullmatch(statement)
                if not assignment or assignment[1] not in pins:
                    continue
                parameter, target = assignment.groups()
                if target.lower() in {"wire", "part"} or PARAMETER_TARGET.fullmatch(
                    target
                ):
                    # Each structural-method formal has one declaration of its role.
                    # A second declaration would make value substitution ambiguous.
                    if parameter in targets:
                        raise CircuitError(
                            f'Bind parameter "{parameter}" once.', body_line
                        )
                    targets[parameter] = target
        # Require unique identifier parameters. Untyped series/parallel methods
        # are two-terminal groups; typed structural methods and net methods can
        # have other arities under their respective binding rules.
        if (
            len(set(pins)) != len(pins)
            or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in pins)
            or (not net_method and not targets and len(pins) != 2)
        ):
            requirement = "distinct" if net_method or targets else "two distinct"
            raise CircuitError(
                f'Use {requirement} parameter names in the header of "{name}".', line, path=origin.path
            )
        # Once a structural method uses typed slots, every formal needs a role.
        # Net methods instead default any undeclared slot to a wire parameter.
        if not net_method and targets and targets.keys() != set(pins):
            missing = ", ".join(p for p in pins if p not in targets)
            raise CircuitError(f'Bind these parameters in "{name}": {missing}.', line)
        # A reusable net method needs at least one body row. Gate overrides must
        # use only wire pins, so they cannot turn a built-in input into a part slot.
        if net_method and j == i + 1:
            raise CircuitError(f'{kind.capitalize()} method "{name}" needs a body.', line)
        if annotation and targets:
            raise CircuitError("Use wire pins in a gate override.", line)
        # Register the unexpanded declaration once. Body control flow, actual
        # arguments, and private instance names are resolved separately per call.
        method = Method(kind, name, pins, child, targets)
        if annotation:
            overrides[key, len(pins)] = method
        else:
            definitions[key] = method
        i = j + 1

    for key in chip_patches:
        if key not in definitions or definitions[key].kind != "chip":
            raise CircuitError(f'Load the original chip "{key}" before overriding its properties.')

    # Share static-expansion work limits across main and every nested method
    # invocation. Per-call variable environments remain separate even though
    # all invocations charge against this one compilation budget.
    budget = ExpansionBudget()

    # Yield bound source rows lazily in execution order. parent contains this
    # invocation's wire/value mappings; active is the current call ancestry,
    # not a global visited set, so repeated sibling calls remain legal.
    def expand(body, parent, active, *, top_level=False):
        opaque_depth = 0
        for text, line, origin in body:
            # Use each body row's source file while retaining the current invocation's
            # scope and bindings. Imported method bodies therefore report their own
            # definition file, while call-site metadata can still identify the caller.
            if parent.path != origin.path or parent.model_patch != origin.model_patch:
                parent = replace(parent, path=origin.path, model_patch=origin.model_patch)
            # Device/math bodies own every nested properties block and local
            # declaration. Do not interpret `let` there as static control flow
            # or stop passthrough at the first inner closing brace.
            if opaque_depth or any(header.fullmatch(text) for header in (TRANSISTOR, OPAMP_BRACE, MATH_BRACE)):
                yield text, line, parent
                opaque_depth += text.count("{") - text.count("}")
                continue
            # Static control flow needs an invocation context: main or a circuit
            # method. It cannot execute among ordinary top-level electrical rows.
            if top_level and is_control_statement(text):
                raise CircuitError(
                    "Put compile-time control flow inside main or a circuit method.",
                    line,
                    path=origin.path,
                )
            # Only whole call-shaped statements are candidates for method expansion.
            # Leave component placements, assignments, braces, and built-in opamp
            # calls for electrical construction rather than interpreting them here.
            call = CALL.fullmatch(text.removesuffix(";").rstrip())
            if not call or OP.fullmatch(call[1]):
                yield text, line, parent
                continue
            key = call[1].casefold()
            # Ordinary recursion identity is the method name. A built-in/overridden
            # gate will refine this to (name, full_pin_count), matching its override
            # lookup. passed stores explicit raw-to-resolved wire argument pairs.
            method_key = key, None
            gate_wires = None
            builtin_gate = False
            passed = ()
            # An ordinary user definition takes precedence over built-in gate fallback.
            # Otherwise normalize gate shorthand first, then choose an override for
            # that exact arity or synthesize the default transistor implementation.
            if key in GATE_HELP and key not in definitions:
                try:
                    gate_wires, passed = _bind_gate_call(key, call[2], parent)
                except CircuitError as error:
                    raise CircuitError(error.message, line, path=parent.path) from None
                # arity includes vcc, gnd, and output. The gate-body generator takes only
                # the remaining logical input count. Generated rows use the call's line
                # and origin because they have no separate user definition location.
                arity = len(gate_wires)
                method_key = key, arity
                method = overrides.get(method_key)
                if method is None:
                    # Mark only synthesized defaults as built-in implementations. A matching
                    # user override remains an ordinary supplied body in instance metadata.
                    builtin_gate = True
                    pins, statements = gate_body(key, arity - 3)
                    child = [(statement, line, parent) for statement in statements]
                    method = Method("gate", key, pins, child, {})
            # Outside main, ordinary call-shaped rows pass to the electrical compiler.
            # Inside a method, known transistor/switch shorthand also passes through;
            # other unresolved section names are reported at their actual call site.
            elif top_level or key not in definitions:
                if top_level or TRANS_CALL.fullmatch(call[1]) or SWITCH_CALL.fullmatch(call[1]):
                    yield text, line, parent
                    continue
                raise CircuitError(f'Unknown section "{call[1]}".', line, path=parent.path)
            else:
                method = definitions[key]
            kind, name = method.kind, method.name
            net_method = kind in NET_METHODS
            # Work with this selected definition, whether imported, overridden, or
            # generated. Do not mutate its stored body when substituting one call.
            formal, child, targets = method.parameters, method.body, method.targets
            # Reject direct or indirect recursion along the current call chain.
            # Keeping gate arity in the key lets distinct override variants call one
            # another unless the chain eventually returns to the same variant.
            if method_key in active:
                raise CircuitError(
                    f'Circuit method "{name}" calls itself recursively.', line, path=parent.path
                )
            parts = {}
            try:
                # Typed methods validate value/part slots and resolve only their wire
                # slots. Pure-wire methods parse an exact pin count; built-in gate calls
                # reuse the normalized list that already includes any inferred ground.
                if targets:
                    actual, mapping, parts = _bind_arguments(method, call[2], parent)
                else:
                    if gate_wires is None:
                        raw_wires = parse_wires(call[2], len(formal))
                        actual = tuple(map(parent.wire, raw_wires))
                        # Keep both source spelling and caller-resolved net for wire-call metadata.
                        # The formal-to-actual mapping below is what scopes electrical rows.
                        passed = tuple(zip(raw_wires, actual))
                    else:
                        actual = gate_wires
                    mapping = dict(zip(formal, actual))
            # Attach argument failures to the call, not the shared definition. Typed
            # parsers already explain the offending slot; pure-wire calls get a
            # signature message appropriate to a net method or a two-terminal group.
            except CircuitError as error:
                if targets:
                    raise CircuitError(error.message, line, path=parent.path) from None
                message = (
                    f"{name} expects {len(formal)} wire arguments."
                    if net_method
                    else f"Use {name}(inputWire, outputWire); with two wire names."
                )
                raise CircuitError(message, line, path=parent.path) from None
            # Sibling calls get distinct scopes even when they share a definition.
            # Count calls by enclosing scope and case-insensitive method name. Nest
            # the new part scope inside the caller so repeated and nested invocations
            # cannot share an internal component or undeclared local wire accidentally.
            instance = parent.scope, key
            counts[instance] = counts.get(instance, 0) + 1
            scope = parent.part(f"{name}_{counts[instance]}")
            # Wire formals live in mapping; the remaining formal arguments stay as
            # source-text values for typed substitution and forwarding to nested calls.
            values = {p: v for p, v in zip(formal, actual) if p not in mapping}
            # Append the hierarchy record before expanding its children. Its list
            # index becomes their parent/owner identity even though electrical rows
            # will ultimately be flattened into one circuit graph.
            index = len(instances)
            instances.append(
                MethodInstance(kind, name, parent.instance, tuple(mapping.items()),
                               line=line, arguments=passed, path=parent.path,
                               builtin_gate=builtin_gate,
                               # A chip whose sole body row is model=unavailable retains its package
                               # instance and pins for rendering, but has no executable model to expand.
                               model_available=not (
                                   kind == "chip" and len(child) == 1
                                   and UNAVAILABLE_MODEL.fullmatch(child[0][0])
                               ))
            )
            # Stop at that package boundary without inventing electrical components.
            # Analysis/export code can later reject the retained unavailable model.
            if not instances[-1].model_available:
                continue
            if kind == "chip" and key in chip_patches:
                child = [*child, *chip_patches[key]]
            # Create a fresh callee binding. Explicit wire mappings connect to the
            # caller; all other local names are qualified by scope downstream.
            # Only typed calls made in main inherit its implicit-chain context.
            binding = Binding(
                scope, mapping, in_main=bool(targets and parent.in_main),
                values=values, instance=index
            )
            # Evaluate static loops/branches for this invocation before substituting
            # its typed declarations and recursively expanding calls. The control-flow
            # helper starts fresh compile-time variables but uses the shared budget.
            child = expand_control_flow(child, budget=budget)
            # Rewrite a new per-call list, preserving every row's original line and
            # source binding. Untyped methods can keep the control-flow iterator.
            if targets:
                expanded_child = []
                for statement, body_line, original in child:
                    assignment = ASSIGNMENT.fullmatch(statement)
                    placement = PART_PLACEMENT.fullmatch(statement)
                    # Replace a net-method part slot with the caller's component name/value.
                    # Leave endpoint text in callee form; the invocation binding resolves
                    # those wires when the electrical builder reads the emitted placement.
                    if net_method and placement and placement[1] in parts:
                        part, value = parts[placement[1]]
                        statement = f"{part}({value}) between({placement[2]})"
                    # In a structural method, rewrite only the assignment declaring a typed
                    # formal's recorded role. Other assignments in the body remain ordinary
                    # electrical source statements rather than accidental substitutions.
                    elif (
                        not net_method
                        and assignment
                        and targets.get(assignment[1]) == assignment[2]
                    ):
                        parameter, target = assignment.groups()
                        # A wire formal marks a tap at this position in the group; the binding
                        # maps that formal back to the actual caller wire. Component slots become
                        # ordinary assignments with either the chosen part or the fixed target.
                        if target.lower() == "wire":
                            statement = f"wire = {parameter}"
                        else:
                            part, value = parts.get(
                                parameter, (target, values[parameter])
                            )
                            statement = f"{part} = {value}"
                    # Retain provenance from the original body, not from the rewritten text.
                    # This lets errors and editor navigation return to the shared definition.
                    expanded_child.append((statement, body_line, original))
                child = expanded_child
            # Series/parallel calls need a structural wrapper for downstream topology.
            # Typed methods use an anonymous group; untyped two-terminal methods emit
            # their scoped name and explicit actual endpoints. Circuit/gate/chip bodies
            # flatten directly, with hierarchy preserved separately in instances.
            if not net_method:
                header = (
                    f"{kind} {{"
                    if targets
                    else f"{kind} {scope}({', '.join(actual)}) {{"
                )
                yield header, line, binding
            # Expand nested calls with this invocation in the recursion ancestry,
            # then close any structural wrapper using the call's line and binding.
            yield from expand(child, binding, (*active, method_key))
            if not net_method:
                yield "}", line, binding

    # Preserve execution order: ordinary rows before main, main after its
    # static-control expansion, then ordinary rows after main. Main itself
    # has no private method scope, and each surrounding region is top-level.
    expanded = chain(
        expand(before, Binding(), (), top_level=True),
        expand(expand_control_flow(rows[start + 1 : stop], budget=budget), Binding(in_main=True), ()),
        expand(after, Binding(), (), top_level=True),
    )
    # Consume the lazy generator directly on the legacy path. Iteration is
    # what performs nested expansion and fills the supplied instances list.
    if not uses_control_flow:
        return list(expanded)

    # With static features present, also cap the fully flattened output.
    # Method calls and structural wrappers can add rows beyond those charged
    # by the control-flow emitter, so check the final statement stream too.
    result = []
    for row in expanded:
        # Permit at most the configured number of rows. The first extra row
        # supplies its own original line/file for the expansion-limit diagnostic.
        if len(result) >= MAX_EXPANDED_ROWS:
            _, line, origin = row
            raise CircuitError(
                f"Compile-time control flow exceeded {MAX_EXPANDED_ROWS:,} expanded electrical statements.",
                line,
                path=origin.path,
            )
        result.append(row)
    return result
