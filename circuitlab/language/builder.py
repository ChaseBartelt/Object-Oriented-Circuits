"""Build electrical graphs from expanded OOC statements.

Device/group state and connection finalization live here. Document expansion
and file linking are separate callers, so this module does not import them.
"""
# Parsing collects incomplete sources, groups, and devices before resolving their terminal names.
# Keep graph construction separate from include loading and method expansion; this layer consumes
# already-expanded rows and records the source location of every resulting component.

import re
from dataclasses import dataclass, field, replace
from math import sqrt

from circuitlab.model.graph import (
    Circuit,
    CircuitError,
    ChipProperty,
    Component,
    ModelIssue,
    OpAmpModel,
    ComparatorInstance,
    ComparatorModel,
    AudioAmpModel,
    Probe,
    Pulse,
    WireTap,
    ground_references,
)
from circuitlab.math.equations import solve_known
from circuitlab.language.opamps import MODEL_ASSIGNMENT, ModelSetting, model_parameter, needs_supplies, parameter_name, resolve_model
from circuitlab.language.math_blocks import compile_scope_equations, remap_equation
from circuitlab.language.properties import (PROPERTIES_BRACE, PROPERTY_DECLARATION,
                                           declared_property_value, validate_property_name)
from circuitlab.math.numbers import real, scalar
from circuitlab.math.numbers import set_known as set_known
from circuitlab.math.quantities import QUANTITIES
from circuitlab.language.syntax import (
    AC_VOLTAGE,
    ASSIGNMENT,
    BATTERY_ALIAS,
    BATTERY_BRACE,
    BATTERY_MARKER,
    BJT_MODEL_NAME,
    BJT_PINS,
    BJT_TYPES,
    CALL,
    FREQUENCY,
    INLINE,
    MATH_BRACE,
    MATH_INLINE,
    OP,
    OPAMP_BRACE,
    OPAMP_DECLARATION,
    OVERRIDE,
    PART_PLACEMENT,
    PLACEMENT,
    PULSE,
    SECTION,
    SOURCE_WIRE,
    SWITCH_CALL,
    SWITCH_ON_RESISTANCE,
    SWITCH_OFF_RESISTANCE,
    TRANSISTOR,
    TRANS_CALL,
    TRANS_POSITIONAL,
    UNITS,
    WAVE,
    analysis_mode,
    parse_potentiometer,
    parse_switch,
    parse_wires,
)


# Groups are a parent-first tree stored in circuit.groups. Entries do not
# yet have electrical endpoints: series/parallel topology is assigned in
# elaborate(). line and entry metadata retain their original source locations.
@dataclass
class Group:
    """A series or parallel block before its terminals have been resolved."""

    mode: str
    line: int
    parent: int | None = None
    name: str = ""
    # A two-wire header supplies output directly. A one-wire named header must
    # finish with return wire; returned then forbids more body statements.
    output: str = ""
    returned: bool = False
    # Entries hold (kind, name, value). G and B reference a nested group
    # or battery by index. P reserves a zero-volt current probe.
    # These are group-only tags: P is a current-probe placeholder, unlike
    # Component(kind="P"), which is a three-terminal potentiometer.
    entries: list = field(default_factory=list)
    # Strong junction bindings: (position, wire_name). Position 0 is the input
    # and len(entries) the output; series groups also have interior junctions.
    wires: list = field(default_factory=list)
    braced: bool = False
    # Terminal annotations: (position, tap_name, source_line). They become
    # WireTap records after an actual leaf component/terminal is known.
    taps: list = field(default_factory=list)
    ground: str | None = None
    # A ground declared after a parallel entry changes that preceding branch's
    # return; a group-wide ground is kept separately in ground.
    branch_grounds: dict[int, str] = field(default_factory=dict)
    # Map ordinary/probe entry positions to expanded-row order. compile_program
    # later translates those order numbers into original line/file locations.
    entry_order: dict[int, int] = field(default_factory=dict)
    entry_sources: dict[int, tuple[int, object]] = field(default_factory=dict)


# A battery is a staged source declaration, possibly inside a group.
# Source type, normalized values, and terminals are finalized only when
# its matching brace/marker closes; incomplete blocks keep their opening line.
@dataclass
class Battery:
    """Collect source values and wire names until the battery block closes."""

    label: str
    line: int
    braced: bool
    # Most entries are scalar source quantities. A direct I setting instead
    # stores (AC_or_DC_value, DC_offset, equation), normalized by _source().
    known: dict = field(default_factory=dict)
    wires: list[str] = field(default_factory=list)
    ground: str | None = None
    # Remember use of Vneg independently of its numeric value so a later V
    # or repeated Vneg cannot silently redefine the same supply.
    negative_supply: bool = False


# Normalize explicit Vpk/Apk or Vrms/Arms notation to an RMS phasor.
# Removing only pk/rms leaves the physical unit for scalar() to validate;
# a peak amplitude is divided by sqrt(2), with its derivation retained.
def _ac_source(raw, unit):
    raw = raw.strip()
    basis = re.search(rf"{unit}(pk|rms)$", raw, re.I)
    if not basis:
        return scalar(raw, unit), "Given"
    value = scalar(raw[: -len(basis[1])], unit)
    if basis[1].lower() == "pk":
        return value / sqrt(2), f"{unit}_RMS = {unit}pk / √2"
    return value, "Given"


# Return (stored_value, DC_offset, explanatory_equation) consistently.
# Stored values are RMS phasors in AC/mixed mode; DC returns the same
# parsed value in both slots. Mixed sources separate DC | AC contributions.
def _source(raw, kind, mode, group=False):
    unit = UNITS[kind]
    if mode == "mixed":
        # A grouped mixed-mode source without a separator is treated as DC-only.
        # Explicit node-source syntax otherwise requires both halves to be written.
        if group and "|" not in raw:
            raw += " | 0"
        parts = raw.split("|")
        if len(parts) != 2:
            raise CircuitError(
                "Use DC value | AC value for each source in DC + AC mode."
            )
        dc = scalar(parts[0], unit)
        value, equation = _ac_source(parts[1], unit)
        return value, dc, equation
    if mode == "ac":
        value, equation = _ac_source(raw, unit)
        return value, 0, equation
    value = scalar(raw, unit)
    return value, value, "Given"


def _group_voltage(known, mode, label):
    if mode == "dc":
        if "V" not in known or known["V"].imag:
            raise CircuitError(f"Enter a real V value for {label}.")
        return known["V"], "Given"
    # Voltage inference may reinterpret or augment the staged inputs. Work on
    # a copy so constructing an RMS phasor does not rewrite the caller's givens.
    known = dict(known)
    # A nonnegative real V accompanied by an explicit angle is a polar
    # magnitude. Move it to Vmag before the quantity solver builds V; an
    # already-complex V keeps its rectangular phasor meaning.
    if "V" in known and "phaseV" in known and not known["V"].imag and known["V"] >= 0:
        set_known(known, "Vmag", known.pop("V"))
    # Magnitude/peak-only source declarations default to zero phase here.
    # An explicitly supplied phasor is not assigned a competing default angle.
    if "V" not in known:
        known.setdefault("phaseV", 0)
    # Reuse reversible RMS/peak/phase rules and reject conflicts before making
    # a source. Preserve a readable equation for the report's definition row.
    values, diagnostics, equations = solve_known("ac", known)
    if any("conflict" in d.lower() for d in diagnostics):
        raise CircuitError(f"Voltage for {label}: {' '.join(diagnostics)}")
    if "V" not in values:
        raise CircuitError(f"Enter the voltage for {label}.")
    equation = equations.get("V", "Given")
    if "V" in equations and "Vpk" in known and "Vmag" not in known:
        equation = "V_RMS = (Vpk / √2) ∠ phaseV"
    return values["V"], equation


# Translate each bound row in source order, then attach locations to the final components.
# Connector validation includes schematic-only terminals without adding numerical unknowns.
def compile_program(program, mode, *, render_only=False, model_preview=False):
    """Read elaborated statements and resolve their electrical graph."""
    mode = mode.lower()
    # The document elaborator supplies the text used for mode detection.
    # Includes, method expansion, and static control flow are handled upstream;
    # this function consumes their ordered, bound statements.
    if mode == "auto":
        mode = analysis_mode(program.mode_source)
    if mode not in {"dc", "ac", "mixed", "transient"}:
        raise CircuitError(f"Unknown analysis mode {mode!r}.")
    compiler = Compiler(mode, render_only=render_only or model_preview)
    # Preserve drawing/instance metadata before reading electrical statements.
    # Connector names also count as named nets when choosing graph elaboration.
    connectors = program.connectors
    compiler.circuit.named_wires.update(connectors)
    compiler.circuit.connector_pins = tuple(connectors)
    compiler.circuit.instances = list(program.instances)
    owners, lines = {}, {}
    paths = {}
    # order identifies an expanded statement; line/path identify its original
    # source. binding also maps local wires/part names into the calling instance.
    # Track all three so repeated method instances retain distinct ownership.
    for order, (text, line, binding) in enumerate(program.rows, 1):
        compiler.order = order
        lines[order] = line
        if binding.path is not None:
            paths[order] = binding.path
        if binding.instance is not None:
            owners[order] = binding.instance
        try:
            compiler.read(text, line, binding)
        # Keep an error that already names its source line. Otherwise attach this
        # bound row's original line/file, rather than its position after expansion.
        except CircuitError as error:
            if error.line is not None:
                raise
            raise CircuitError(str(error), line, error.code, path=binding.path) from None
    circuit = compiler.finish(render_only)
    # Unreduced group entries need provenance too, especially for reporting
    # an unspecified value before the group has become explicit components.
    for group in circuit.groups:
        group.entry_sources = {index: (lines[order], paths.get(order))
                               for index, order in group.entry_order.items()}
    # Supply pins on an ideal op amp are real schematic connections even when
    # no source drives them; they do not add unknowns to the signal solver.
    # Validate connectors against all meaningful schematic terminals, not only
    # the solver's node list. Ideal op-amp supply identifiers can be valid pins
    # without creating numerical supply-current or rail-clipping equations.
    schematic_nets = set(circuit.nodes)
    schematic_nets.update(net for part in circuit.components for net in part.terminal_nets)
    for pin, line in connectors.items():
        if circuit.resolve_wire(pin) not in schematic_nets:
            raise CircuitError(f'Connector pin "{pin}" is not a net in this file.', line)
    # Parts retain the row order that created/finalized them. Translate it
    # into per-component ownership and source maps only after construction
    # has expanded groups into their final list of component identities.
    circuit.owners, circuit.component_lines, circuit.component_files = (
        {part.name: origins[part.order] for part in circuit.components if part.order in origins}
        for origins in (owners, lines, paths)
    )
    compiler.bind_comparators()
    compiler.apply_supply_totals()
    return circuit


class Compiler:
    """Collect circuit statements, then resolve groups, taps, and returns."""

    # Keep incomplete blocks separate from completed Circuit components.
    # active is a group index; pending amps/transistors and grouped sources
    # wait for finalization. This "transient" mode means the quantity-based
    # RC/RL model, distinct from running a DC/AC graph in the time simulator.
    def __init__(self, mode, *, render_only=False):
        self.circuit = Circuit(mode)
        self.render_only = render_only
        self.order = 0
        self.mode = mode
        self.active = None
        self.amp = None
        self.battery = None
        self.amps = []
        self.opamp_config = None
        self.model_settings = []
        self.default_declarations = set()
        self.pending_override = False
        self.audios = []
        self.math_block = None
        self.properties_open = None
        self.property_rows = []
        self.math_rows = []
        self.supply_totals = []
        self.transistor = None
        self.transistors = []
        self.sources = []
        self.names = set()
        # Track source-only wrapper groups, detected DC offsets, explicitly bound
        # method parts, and the implicit series chain used by bare blocks in main.
        # These flags determine which connection conventions are valid at finish().
        self.standalone = False
        self.standalone_batteries = []
        self.offset = False
        self.method_parts = False
        self.inline_chain = None

    # Append a parent-first group and a G reference in its parent when nested.
    # Opening a child changes active; closing it restores its saved parent.
    # Header pins name the initial boundary and optional final boundary.
    def start_group(self, mode, line, name="", pins=(), *, braced=False):
        group = Group(mode, line, self.active, name, braced=braced)
        if pins:
            group.wires.append((0, pins[0]))
            self.circuit.named_wires.update(pins)
            if len(pins) == 2:
                group.output = pins[1]
        if self.active is not None:
            self.circuit.groups[self.active].entries.append(
                ("G", "", len(self.circuit.groups))
            )
        self.active = len(self.circuit.groups)
        self.circuit.groups.append(group)

    # Resolve the active index on demand; None means a top-level context.
    # Keeping an index avoids losing parent/child identity as groups accumulate.
    @property
    def group(self):
        return self.circuit.groups[self.active] if self.active is not None else None

    # Dispatch using the active block context before interpreting a statement as a free-standing
    # component. The same token can have different roles inside a battery, group, or device.
    def read(self, text, line, binding):
        """Consume a statement in the current device, source, or group context."""
        text = text.removesuffix(";").rstrip()
        if self.math_block is not None:
            digital, opened, priority = self.math_block
            declaration = PROPERTY_DECLARATION.fullmatch(text)
            if self.properties_open is not None:
                if text == "}":
                    self.properties_open = None
                else:
                    assignment = MODEL_ASSIGNMENT.fullmatch(text)
                    if declaration:
                        validate_property_name(declaration[1])
                        self.property_rows.append((binding, declaration[1], declaration[3], line, priority, declaration[2]))
                        return
                    if not assignment:
                        raise CircuitError("Use name = value inside properties { ... }.")
                    validate_property_name(assignment[1])
                    self.property_rows.append((binding, assignment[1], assignment[2], line, priority))
            elif PROPERTIES_BRACE.fullmatch(text):
                self.properties_open = line
            elif text == "}":
                self.math_block = None
            elif declaration:
                validate_property_name(declaration[1])
                self.property_rows.append((binding, declaration[1], declaration[3], line, priority, declaration[2]))
            elif not digital and (field := MODEL_ASSIGNMENT.fullmatch(text)):
                if parameter_name(field[1]) == "supply_current_total":
                    try:
                        current = real(scalar(field[2], "A"))
                        if current < 0:
                            raise CircuitError("supply.current.total must be nonnegative.")
                        self.supply_totals.append((binding, current, line, priority))
                    except CircuitError as error:
                        if not self.render_only:
                            raise
                        self.defer_model_issue(error.message, line, binding.path, field[1], field[2], owner=binding.instance)
                    return
                if parameter_name(field[1]) in {"bandwidth", "slew"} or field[1].casefold().startswith("supply."):
                    message = f'Anonymous analog model property "{field[1]}" is not yet implemented for simulation.'
                    if not self.render_only:
                        raise CircuitError(message, line, path=binding.path)
                    self.defer_model_issue(message, line, binding.path, field[1], field[2], owner=binding.instance)
                else:
                    self.add_equation(text, digital, binding, line, priority)
            else:
                self.add_equation(text, digital, binding, line, priority)
            return
        if self.opamp_config is not None:
            setting = self.opamp_config
            if text == "}":
                self.model_settings.append(setting)
                self.opamp_config = None
            else:
                field = MODEL_ASSIGNMENT.fullmatch(text)
                if not field:
                    raise CircuitError("Use parameter = value inside an analog model declaration.")
                key = parameter_name(field[1])
                if key in setting.origins:
                    raise CircuitError(f'Model parameter "{key.replace("_", ".")}" is already declared.')
                setting.origins[key] = line, binding.path
                setting.raws[key] = field[2]
                try:
                    setting.fields[key] = model_parameter(key, field[2])
                except CircuitError as error:
                    if not self.render_only:
                        raise
                    self.defer_model_issue(error.message, line, binding.path, field[1], field[2], owner=binding.instance)
            return
        if OVERRIDE.fullmatch(text):
            if self.pending_override:
                raise CircuitError("Use one @override annotation per model block.")
            self.pending_override = True
            return
        math_block = MATH_BRACE.fullmatch(text)
        math_inline = MATH_INLINE.fullmatch(text) if not OPAMP_BRACE.fullmatch(text) else None
        if math_block or math_inline:
            if self.group or self.battery or self.amp or self.transistor or self.opamp_config or self.mode == "transient":
                raise CircuitError("Put mathematical behavior in a steady-state circuit, chip, or main.")
            digital = (math_block or math_inline)[1].casefold() == "digital"
            priority = 2 if binding.model_patch else 1 if self.pending_override else 0
            self.pending_override = False
            if math_block:
                self.math_block = digital, line, priority
            else:
                self.add_equation(math_inline[2], digital, binding, line, priority)
            return
        # Configuration blocks own their braces and fields, independently of the
        # legacy (opamp ...) pin-connection block below. Keep declarations scoped
        # by the same binding.part() identity as their connection calls.
        declaration = OPAMP_BRACE.fullmatch(text) or OPAMP_DECLARATION.fullmatch(text)
        if declaration:
            if self.amp or self.battery or self.group or self.transistor or self.mode == "transient":
                raise CircuitError("Declare op amps at the top level or in a circuit method/main.")
            raw = declaration[1].strip()
            raw = raw[1:-1] if raw.startswith("(") else raw
            local = parse_wires(raw)
            if (not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in local)
                    or len({name.casefold() for name in local}) != len(local)):
                raise CircuitError("List distinct component identifiers in an analog model block.")
            targets = tuple(binding.part(name).casefold() for name in local)
            setting = ModelSetting(targets, {}, {}, self.pending_override, binding.model_patch, line, binding.path,
                                   owner=binding.instance, locals=local)
            self.pending_override = False
            if text.endswith("{"):
                self.opamp_config = setting
            else:
                if any(target in self.default_declarations for target in targets):
                    raise CircuitError("Op amp is already declared in this scope.")
                self.default_declarations.update(targets)
                self.model_settings.append(setting)
            return
        if self.pending_override:
            raise CircuitError("Put @override directly above an analog or digital model block.")
        # An open transistor block owns every following field, including its }.
        # Handle it before generic brace/group parsing so its close cannot end
        # a surrounding electrical block by mistake.
        if self.transistor:
            self.transistor_field(text, binding)
            return
        call = CALL.fullmatch(text.rstrip(";").strip())
        amp_name = re.fullmatch(
            r"\(\s*opamp\s+([A-Za-z][A-Za-z0-9_]*)\s*\)", text, re.I
        )
        # Accept the compact call or the older named (opamp ...) block. Compact
        # Arity selects a 3-pin ideal amplifier, its powered 5-pin form, or
        # an 8-pin audio core. The common first five pins retain their order.
        if (call and OP.fullmatch(call[1])) or amp_name:
            if self.amp or self.battery or self.group or self.mode == "transient":
                raise CircuitError(
                    "Use op amps at the top level or in main, in a steady-state mode."
                )
            if amp_name:
                self.amp = (amp_name[1], line, {})
            else:
                try:
                    pins = parse_wires(call[2])
                    if len(pins) not in {3, 5, 8}:
                        raise CircuitError("Use 3, 5, or 8 pins.")
                    pins = tuple(map(binding.wire, pins))
                    if len(pins) == 8:
                        plus, minus, out, high, low, gain1, gain8, bypass = pins
                        self.audios.append(Component(binding.part(call[1]), "A", out, low,
                            label=binding.part(call[1]), in_plus=plus, in_minus=minus,
                            vplus=high, vminus=low, gain1=gain1, gain8=gain8, bypass=bypass, order=self.order))
                        self.circuit.named_wires.update(pins)
                    else:
                        self.add_amp(binding.part(call[1]), *pins)
                except CircuitError:
                    raise CircuitError(
                        "Use op1(in+, in-, out), op1(in+, in-, out, vplus, vminus), "
                        "or op1(in+, in-, out, vplus, vminus, gain1, gain8, bypass)."
                    ) from None
            return
        amp_end = bool(re.fullmatch(r"\(\s*end\s+opamp\s*\)", text, re.I))
        # The marker form accumulates in+/in-/out once each, then constructs the
        # device on (end opamp). Its fields cannot leak into quantity assignments.
        if self.amp or amp_end:
            if not self.amp:
                raise CircuitError("Unexpected (end opamp).")
            name, _, pins = self.amp
            if amp_end:
                if set(pins) != {"in+", "in-", "out"}:
                    raise CircuitError("Connect in+, in-, and out before (end opamp).")
                actuals = (binding.wire(pins[pin]) for pin in ("in+", "in-", "out"))
                self.add_amp(binding.part(name), *actuals)
                self.amp = None
            else:
                pin = re.fullmatch(r"(in\+|in-|out)\s*=\s*(.+)", text.rstrip(";"), re.I)
                if not pin or pin[1].lower() in pins:
                    raise CircuitError(
                        "Connect each pin once using in+ = wire, in- = wire, or out = wire."
                    )
                pins[pin[1].lower()] = parse_wires(pin[2], 1)[0]
            return
        group = self.group
        # switch1(...) is the four-terminal voltage-controlled device (kind S).
        # Its first two nodes sense control voltage, its next two conduct, and
        # its remaining values specify threshold and ordered on/off resistances.
        if call and SWITCH_CALL.fullmatch(call[1]):
            if group or self.battery or self.mode == "transient":
                raise CircuitError("Place switches in a steady-state circuit or method.")
            arguments = [arg.strip() for arg in call[2].split(",")]
            if len(arguments) != 7:
                raise CircuitError("Use switch1(control, reference, a, b, threshold, on, off).")
            control, reference, a, b = map(binding.wire, parse_wires(",".join(arguments[:4]), 4))
            threshold = real(scalar(arguments[4], "V"))
            on, off = (real(scalar(value, "ohm")) for value in arguments[5:])
            if not 0 < on < off:
                raise CircuitError("Switch resistances require 0 < on < off.")
            name = binding.part(call[1])
            if name.casefold() in self.names:
                raise CircuitError(f'Give switch "{name}" a unique component name.')
            self.names.add(name.casefold())
            self.circuit.components.append(Component(
                name, "S", a, b, on, label=name, order=self.order, in_plus=control,
                in_minus=reference, threshold=threshold, off_resistance=off,
            ))
            self.circuit.named_wires.update((a, b, control, reference))
            return
        transistor = TRANSISTOR.fullmatch(text)
        compact = TRANS_CALL.fullmatch(call[1]) if call else None
        positional = TRANS_POSITIONAL.fullmatch(text.rstrip(";").strip())
        # Normalize braced, transQ1(...), and positional transistor declarations
        # into the same staged component. A numeric compact suffix gets a Q prefix;
        # the first three arguments still bind base, collector, and emitter.
        if transistor or compact or positional:
            if group or self.battery:
                raise CircuitError(
                    "Place a transistor at the top level, in main, or directly in a circuit method."
                )
            if self.mode == "transient":
                raise CircuitError(
                    "Use a steady-state mode to render or export BJT circuits. "
                    "The built-in Transient mode supports first-order RC and RL circuits."
                )
            if compact or positional:
                name, arguments = (compact[1], call[2]) if compact else positional.groups()
                if not name[0].isalpha():
                    name = f"Q{name}"
                self.transistor_call(binding.part(name), arguments, line, binding)
            else:
                self.transistor = (binding.part(transistor[1]), line, {})
            return
        # Reusable method expansion belongs upstream. A remaining section header
        # here opens an electrical block with actual terminal names; a header
        # with no argument list cannot supply those connections.
        if section := SECTION.fullmatch(text):
            kind, name, raw = section.groups()
            if group or self.battery or self.mode == "transient":
                raise CircuitError(
                    "Put named sections at the top level in a steady-state mode."
                )
            if any(g.name.casefold() == name.casefold() for g in self.circuit.groups):
                raise CircuitError(f'Duplicate section name "{name}".')
            if raw is None:
                raise CircuitError(f'Call "{name}" from main or a circuit method.')
            pins = parse_wires(raw)
            if len(pins) > 2:
                raise CircuitError(f"Use {kind} {name}(input, output) {{ ... }}.")
            self.start_group(kind.lower(), line, name, pins, braced=True)
            return
        # Inline series/parallel blocks may provide explicit endpoints. Bare blocks
        # in main share an implicit series parent; bare blocks in a scoped method
        # must instead name their endpoints so connection intent is explicit.
        if inline := INLINE.fullmatch(text):
            if self.battery or self.mode == "transient":
                raise CircuitError(
                    "Use series and parallel blocks in steady-state circuits."
                )
            if group and group.returned:
                raise CircuitError("Close the section with } immediately after return.")
            kind, raw = inline.groups()
            pins = (
                tuple(map(binding.wire, parse_wires(raw, 2))) if raw is not None else ()
            )
            if not group and not pins:
                if binding.in_main:
                    # Bare blocks in main share a series parent. Bind its source
                    # terminals after all source declarations have been read.
                    if self.inline_chain is None:
                        self.start_group("series", line)
                        self.inline_chain = self.active
                    else:
                        self.active = self.inline_chain
                elif binding.scope:
                    raise CircuitError(
                        f"Use {kind}(input, output) to connect an inline block in a circuit method."
                    )
            self.start_group(kind.lower(), line, pins=pins, braced=True)
            return
        # Close the currently open battery or braced group, checking its closing
        # convention and required contents. Attach a header output at the final
        # junction before restoring the parent group.
        if text == "}":
            if self.battery:
                if not self.battery.braced:
                    raise CircuitError("Close the battery with (end battery) before }.")
                self.end_battery(binding)
                return
            if not group or not group.braced:
                raise CircuitError("Unexpected closing brace.")
            if not group.entries:
                raise CircuitError(
                    f'Section "{group.name or group.mode}" needs at least one component.'
                )
            if group.output:
                group.wires.append((len(group.entries), group.output))
            elif group.name and not group.returned:
                raise CircuitError(
                    "Declare an output wire in the header or end the section with return wire;."
                )
            self.active = group.parent
            # The implicit main chain remains as a parent record, but is not left
            # active between sibling blocks. Ordinary top-level parts can follow.
            if self.active == self.inline_chain:
                self.active = None
            return
        if group and group.returned and not self.battery:
            raise CircuitError("Close the section with } immediately after return.")
        # A one-header-wire named section can declare its output after its entries.
        # Store that final junction binding and require the section to close next.
        if returned := re.fullmatch(r"return\s+(.+)", text, re.I):
            if (
                not group
                or not group.name
                or not group.entries
                or self.battery
                or group.output
            ):
                raise CircuitError(
                    "Put return wire; after the components of a named section with one header wire."
                )
            name = parse_wires(returned[1], 1)[0]
            group.returned = True
            group.wires.append((len(group.entries), name))
            self.circuit.named_wires.add(name)
            return
        if self.battery:
            text = text.removesuffix(";").rstrip()
        # Ground/wire statements are interpreted in the active battery/group
        # context before a generic scalar assignment can consume their names.
        if ground := re.fullmatch(r"ground\s*=\s*(.*)", text, re.I):
            self.set_ground(binding.wire(parse_wires(ground[1], 1)[0]))
            return
        wire = re.fullmatch(r"wire\s*=\s*(.*)", text, re.I)
        bare = (
            (group or self.battery)
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", text)
            or re.fullmatch(
                r"wire(?:[A-Za-z0-9_.-]+|[ \t]+[A-Za-z0-9_][A-Za-z0-9_. \t-]*)",
                text,
                re.I,
            )
        )
        # Normalize explicit wire= and compatible bare wire syntax, then apply
        # instance bindings. Battery terminals, strong group junction names, and
        # terminal taps are distinct records even though their input syntax overlaps.
        if wire or bare:
            name = binding.wire(parse_wires(wire[1] if wire else text, 1)[0])
            self.circuit.named_wires.add(name)
            if self.battery:
                wires = self.battery.wires
                if len(wires) >= 2:
                    raise CircuitError("A battery has two terminals.")
                wires.append(name)
            elif group:
                if (
                    wire
                    and name != "0"
                    and (group.mode == "parallel" or not (group.name and group.output))
                ):
                    # In these contexts an explicit nonzero wire= annotates a component
                    # terminal. Keep its source position for later tap resolution; it does
                    # not insert a zero-resistance branch across a parallel group.
                    group.taps.append((len(group.entries), name, line))
                else:
                    group.wires.append((len(group.entries), name))
            else:
                raise CircuitError(
                    "Put wire connections inside a battery, series, or parallel block."
                )
            return
        braced_battery = BATTERY_BRACE.fullmatch(text)
        # Both battery syntaxes build the same temporary record while remembering
        # which closing form is required. Electrical source construction waits
        # until all its values and optional wire names have been read.
        if battery_start := braced_battery or BATTERY_MARKER.fullmatch(text):
            if self.mode == "transient" or self.battery:
                raise CircuitError(
                    "Use one source per battery block in a steady-state mode."
                )
            self.battery = Battery(
                (battery_start[1] or f"Battery {len(self.sources) + 1}").strip(),
                line,
                braced_battery is not None,
            )
            return
        if re.match(r"\(\s*end\s+battery\b", text, re.I):
            if not self.battery or not re.fullmatch(
                r"\(\s*end\s+battery\s*\)", text, re.I
            ):
                raise CircuitError("Close a named battery with (end battery).")
            if self.battery.braced:
                raise CircuitError("Close a braced battery with }.")
            self.end_battery(binding)
            return
        # Normalize source-block aliases such as Vrms and rad/s only within the
        # battery context, then pass its setting to the dedicated value parser.
        if self.battery:
            text = BATTERY_ALIAS.sub(
                lambda match: ("Vmag" if match[1].lower() == "vrms" else "w") + " =",
                text,
            )
            self.battery_value(ASSIGNMENT.fullmatch(text), binding)
            return
        assignment = ASSIGNMENT.fullmatch(text)
        # Retain the legacy (series)/(parallel) form with explicit matching end
        # markers. It uses the same group tree as braces but cannot be closed by }.
        if marker := re.fullmatch(r"\(\s*(end\s+)?(series|parallel)\s*\)", text, re.I):
            close, kind = marker[1], marker[2].lower()
            if close:
                if not group or group.braced or group.mode != kind or not group.entries:
                    raise CircuitError(
                        f"Unexpected closing marker for {kind} or empty group."
                    )
                self.active = group.parent
            else:
                if self.mode == "transient":
                    raise CircuitError("Use groups in a steady-state mode.")
                self.start_group(kind, line)
            return
        if assignment and assignment[1] == "measure":
            self.measure(assignment[2], binding)
            return
        # The one-wire source shorthand uses node->0 for voltage and 0->node
        # for current, making positive current inject into the named net.
        if source := SOURCE_WIRE.fullmatch(text):
            if group:
                raise CircuitError(
                    "Put a source with : wire = at the top level, in main, or in a circuit method."
                )
            name, raw, wire = source.groups()
            (node,) = parse_wires(wire, 1)
            p, n = ("0", node) if name[0].upper() == "I" else (node, "0")
            self.add_node_component(name, raw, p, n, binding)
            return
        # Inside a group, R1=... and similar rows are entries whose endpoints are
        # deferred. At top level, quantities, waveforms, and explicit-node parts
        # have different meanings and are dispatched only after this check.
        if group:
            self.group_entry(assignment, binding)
            return
        if waveform := re.fullmatch(r"([iv])\s*\(\s*t\s*\)\s*=\s*(.*)", text):
            self.waveform(waveform[1].upper(), waveform[2])
            return
        if assignment and "_" not in assignment[1]:
            self.assignment(*assignment.groups(), binding)
            return
        self.node_row(text, binding)

    # Defer equation lowering until every property default/patch is known.
    def add_equation(self, text, digital, binding, line, priority=0):
        self.math_rows.append((text, digital, binding, line, priority))

    def defer_model_issue(self, message, line=None, path=None, parameter="", raw="", *, owner=None):
        """A layout may retain unfinished behavior; analysis must reject it."""
        issue = ModelIssue(message, line, path, parameter, raw, owner)
        if issue not in self.circuit.model_issues:
            self.circuit.model_issues.append(issue)

    def apply_supply_totals(self):
        """Allocate a chip's shared bias draw once across its powered channels.

        Explicit per-channel currents are retained. Any remaining total is
        divided equally among channels whose current was left at its default.
        Other device types need an explicit numerical supply-load model.
        """
        circuit = self.circuit
        totals = {}
        allocated = set()
        for binding, current, line, priority in self.supply_totals:
            scope = binding.scope, binding.instance
            choices = totals.setdefault(scope, {})
            if priority in choices and choices[priority][0] != current:
                raise CircuitError("Conflicting overrides for supply.current.total.", line, path=binding.path)
            choices[priority] = current, line, binding
        for (_, owner), choices in totals.items():
            total, line, binding = choices[max(choices)]
            members = {owner}
            if owner is not None:
                for index, instance in enumerate(circuit.instances):
                    if instance.parent in members:
                        members.add(index)
            modeled_members = {name for cmp in circuit.comparators for name in cmp.members}
            channels = [part for part in circuit.components if part.kind in {"O", "A"}
                        and part.name not in modeled_members
                        and (owner is None or circuit.owners.get(part.name) in members)]
            channels += [cmp for cmp in circuit.comparators if owner is None or cmp.owner in members]
            explicit = {part.name for part in channels if any(
                part.name.casefold() in setting.targets and "supply_current" in setting.fields
                for setting in self.model_settings)}
            explicit.update(part.name for part in channels if isinstance(part, ComparatorInstance) and part.explicit_supply)
            fixed = sum((part.model.supply_current if isinstance(part, ComparatorInstance) else
                         part.opamp.supply_current if part.kind == "O" and part.opamp else
                         part.audio.supply_current if part.kind == "A" and part.audio else 0)
                        for part in channels if part.name in explicit)
            free = [part for part in channels if part.name not in explicit]
            remainder = total - fixed
            tolerance = 1e-9 * max(total, fixed, 1e-15)
            message = ""
            if allocated.intersection(part.name for part in channels):
                message = "Overlapping chip supply.current.total scopes are not supported; configure each channel group once."
            elif not channels and total:
                message = "supply.current.total needs a numerical supply-load model for this chip's device types."
            elif remainder < -tolerance:
                message = "supply.current.total is less than the explicitly configured channel currents."
            elif not free and abs(remainder) > tolerance:
                message = "supply.current.total cannot be allocated: every channel already has an explicit supply.current."
            elif total and any(not (part.vplus and part.vminus) for part in channels):
                message = "supply.current.total needs both supply pins on each amplifier channel."
            if message:
                if not self.render_only:
                    raise CircuitError(message, line, path=binding.path)
                self.defer_model_issue(message, line, binding.path, "supply.current.total", str(total), owner=binding.instance)
                continue
            allocated.update(part.name for part in channels)
            if not free:
                continue
            share = max(0.0, remainder) / len(free)
            free_names = {part.name for part in free}
            updated = []
            for part in circuit.components:
                if part.name in free_names:
                    if part.kind == "O":
                        part = replace(part, opamp=replace(part.opamp or OpAmpModel(), supply_current=share))
                    elif part.audio is not None:
                        part = replace(part, audio=replace(part.audio, supply_current=share))
                updated.append(part)
            circuit.components = updated
            circuit.comparators = [replace(cmp, model=replace(cmp.model,supply_current=share))
                                   if cmp.name in free_names else cmp for cmp in circuit.comparators]

    def comparator_matches(self, setting):
        """Match explicitly configured six-terminal open-collector methods."""
        names = {name.casefold() for name in setting.locals}
        matches = []
        for index, instance in enumerate(self.circuit.instances):
            pins = dict(instance.pins)
            required = {"plus","minus","collector","emitter","vminus"}
            if (instance.parent == setting.owner and instance.name.casefold() in names
                    and len(pins) == 6 and required <= pins.keys()
                    and ("vcc" in pins or "vplus" in pins)):
                matches.append(index)
        return matches

    def bind_comparators(self):
        """Keep the physical display graph, binding a numerical macro separately."""
        circuit = self.circuit
        settings_by_owner = {}
        for setting in self.model_settings:
            for owner in self.comparator_matches(setting):
                settings_by_owner.setdefault(owner,[]).append(setting)
        for owner, settings in settings_by_owner.items():
            instance = circuit.instances[owner]
            descendants = {owner}
            for index, child in enumerate(circuit.instances):
                if child.parent in descendants:
                    descendants.add(index)
            members = [p for p in circuit.components if circuit.owners.get(p.name) in descendants]
            pins = {name:circuit.resolve_wire(net) for name,net in instance.pins}
            output = next((p for p in members if circuit.owners.get(p.name) == owner and p.kind == "Q"
                           and p.p == pins["collector"] and p.n == pins["emitter"]),None)
            try:
                if output is None:
                    raise CircuitError("An open-collector method needs its own collector/emitter output transistor.")
                name = "@comparator:" + output.name
                configured = [replace(setting,targets=tuple(name.casefold() if local.casefold() == instance.name.casefold()
                                                            else target for local,target in zip(setting.locals,setting.targets)))
                              for setting in settings]
                configured = self.preview_settings(configured,name,ComparatorModel)
                model = resolve_model(configured,name,comparator=True)
                member_names = {p.name for p in members}
                terminals = set(pins.values()) | {"0"}
                internals = {net for p in members for net in p.terminal_nets} - terminals
                outside = {net for p in circuit.components if p.name not in member_names for net in p.terminal_nets}
                outside.update(net for eq in circuit.behavior for net in (eq.output,eq.reference,*eq.inputs))
                outside.update(circuit.resolve_wire(net) for net in circuit.connector_pins)
                outside.update(tap.net for tap in circuit.wire_taps)
                if internals & outside:
                    raise CircuitError("Behavioral comparator internals cannot be connected externally; use the declared method pins.")
                circuit.comparators.append(ComparatorInstance(
                    name,owner,output.name,pins["plus"],pins["minus"],pins["collector"],pins["emitter"],
                    pins.get("vcc",pins.get("vplus")),pins["vminus"],tuple(p.name for p in members),
                    frozenset(internals),model,settings[0].line,settings[0].path,
                    any("supply_current" in setting.fields for setting in settings)))
            except CircuitError as error:
                origin = settings[-1]
                if not self.render_only:
                    raise CircuitError(error.message,error.line or origin.line,error.code,path=error.path or origin.path) from None
                self.defer_model_issue(error.message,error.line or origin.line,error.path or origin.path)

    def preview_settings(self, settings, name, model_class):
        if not self.render_only:
            return settings
        result = []
        allowed = set(model_class.__dataclass_fields__)
        for setting in settings:
            if name.casefold() not in setting.targets:
                result.append(setting)
                continue
            fields = {}
            for key,value in setting.fields.items():
                target = "gain_default" if model_class is AudioAmpModel and key == "gain" else key
                if target in allowed:
                    fields[key] = value
                else:
                    line,path = setting.origins.get(key,(setting.line,setting.path))
                    self.defer_model_issue(f'Parameter "{key.replace("_",".")}" is not supported by {model_class.__name__}.',
                                           line,path,key.replace("_","."),setting.raws.get(key,str(value)))
            result.append(replace(setting,fields=fields))
        return result

    def compile_math(self):
        """Resolve property patches before substituting constants in equations."""
        scopes = {}
        for entry in self.property_rows:
            binding, name, raw, line, priority = entry[:5]
            annotation = entry[5] if len(entry) > 5 else None
            properties = scopes.setdefault((binding.scope, binding.instance), {})
            properties.setdefault(name, []).append((priority, raw, line, binding, annotation))
        values = {}
        unresolved = {}
        for scope, properties in scopes.items():
            resolved = values.setdefault(scope, {})
            for name, entries in properties.items():
                defaults = [entry for entry in entries if entry[0] == 0]
                if len(defaults) != 1:
                    _, _, line, binding, _ = (defaults[-1] if defaults else entries[-1])
                    message = (f'Custom property "{name}" is already declared.' if defaults else
                               f'Cannot override unknown custom property "{name}".')
                    raise CircuitError(message, line, path=binding.path)
                _, raw, line, binding, annotation = defaults[0]
                try:
                    default, unit = declared_property_value(raw, annotation)
                except CircuitError as error:
                    if not self.render_only:
                        raise CircuitError(error.message, line, path=binding.path) from None
                    self.defer_model_issue(error.message, line, binding.path, name, raw, owner=binding.instance)
                    unresolved.setdefault(scope, set()).add(name)
                    continue
                choices = {0: (default, line, binding)}
                invalid = False
                for priority, raw, line, binding, change_annotation in entries:
                    if not priority:
                        continue
                    try:
                        value, _ = declared_property_value(raw, change_annotation, unit=unit,
                                                          base_annotation=annotation)
                    except CircuitError as error:
                        if not self.render_only:
                            raise CircuitError(error.message, line, path=binding.path) from None
                        self.defer_model_issue(error.message, line, binding.path, name, raw, owner=binding.instance)
                        unresolved.setdefault(scope, set()).add(name)
                        invalid = True
                        continue
                    if priority in choices and choices[priority][0] != value:
                        raise CircuitError(f'Conflicting overrides for custom property "{name}".', line, path=binding.path)
                    choices[priority] = value, line, binding
                if invalid:
                    continue
                value, line, binding = choices[max(choices)]
                resolved[name] = value
                self.circuit.properties.append(ChipProperty(name, value, unit, binding.scope,
                                                            binding.instance, line, binding.path))
        scoped_rows = {}
        row_order = {}
        for index, row in enumerate(self.math_rows):
            binding = row[2]
            scoped_rows.setdefault((binding.scope, binding.instance), []).append(row)
            row_order[id(row)] = index
        compiled = []
        for scope, rows in scoped_rows.items():
            issues = [] if self.render_only else None
            equations = compile_scope_equations(rows, values.get(scope, {}), issues=issues,
                                                 unresolved=unresolved.get(scope, ()))
            for issue in issues or ():
                self.defer_model_issue(issue.message, issue.line, issue.path, owner=scope[1])
            for equation in equations:
                source_index = int(equation.name.removeprefix("@math"))
                compiled.append((row_order[id(rows[source_index])], equation))
        # Resolving each dependency scope need not reorder the source's
        # public voltage equations or their established numerical node order.
        for _, equation in sorted(compiled, key=lambda item: item[0]):
            equation = replace(equation, name=f"@math{len(self.circuit.behavior)}")
            self.circuit.behavior.append(equation)
            self.circuit.named_wires.update((equation.output, equation.reference, *equation.inputs))

    # Stage an O device with separate sensing pins and output branch.
    # Its provisional output return is 0; finish() can select a named domain.
    def add_amp(self, name, plus, minus, out, vplus="", vminus=""):
        self.amps.append(
            Component(
                name,
                "O",
                out,
                "0",
                label=name,
                in_plus=plus,
                in_minus=minus,
                order=self.order,
                vplus=vplus,
                vminus=vminus,
            )
        )
        self.circuit.named_wires.update((plus, minus, out))
        if vplus and vminus:
            self.circuit.named_wires.update((vplus, vminus))

    # Represent collector/emitter as p/n and keep base separately. Save both
    # the part's expanded-row order and the declaration line used for later
    # duplicate/unclosed-device errors.
    def add_transistor(self, name, line, base, collector, emitter, polarity, model=""):
        part = Component(
            name, "Q", collector, emitter, label=name, order=self.order, base=base,
            transistor_type=polarity, model=model,
        )
        self.transistors.append((part, line))
        self.circuit.named_wires.update((base, collector, emitter))

    # Validate polarity and the optional single model identifier, then bind
    # all terminal names. A model name is data for SPICE export; it is not
    # executed or loaded by this construction layer.
    def transistor_call(self, name, raw, line, binding):
        args = [arg.strip() for arg in raw.split(",")]
        if len(args) not in (4, 5):
            raise CircuitError("Use transQ1(base, collector, emitter, npn or pnp, [model]).")
        polarity = args[3].lower()
        if polarity not in BJT_TYPES:
            raise CircuitError("Choose npn or pnp as the fourth argument.")
        model = args[4] if len(args) == 5 else ""
        if len(args) == 5 and not BJT_MODEL_NAME.fullmatch(model):
            raise CircuitError("Use one SPICE model name such as 2N3904.")
        base, collector, emitter = (
            binding.wire(parse_wires(net, 1)[0]) for net in args[:3]
        )
        self.add_transistor(name, line, base, collector, emitter, polarity, model)

    # Accumulate one value per recognized field. Closing requires all three
    # terminals and a polarity; terminal values are bound immediately, while
    # type/model strings receive their own validation.
    def transistor_field(self, text, binding):
        name, line, fields = self.transistor
        if text == "}":
            missing = [key for key in (*BJT_PINS, "type") if key not in fields]
            if missing:
                raise CircuitError(
                    f"{name} needs these fields: {', '.join(missing)}.", line
                )
            base, collector, emitter = (fields[pin] for pin in BJT_PINS)
            self.add_transistor(
                name, line, base, collector, emitter, fields["type"], fields.get("model", "")
            )
            self.transistor = None
            return
        assignment = ASSIGNMENT.fullmatch(text)
        if not assignment:
            raise CircuitError("Use field = value inside a transistor block.")
        key, value = assignment[1].lower(), assignment[2].strip()
        if key in fields:
            raise CircuitError(f'Set "{key}" once in transistor {name}.')
        if key in BJT_PINS:
            value = binding.wire(parse_wires(value, 1)[0])
        elif key == "type":
            value = value.lower()
            if value not in BJT_TYPES:
                raise CircuitError("Use type = npn or type = pnp.")
        elif key == "model":
            if not BJT_MODEL_NAME.fullmatch(value):
                raise CircuitError("Use one SPICE model name such as 2N3904.")
        else:
            raise CircuitError(f'Unknown transistor field "{key}".')
        fields[key] = value

    # A battery takes priority over its surrounding group. In a parallel
    # group with entries, ground= applies to the immediately preceding branch;
    # otherwise it declares the block's return. Record the ground identity
    # without globally aliasing every named return to node 0.
    def set_ground(self, name):
        target = self.battery or self.group
        if target is None:
            raise CircuitError(
                "Put ground = name inside a battery, series, or parallel block."
            )
        if isinstance(target, Group) and target.returned:
            raise CircuitError("Close the section with } immediately after return.")
        if isinstance(target, Group) and target.mode == "parallel" and target.entries:
            branch = len(target.entries) - 1
            if target.branch_grounds.setdefault(branch, name) != name:
                raise CircuitError("Set one ground return per parallel branch.")
        else:
            if target.ground is not None and target.ground != name:
                raise CircuitError("Set one ground return per block.")
            target.ground = name
        self.circuit.grounds.add(name)
        self.circuit.named_wires.add(name)

    # A battery may appear inside a group or stand alone. Queue its source first, then let
    # group elaboration apply explicit wire names and the selected return node.
    def end_battery(self, binding):
        """Normalize the source values and queue the battery for wiring."""
        battery = self.battery
        label, line, known, wires = (
            battery.label,
            battery.line,
            battery.known,
            battery.wires,
        )
        kind, value, dc, equation = "V", 0, 0, "Given"
        # Separate the DC offset from the source waveform settings before RMS
        # inference. A voltage/current source must not combine incompatible
        # alternative declarations in the same block.
        offset = known.pop("Vdc", None)
        if offset is not None:
            dc = offset
        if "I" in known:
            if offset is not None or len(known) != 1:
                raise CircuitError(
                    "Use one current or voltage setting per source.", line
                )
            kind = "I"
            value, dc, equation = known["I"]
        # Peak-current source notation reuses AC quantity inference. The older
        # phaseV spelling is translated to phaseI for this current-source case,
        # and an omitted phase defaults to zero.
        elif "Ipk" in known:
            if offset is not None or AC_VOLTAGE & known.keys():
                raise CircuitError(
                    "Use either voltage values or Apk in a source block."
                )
            if "phaseV" in known:
                set_known(known, "phaseI", known.pop("phaseV"))
            known.setdefault("phaseI", 0)
            values, notes, equations = solve_known("ac", known)
            if "I" not in values or any("conflict" in n.lower() for n in notes):
                raise CircuitError(
                    "Enter a valid peak current and phase for the source."
                )
            kind, value, equation = "I", values["I"], equations.get("I", "Given")
        # In mixed mode, a plain V-only battery with no explicit AC magnitude is
        # DC-only: leave its AC value at zero and infer a real offset.
        elif (
            self.mode == "mixed"
            and offset is None
            and not (AC_VOLTAGE - {"V"}) & known.keys()
        ):
            if {"phaseV", "phaseI"} & known.keys():
                raise CircuitError(
                    "Specify an AC voltage or peak current with the phase."
                )
            dc, _ = _group_voltage(known, "dc", "the battery")
        else:
            if "phaseI" in known:
                raise CircuitError("Use phaseV for the voltage phase.")
            if offset is None or known:
                value, equation = _group_voltage(known, self.mode, "the battery")
        standalone = self.group is None
        # An explicit battery ground must agree with a second wire if supplied.
        # Fill an omitted first wire with the conventional in name before adding
        # the declared return; do not reverse the numeric source sign here.
        if battery.ground is not None:
            if len(wires) == 2 and wires[1] != battery.ground:
                raise CircuitError(
                    "The second battery wire and ground must name the same return.",
                    line,
                )
            if not wires:
                wires.append("in")
            if len(wires) == 1:
                wires.append(battery.ground)
        # Wrap a standalone battery in a one-entry series group so batteries
        # inside/outside groups share the same later endpoint elaboration.
        # Retain its wire list for connecting a possible bare main chain.
        if standalone:
            self.standalone = True
            self.standalone_batteries.append(wires)
            self.start_group("series", line)
        self.add_source(kind, binding.part(label), value, dc, equation, wires)
        if standalone:
            self.active = None
        self.battery = None

    # Normalize battery aliases at this boundary; Vneg supplies a negative magnitude while
    # frequency settings belong to the circuit-wide known quantities rather than a source pin.
    def battery_value(self, assignment, binding):
        if not assignment or not assignment[2] or "_" in assignment[1]:
            raise CircuitError(
                "Use a source value, wire name, or AC frequency inside the battery block."
            )
        key, raw = assignment.groups()
        label, known = self.battery.label, self.battery.known
        # Accept either sign in Vneg input, storing -abs(real voltage). The flag
        # enforces mutual exclusion with V even if repeated settings would agree.
        if key.casefold() == "vneg":
            if "V" in known or self.battery.negative_supply:
                raise CircuitError("Use either V or Vneg once in a battery block.")
            value = -abs(real(scalar(raw, "V")))
            set_known(known, "V", value)
            self.battery.negative_supply = True
            return
        # A source-local power request targets its bound display label, which
        # will be attached to the generated battery component during elaboration.
        if key == "measure" and raw.upper() == "P":
            self.circuit.probes.append(Probe("P", (binding.part(label),)))
            return
        if (
            key != "Vdc"
            and label.upper().startswith("V")
            and key.casefold() == label.casefold()
        ):
            key = "V"
        if key == "V" and self.battery.negative_supply:
            raise CircuitError("Use either V or Vneg once in a battery block.")
        # Permitted source fields depend on parsing mode. Mixed peak-current
        # notation also accepts Apk; generic arbitrary quantities are not valid
        # battery fields.
        allowed = {"V"}
        if self.mode in {"ac", "mixed"}:
            allowed = AC_VOLTAGE | FREQUENCY | {"Vdc", "phaseV"}
        if self.mode == "mixed":
            allowed |= {"Ipk", "phaseI"}
            if key == "Apk":
                key = "Ipk"
        if key != "I" and key not in allowed:
            raise CircuitError(
                "Use a source value, wire name, or AC frequency inside the battery block."
            )
        # Direct current syntax is normalized once into its value/offset/equation
        # tuple. Other battery fields below are scalar entries.
        if key == "I":
            if key in known:
                raise CircuitError("Set I only once in the source block.")
            value, dc, equation = _source(raw, "I", self.mode, group=True)
            known[key] = (value, real(dc), equation)
            return
        unit = (
            "V"
            if key == "Vdc"
            else QUANTITIES["ac" if self.mode == "mixed" else self.mode][key][1]
        )
        value = scalar(raw, unit)
        # Remember an offset so finish() can promote the result to mixed mode.
        # Frequency belongs to the circuit-wide known set, so all batteries share
        # and cross-check f/T/w rather than silently using independent frequencies.
        if key == "Vdc":
            value = real(value)
            self.offset = True
        set_known(self.circuit.known if key in FREQUENCY else known, key, value)

    # Give the staged source an internal identity and preserve its user label.
    # A B entry points into sources; empty p/n are filled by group topology
    # later. The part itself keeps the row order of this source creation.
    def add_source(self, kind, label, value, dc, equation, wires=()):
        part = Component(
            f"{kind}battery{len(self.sources) + 1}",
            kind,
            "",
            "",
            value,
            dc,
            label,
            equation,
            order=self.order,
        )
        self.group.entries.append(("B", "", len(self.sources)))
        self.sources.append((part, wires))

    # Bind then reserve a name for the contexts using this helper, comparing
    # case-insensitively. This avoids colliding scoped grouped entries/probes
    # while retaining the original spelling in the component record.
    def component_name(self, name, binding):
        name = binding.part(name)
        if name.casefold() in self.names:
            raise CircuitError(f'Duplicate component name "{name}".')
        self.names.add(name.casefold())
        return name

    # A group entry records kind/name/value without p/n. Named entries retain
    # row order; one-letter anonymous entries get generated labels at expansion.
    # Keep a missing value as None so drawing is possible without simulating it.
    def group_entry(self, assignment, binding):
        # Grouped S1=open/closed is a manual contact, stored as numerical kind K.
        # It differs from the four-terminal switch1(...) controlled device.
        if assignment and assignment[1][0].upper() == "S":
            name, raw = assignment.groups()
            closed = parse_switch(raw)
            name = self.component_name(name, binding)
            self.group.entry_order[len(self.group.entries)] = self.order
            self.group.entries.append(("K", name, SWITCH_ON_RESISTANCE if closed else SWITCH_OFF_RESISTANCE))
            return
        # A real potentiometer needs high/wiper/low nodes and cannot be inferred
        # from a two-terminal series/parallel entry assignment.
        if assignment and assignment[1][0].upper() == "P":
            raise CircuitError("A potentiometer needs three terminals: P1(value) between(high, wiper, low).")
        if not assignment or assignment[1][0].upper() not in UNITS or (
            not assignment[2].strip() and assignment[1][0].upper() == "X"
        ):
            raise CircuitError(
                "Use component assignments inside a group, such as R1 = 50."
            )
        name, raw = assignment.groups()
        kind = name[0].upper()
        if kind == "X" and self.mode != "ac":
            raise CircuitError("Use AC steady state for a specified reactance X.")
        name = self.component_name(name, binding) if len(name) > 1 else ""
        # Grouped sources use the same staged B representation as battery blocks.
        # A blank raw value is an unspecified source, never a zero-valued supply.
        if kind in "VI":
            label = (
                name
                or f"{'Battery' if kind == 'V' else 'Current source'} {len(self.sources) + 1}"
            )
            values = _source(raw, kind, self.mode, group=True) if raw.strip() else (None, 0, "Unspecified value")
            self.add_source(kind, label, *values)
            return
        # Specified complex impedance has its own mode/zero validation. Ordinary
        # group passive values must be real; zero R/X is handled as an exact
        # connection during graph expansion rather than as infinite admittance.
        if kind == "Z":
            value = self.impedance_value(raw)
        else:
            value = real(scalar(raw, UNITS[kind])) if raw.strip() else None
        if value is not None and ((kind == "R" and value < 0) or (kind in "CL" and value <= 0)):
            raise CircuitError("Use a real X, nonnegative R, or positive C or L.")
        self.group.entry_order[len(self.group.entries)] = self.order
        self.group.entries.append((kind, name, value))

    def measure(self, raw, binding):
        """Record an explicit probe or the legacy group measurement shorthand."""
        if not raw or self.mode == "transient":
            raise CircuitError("Circuit measurements use a steady-state mode.")
        # Explicit V/R measurements name two bound nets; I/P measurements name
        # one bound component. Probe records request readings without solving here.
        kind, *targets = raw.split()
        kind = kind.upper()
        if kind in {"V", "R"} and len(targets) == 2:
            self.circuit.probes.append(Probe(kind, tuple(map(binding.wire, targets))))
        # For a whole-group probe, leave targets empty and temporarily put the
        # group index in Probe.line. elaborate() replaces it with actual terminals
        # and the group's source line once endpoints exist.
        elif kind in {"V", "R"} and not targets and self.group:
            self.circuit.probes.append(Probe(kind, (), self.active))
        elif kind in {"I", "P"} and len(targets) == 1:
            self.circuit.probes.append(Probe(kind, (binding.part(targets[0]),)))
        # Legacy measure=Ix inside a group inserts a current-probe P marker.
        # It later becomes a zero-volt branch in series with the measured path.
        elif self.group and re.fullmatch(r"I[A-Za-z0-9_]*", raw, re.I):
            self.group.entry_order[len(self.group.entries)] = self.order
            self.group.entries.append(("P", self.component_name(raw, binding), 0))
        elif (
            self.mode == "ac"
            and not self.group
            and not self.circuit.legacy_measure
            and re.fullmatch(r"[RCLXZ][A-Za-z0-9_]+", raw, re.I)
        ):
            self.circuit.legacy_measure = raw
        else:
            raise CircuitError(
                "Use measure = I R1, measure = P V2, measure = V out 0, or measure = R out 0."
            )

    # Convert a standalone cosine into peak, angular-frequency, and phase
    # quantities. Record its source equation only for new givens and use
    # set_known() to reject incompatible repeated descriptions.
    def waveform(self, key, raw):
        if self.mode != "ac":
            raise CircuitError("Use AC steady state for a standalone cosine waveform.")
        match = WAVE.fullmatch(raw.replace("−", "-"))
        if not match:
            raise CircuitError(
                "Use i(t) = 58cos(377t - 30) A or v(t) = 5cos(377t) V. Phase is in degrees."
            )
        peak, omega, phase, unit = match.groups()
        peak = real(scalar(f"{peak} {(unit or '').strip('[] ')}", UNITS[key]))
        phase = real(scalar(phase, "deg")) if phase else 0
        # A negative cosine amplitude is represented by a nonnegative peak and
        # a phase shifted by half a turn; preserve the same waveform.
        if peak < 0:
            peak, phase = -peak, phase % 360 - 180
        for name, value in (
            (f"{key}pk", peak),
            ("w", float(omega)),
            (f"phase{key}", phase),
        ):
            if name not in self.circuit.known:
                self.circuit.equations[name] = (
                    f"{key.lower()}(t) = {key}pk x cos(wt + phase{key})"
                )
            set_known(self.circuit.known, name, value)

    # Handle top-level model/port declarations before scalar quantities.
    # Quantity names and unit tokens come from the shared catalog; in mixed
    # mode, only shared frequency quantities can be given through this path.
    def assignment(self, key, raw, binding):
        c = self.circuit
        if key == "model":
            if (
                self.mode != "transient"
                or c.model != "generic"
                or raw.lower() not in {"rc", "rl"}
            ):
                raise CircuitError(
                    "Use model = rc or model = rl once in Transient mode."
                )
            c.model = raw.lower()
        elif key == "port":
            if c.port or self.mode == "transient" or len(raw.split()) != 2:
                raise CircuitError(
                    "Use port = positive_node negative_node once with a circuit."
                )
            c.port = tuple(map(binding.wire, raw.split()))
        else:
            info = QUANTITIES.get(self.mode, QUANTITIES["ac"])
            if key not in info or (self.mode == "mixed" and key not in FREQUENCY):
                raise CircuitError(f'Unknown quantity "{key}" for this analysis.')
            set_known(c.known, key, scalar(raw, info[key][1]))

    # Normalize name(value) between(...), a bare name between(...), and the
    # space-separated name p n value form. Empty value syntax deliberately
    # survives as a schematic placeholder.
    def node_row(self, text, binding):
        if placement := PLACEMENT.fullmatch(text.removesuffix(";").rstrip()):
            name, raw, wires = placement.groups()
        elif placement := PART_PLACEMENT.fullmatch(text.removesuffix(";").rstrip()):
            name, wires = placement.groups()
            raw = ""
        else:
            match = re.fullmatch(
                r"([A-Za-z][A-Za-z0-9_]*)\s+(\S+)\s+(\S+)\s+(.+)", text
            )
            if not match:
                raise CircuitError(
                    "Use quantity = value or name positive_node negative_node value."
                )
            name, p, n, raw = match.groups()
            self.add_node_component(name, raw, p, n, binding)
            return
        # The placement form can bind three potentiometer terminals explicitly.
        # Store total resistance and fractional position without expanding legs;
        # electrical backends perform that expansion and fold results afterward.
        if name[0].upper() == "P":
            if self.mode == "transient":
                raise CircuitError("Use DC mode and Run in Results for a potentiometer time simulation.")
            p, wiper, n = map(binding.wire, parse_wires(wires, 3))
            value, position = parse_potentiometer(raw)
            name = self.component_name(name, binding)
            self.circuit.components.append(Component(name, "P", p, n, value, label=name,
                                                    order=self.order, wiper=wiper, position=position))
            self.circuit.named_wires.update((p, wiper, n))
            self.method_parts |= bool(binding.scope)
            return
        p, n = parse_wires(wires, 2)
        self.add_node_component(name, raw, p, n, binding)

    # Create explicit-terminal parts directly, preserving their bound instance names.
    # Special devices such as switches need their own state parsing before ordinary unit parsing.
    def add_node_component(self, name, raw, p, n, binding):
        kind = name[0].upper()
        # Two-terminal S syntax is normalized to manual kind K, including its
        # selected finite contact resistance, state, and off-resistance metadata.
        if kind == "S":
            if self.mode == "transient":
                raise CircuitError("Use DC mode and Run in Results for a switch time simulation.")
            closed = parse_switch(raw)
            name = self.component_name(name, binding)
            p, n = binding.wire(p), binding.wire(n)
            self.circuit.components.append(Component(
                name, "K", p, n, SWITCH_ON_RESISTANCE if closed else SWITCH_OFF_RESISTANCE,
                label=name, order=self.order, off_resistance=SWITCH_OFF_RESISTANCE, closed=closed,
            ))
            self.circuit.named_wires.update((p, n))
            self.method_parts |= bool(binding.scope)
            return
        if kind == "Z":
            value = self.impedance_value(raw)
        # Explicit connected parts belong to steady-state graph modes here.
        # The separate transient quantity mode describes known RC/RL responses;
        # time simulation of a graph is launched from its steady-state document.
        if self.mode == "transient":
            raise CircuitError(
                "Use known quantities with model = rc or model = rl for a series step response."
            )
        if kind not in "RCLZVI":
            raise CircuitError(
                f'Component "{name}" needs a name beginning R, C, L, Z, V, I, or S.'
            )
        pulse = None
        # A missing number is None with an explanatory label. Preserve this for
        # schematics; numerical entry points later require actual component values.
        if not raw.strip():
            value, dc, equation = None, 0, "Unspecified value"
        # A DC voltage source may carry pulse timing for the Results time runner.
        # Validate seven fields in V/s and keep its initial low level as the
        # steady-state source value; do not evaluate the waveform while compiling.
        elif signal := PULSE.fullmatch(raw.strip()):
            if self.mode != "dc" or kind != "V":
                raise CircuitError("Pulse sources use a voltage source in DC mode. Run the waveform in Results.")
            fields = signal[1].split(",")
            if len(fields) != 7:
                raise CircuitError("Use pulse(low, high, delay, rise, fall, width, period).")
            low, high, delay, rise, fall, width, period = (
                real(scalar(field, "V" if index < 2 else "s"))
                for index, field in enumerate(fields)
            )
            if delay < 0 or min(rise, fall, width) <= 0 or period < rise + fall + width:
                raise CircuitError("Use a nonnegative pulse delay, positive rise, fall and width, and a period that fits all three.")
            pulse = Pulse(low, high, delay, rise, fall, width, period)
            value, dc, equation = low, low, "Pulse initial level"
        elif kind == "Z":
            dc, equation = 0, "Specified complex impedance (AC only)"
        elif kind in "VI":
            value, dc, equation = _source(raw, kind, self.mode)
        else:
            value, dc, equation = scalar(raw, UNITS[kind]), 0, "Given"
        # Explicit rows already name their endpoints. Bind local names, append
        # the component directly, and retain named method terminals for drawing;
        # there is no series/parallel endpoint inference for these rows.
        name = binding.part(name)
        p, n = binding.wire(p), binding.wire(n)
        self.circuit.components.append(
            Component(name, kind, p, n, value, dc, name, equation, order=self.order, pulse=pulse)
        )
        if binding.scope:
            self.method_parts = True
            self.circuit.named_wires.update((p, n))

    def impedance_value(self, raw):
        """Z is a specified phasor-domain impedance, not a time-domain device."""
        # A theoretical Z has no DC/time-domain realization. Enforce pure AC even
        # for a placeholder, so mode constraints do not disappear until a value
        # is supplied. A specified zero Z must use an explicit connection instead.
        if self.mode != "ac":
            raise CircuitError("Use AC steady state for a theoretical impedance Z. "
                               "DC, mixed and transient analyses require physical R/L/C components.",
                               code="UnsupportedComponent")
        if not raw.strip():
            return None
        value = scalar(raw, "ohm")
        if not value:
            raise CircuitError("Impedance Z must be nonzero. Use the same wire name or an explicit zero-volt source for a short.")
        return value

    # Delay connection finalization until all declarations are known. Bare grouped calculations
    # may stay reducible, while probes, sources, and named wires require an explicit graph.
    def finish(self, render_only):
        """Choose graph elaboration or legacy group reduction after parsing."""
        c = self.circuit
        # Close every syntactic state before interpreting electrical connectivity.
        # Opening lines retained in the pending records locate missing terminators.
        if self.math_block is not None:
            raise CircuitError("Close the mathematical behavior block with }.", self.math_block[1])
        if self.amp:
            raise CircuitError("Missing (end opamp).", self.amp[1])
        if self.opamp_config is not None:
            raise CircuitError("Close the analog model block with }.", self.opamp_config.line, path=self.opamp_config.path)
        if self.pending_override:
            raise CircuitError("Put @override directly above an analog or digital model block.")
        if self.transistor:
            name, line, _ = self.transistor
            raise CircuitError(f"Close transistor {name} with }}.", line)
        if self.battery:
            closing = "}" if self.battery.braced else "(end battery)"
            raise CircuitError(f"Missing {closing}.", self.battery.line)
        if self.group:
            raise CircuitError(
                f"Missing closing marker for {self.group.name or self.group.mode}.",
                self.group.line,
            )
        self.compile_math()
        if self.inline_chain is not None:
            self.connect_inline_chain()
        if (
            not self.standalone
            and not c.named_wires
            and sum(g.parent is None for g in c.groups) > 1
        ):
            raise CircuitError("Use wire = name to connect multiple outer blocks.")
        if c.legacy_measure and c.legacy_measure.casefold() not in self.names:
            raise CircuitError(
                "Name a component in a series or parallel block to measure."
            )
        # Keep unnamed quantity-reduction groups separate from explicit node
        # networks unless an allowed source/method form establishes their wiring.
        # Otherwise there is no unambiguous connection between the two input styles.
        if (
            c.groups
            and self.inline_chain is None
            and not any(
                g.name or (g.braced and g.output and g.parent is None) for g in c.groups
            )
            and not (self.amps or self.transistors)
            and (c.components or c.port)
            and not (
                self.standalone
                and (
                    self.method_parts
                    or all(
                        g.parent is None
                        and len(g.entries) == 1
                        and g.entries[0][0] == "B"
                        for g in c.groups
                    )
                )
            )
        ):
            raise CircuitError(
                "Use groups with known quantities, or enter a circuit with node names."
            )
        # A Vdc setting promotes the returned Circuit to mixed analysis. Specified
        # X/Z are rejected where their lack of a DC realization would make that
        # split invalid; physical C/L have well-defined DC behavior.
        if self.offset:
            if any(p.kind == "Z" for p in c.components) or any(
                kind == "Z" for g in c.groups for kind, _, _ in g.entries
            ):
                raise CircuitError("Theoretical impedance Z is AC-only and cannot be combined with a DC offset.",
                                   code="UnsupportedComponent")
            if any(kind == "X" for g in c.groups for kind, _, _ in g.entries):
                raise CircuitError(
                    "Use C or L to specify reactance in a circuit with a DC offset."
                )
            c.mode = "mixed"
        # Sources, mixed mode, explicit probes/nets, and group current probes need
        # actual terminal identities. Plain unnamed groups with only quantities
        # can instead be reduced to equivalent R/Z by the later analysis layer.
        needs_graph = bool(
            self.sources
            or c.mode == "mixed"
            or c.probes
            or c.named_wires
            or any(kind == "P" for g in c.groups for kind, _, _ in g.entries)
        )
        # Rendering also needs an explicit graph even when numerical analysis
        # could use reduction. Building geometry does not itself solve the graph.
        if c.groups and (needs_graph or render_only):
            if c.legacy_measure and not render_only:
                raise CircuitError(
                    "Battery circuits already show the voltage and current for each component."
                )
            self.elaborate(needs_graph)
        elif c.groups:
            c.reduce_groups = True
        # Append staged op amps/BJTs after grouped components exist, checking their
        # names against final part names/labels. Resolve tap aliases before
        # selecting each ideal amplifier's return domain.
        labels = {name.casefold() for e in c.components for name in (e.name, e.label)}
        targets = {part.name.casefold() for part in (*self.amps, *self.audios)}
        targets.update(target for setting in self.model_settings if self.comparator_matches(setting)
                       for target in setting.targets)
        for setting in self.model_settings:
            if setting.fields and any(name not in targets for name in setting.targets):
                message = ("An analog model targets an unsupported method or a component without connections: " +
                           ", ".join(name for name in setting.targets if name not in targets))
                if not self.render_only:
                    raise CircuitError(message, setting.line, path=setting.path)
                self.defer_model_issue(message, setting.line, setting.path)
                for key,value in setting.fields.items():
                    line,path = setting.origins.get(key,(setting.line,setting.path))
                    self.defer_model_issue(message,line,path,key.replace("_","."),setting.raws.get(key,str(value)))
        for amp in self.amps:
            configured = [setting for setting in self.model_settings if amp.name.casefold() in setting.targets]
            if configured:
                configured = self.preview_settings(configured,amp.name,OpAmpModel)
                try:
                    model = resolve_model(configured, amp.name)
                    if needs_supplies(model) and not (amp.vplus and amp.vminus):
                        origin = configured[-1]
                        raise CircuitError("Supply draw and rail-relative model limits need both supply pins.", origin.line, path=origin.path)
                    amp = replace(amp, opamp=model)
                except CircuitError as error:
                    if not self.render_only:
                        raise
                    self.defer_model_issue(error.message, error.line, error.path)
            if amp.name.casefold() in labels:
                raise CircuitError("Give each op amp a unique component name.")
            labels.add(amp.name.casefold())
            c.components.append(amp)
        for amp in self.audios:
            if amp.name.casefold() in labels:
                raise CircuitError("Give each op amp a unique component name.")
            labels.add(amp.name.casefold())
            try:
                amp = replace(amp, audio=resolve_model(self.preview_settings(self.model_settings,amp.name,AudioAmpModel), amp.name, audio=True))
            except CircuitError as error:
                if not self.render_only:
                    raise
                self.defer_model_issue(error.message, error.line, error.path)
            c.components.append(amp)
        for part, line in self.transistors:
            if part.name.casefold() in labels:
                raise CircuitError(
                    f'Give transistor "{part.name}" a unique component name.', line
                )
            labels.add(part.name.casefold())
            c.components.append(part)
        if c.wire_taps:
            self.resolve_tap_names()
        driven = {}
        aliases = {tap.name: tap.net for tap in c.wire_taps}
        c.behavior = [remap_equation(eq, lambda net: resolve_alias(net, aliases)) for eq in c.behavior]
        for eq in c.behavior:
            if eq.output in driven:
                previous = driven[eq.output]
                if eq.priority == previous.priority:
                    raise CircuitError(f'Multiple mathematical drivers or conflicting overrides for "{eq.output}".', eq.line, path=eq.path)
                if eq.priority < previous.priority:
                    continue
            driven[eq.output] = eq
        c.behavior = list(driven.values())
        if self.amps and c.grounds != {"0"}:
            self.connect_amp_returns()
        if render_only and not c.components and not c.instances and not c.behavior:
            raise CircuitError(
                "Enter a circuit with component rows or series/parallel blocks to render it."
            )
        return c

    # Ideal amplifier output current still needs the correct reference domain; input sensing
    # must not merge independently named grounds into the implicit global reference.
    def connect_amp_returns(self):
        """Find each ideal op amp's output reference after wiring is resolved."""
        c = self.circuit
        branches = [
            part.terminal_nets if part.kind in {"Q", "S", "P"} else (part.p, part.n)
            for part in c.components
            if part.kind != "O"
        ]
        # Prefer the output's actual non-op-amp return domain. If an unloaded
        # buffer has no such path, the second connectivity view below follows
        # its signal inputs to find a return, excluding the provisional output 0.
        local = ground_references(c.grounds, branches)
        # Follow ideal amplifier inputs when the output has no passive return,
        # as in an unloaded buffer. Exclude the old implicit node 0 here.
        signal = ground_references(
            c.grounds,
            [
                *branches,
                *((p.p, p.in_plus, p.in_minus) for p in c.components if p.kind == "O"),
            ],
        )
        # Rewrite only ideal amplifier output returns, preferring local over the
        # signal-derived fallback, then 0. Sensing identities and optional rails
        # remain separate from the branch that supplies output current.
        c.components = [
            replace(part, n=local.get(part.p, signal.get(part.p, "0")))
            if part.kind == "O"
            else part
            for part in c.components
        ]

    # Implicit main blocks can follow only one grounded source. More than one supply requires
    # explicit terminals so the compiler never guesses which source should drive the chain.
    def connect_inline_chain(self):
        """Connect bare main blocks between the single supply and its return."""
        group = self.circuit.groups[self.inline_chain]
        # Find explicitly grounded independent sources and normalize each pair
        # as (non-ground endpoint, return), regardless of branch orientation.
        # A bare main chain may use this inference only for exactly one supply.
        supplies = [
            (part.n, part.p) if part.p in self.circuit.grounds else (part.p, part.n)
            for part in self.circuit.components
            if part.kind in "VI"
            and ((part.p in self.circuit.grounds) != (part.n in self.circuit.grounds))
        ]
        # Standalone battery wrappers have not yet become endpoint-bound parts.
        # Include their declared/conventional wires in the same supply candidate
        # list rather than overlooking a second source.
        for wires in self.standalone_batteries:
            if len(wires) < 2:
                supplies.append((wires[0] if wires else "in", "0"))
            elif (wires[0] in self.circuit.grounds) != (
                wires[1] in self.circuit.grounds
            ):
                supplies.append(
                    tuple(reversed(wires))
                    if wires[0] in self.circuit.grounds
                    else tuple(wires)
                )
        if len(supplies) != 1 or supplies[0][0] in self.circuit.grounds:
            raise CircuitError(
                "Bare blocks in main need one grounded source. Use series(input, output) or parallel(input, output) to wire them explicitly.",
                group.line,
            )
        supply, ground = supplies[0]
        # A return explicitly set on the last block overrides the supply's default
        # return for the chain's final junction.
        last = self.circuit.groups[group.entries[-1][2]]
        ground = last.ground if last.ground is not None else ground
        group.wires.extend(((0, supply), (len(group.entries), ground)))
        self.circuit.named_wires.update((supply, ground))

    # Resolve aliases on every electrical terminal, including potentiometer wipers and optional
    # op-amp supplies, so later drawing and solver code see the same canonical net identities.
    def resolve_tap_names(self):
        c = self.circuit
        # A tap name can be used by later explicit parts as an alias of its actual
        # net. Apply that mapping to every terminal role, including wipers, BJT
        # bases, and schematic op-amp rails, not just ordinary p/n endpoints.
        aliases = {tap.name: tap.net for tap in c.wire_taps}

        c.components = [
            replace(
                part,
                p=resolve_alias(part.p, aliases),
                n=resolve_alias(part.n, aliases),
                in_plus=resolve_alias(part.in_plus, aliases),
                in_minus=resolve_alias(part.in_minus, aliases),
                base=resolve_alias(part.base, aliases),
                wiper=resolve_alias(part.wiper, aliases),
                vplus=resolve_alias(part.vplus, aliases),
                vminus=resolve_alias(part.vminus, aliases),
                gain1=resolve_alias(part.gain1, aliases),
                gain8=resolve_alias(part.gain8, aliases),
                bypass=resolve_alias(part.bypass, aliases),
            )
            for part in c.components
        ]
        c.wire_taps = [
            replace(tap, net=resolve_alias(tap.net, aliases)) for tap in c.wire_taps
        ]
        c.named_wires.update(tap.net for tap in c.wire_taps)
        if c.terminals:
            c.terminals = tuple(resolve_alias(node, aliases) for node in c.terminals)

    # Build provisional terminals for group branches, apply wire aliases, then resolve the
    # finished parts. A parallel wire tap names a rail; it does not create a shorted branch.
    def elaborate(self, needs_graph):
        """Assign terminals to nested groups, then apply wire names and taps."""
        c = self.circuit
        # A known supply above groups may be converted into a real source branch.
        # For a numerical battery graph, other outside quantities would be
        # ambiguous; a rendering-only reduction view uses just the voltage subset.
        source_names = AC_VOLTAGE | {"phaseV"} if self.mode == "ac" else {"V"}
        supply_keys = set(c.known) - FREQUENCY
        if not needs_graph:
            supply_keys &= source_names
            if supply_keys == {"phaseV"}:
                supply_keys.clear()
        if not supply_keys <= source_names:
            raise CircuitError(
                "Enter only the supply voltage, phase, and frequency outside a group with batteries."
            )
        # nodes stores group endpoint pairs; aliases maps provisional junctions
        # to explicit wire identities; entries links a leaf entry to its built
        # part and orientation. Tap resolution waits until these structures exist.
        nodes = {}
        aliases = {}
        taps = []
        entries = {}
        # When user net names are present, mark generated junctions as private.
        # Unaliased root boundaries initially get distinct temporary identities
        # instead of prematurely connecting unrelated roots through in/0.
        prefix = "@" if c.named_wires else ""
        roots = [i for i, g in enumerate(c.groups) if g.parent is None]
        for i in roots:
            nodes[i] = (f"@start{i + 1}", f"@return{i + 1}") if prefix else ("in", "0")

        # Register one explicit name for a provisional junction. Conflicting names
        # are source errors; an already declared ground cannot be renamed through
        # this path into a different wire identity.
        def alias(node, wire, line):
            existing = node if node in c.grounds else aliases.get(node)
            if existing is not None and existing != wire:
                raise CircuitError(
                    f'Wire names "{existing}" and "{wire}" refer to the same junction.',
                    line,
                )
            aliases[node] = wire

        parts = []
        # Move eligible top-level supply givens into a generated Vsupply branch.
        # Removing them from c.known prevents displaying the same values as
        # unrelated free quantities as well as a connected circuit source.
        if supply_keys:
            supply = {
                key: c.known.pop(key) for key in c.known.copy() if key in supply_keys
            }
            value, equation = _group_voltage(
                supply, self.mode, "the supply above the group"
            )
            parts.append(
                Component("Vsupply", "V", *nodes[0], value, 0, "Supply", equation)
            )
        # Groups are stored parent-first, so each parent establishes its
        # children's terminals before their entries are visited.
        for i, group in enumerate(c.groups):
            first, last = nodes[i]
            # A block-wide ground replaces its output return. In a series parent,
            # also bind the inherited junction so adjacent entries see that return;
            # a contradictory explicit output name is rejected.
            if group.ground is not None:
                if group.output and group.output != group.ground:
                    raise CircuitError(
                        "The output wire and ground must name the same return.",
                        group.line,
                    )
                if group.parent is not None:
                    parent = c.groups[group.parent]
                    if parent.mode == "series":
                        alias(last, group.ground, group.line)
                last = group.ground
                nodes[i] = first, last
            count = len(group.entries)
            # A series group has count+1 junctions and one adjacent pair per entry.
            # This lets interior wire positions name real series junctions.
            if group.mode == "series":
                junctions = [
                    first,
                    *(f"{prefix}g{i + 1}_{j}" for j in range(1, count)),
                    last,
                ]
                endpoints = list(zip(junctions, junctions[1:]))
                for position, name in group.wires:
                    alias(junctions[position], name, group.line)
            # Parallel entries initially share both rails. Strong wire bindings can
            # name only the first/last boundary; terminal taps are handled separately.
            else:
                endpoints = [(first, last)] * count
                for position, name in group.wires:
                    if position not in {0, count}:
                        raise CircuitError(
                            "Put parallel wires before the first or after the last branch.",
                            group.line,
                        )
                    alias(first if position == 0 else last, name, group.line)
                # Put a current-probe marker in series with exactly the following branch:
                # split that branch at a private probe node, rather than shorting the
                # parallel input/output rails with the probe's zero-volt source.
                for j, (kind, _, _) in enumerate(group.entries):
                    if kind != "P":
                        continue
                    if j + 1 == count or group.entries[j + 1][0] == "P":
                        raise CircuitError(
                            "Put a current probe immediately before the branch to measure."
                        )
                    node = f"{prefix}probe{i + 1}_{j + 1}"
                    endpoints[j], endpoints[j + 1] = (first, node), (node, last)
                # Override a selected parallel branch's negative endpoint without changing
                # other branches. A probe marker itself cannot own the branch return.
                for j, ground in group.branch_grounds.items():
                    if group.entries[j][0] == "P":
                        raise CircuitError(
                            "Put a branch ground after the component being measured.",
                            group.line,
                        )
                    endpoints[j] = endpoints[j][0], ground
            # A tap between entries refers to the next entry's input terminal; a tap
            # at the end refers to the last entry's output. Preserve that selection
            # until nested groups have actual leaf components.
            for position, name, line in group.taps:
                entry = min(position, count - 1)
                terminal = int(position == count)
                taps.append((i, entry, terminal, name, line))
            for j, (kind, name, value) in enumerate(group.entries):
                p, n = endpoints[j]
                # A child group inherits this entry's endpoint pair. It is visited later
                # because the group list is parent-first; no component is created for G.
                if kind == "G":
                    nodes[value] = p, n
                    continue
                reversed_source = False
                # Attach a staged battery/source and its declared wire aliases. Grouped
                # current sources reverse p/n to inject in the intended group direction;
                # remember that reversal for later terminal-tap selection.
                if kind == "B":
                    part, wires = self.sources[value]
                    for node, wire in zip((p, n), wires):
                        alias(node, wire, group.line)
                    if part.kind == "I":
                        reversed_source = True
                        p, n = n, p
                    parts.append(replace(part, p=p, n=n))
                else:
                    name = name or f"{kind}[{len(parts)}]"
                    equation = "Given"
                    # An ideal current probe or zero R/X is an exact 0-V branch with a solved
                    # current unknown, rather than a tiny invented resistance. Its equation
                    # marks it as a connection so reporting does not treat it as excitation.
                    if kind == "P" or (kind in "RX" and value == 0):
                        kind, equation = "V", "V = I x 0 = 0"
                    parts.append(
                        Component(
                            name,
                            kind,
                            p,
                            n,
                            value,
                            0,
                            name,
                            equation,
                            order=group.entry_order[j],
                            closed=kind == "K" and value == SWITCH_ON_RESISTANCE,
                            off_resistance=SWITCH_OFF_RESISTANCE if kind == "K" else 0,
                        )
                    )
                entries[i, j] = parts[-1], reversed_source
        # After explicit aliases have been recorded, give remaining root boundaries
        # their conventional in/0 defaults. Existing aliases take precedence.
        if prefix:
            for i in roots:
                p, n = nodes[i]
                aliases.setdefault(p, "in")
                aliases.setdefault(n, "0")

        def resolve(node):
            return resolve_alias(node, aliases)

        # Walk a tapped nested group to its first/last leaf entry. Convert logical
        # input/output side to physical p/n with XOR when a current source was
        # reversed, so the tap still points at the intended schematic terminal.
        def terminal(group, entry, side):
            while c.groups[group].entries[entry][0] == "G":
                group = c.groups[group].entries[entry][2]
                entry = len(c.groups[group].entries) - 1 if side else 0
            part, reversed_source = entries[group, entry]
            pin = side ^ reversed_source
            return part, pin, (part.p, part.n)[pin]

        # First locate each tap's leaf terminal. A still-private junction may adopt
        # the tap name as its canonical wire; an already named net keeps its identity.
        positions = []
        for group, entry, side, name, line in taps:
            part, pin, raw = terminal(group, entry, side)
            node = resolve(raw)
            if node.startswith("@") and name not in c.grounds:
                alias(node, name, line)
            positions.append((name, raw, part.name, pin, line))

        # Then resolve all aliases and build persistent WireTap records. Reusing
        # a tap name is legal only for the same final net. A tap label cannot
        # silently select a different ground return in place of ground= syntax.
        tap_aliases = {}
        for name, raw, component, pin, line in positions:
            net = resolve(raw)
            if name in c.grounds and name != net:
                raise CircuitError(f"Use ground = {name} to select that return.", line)
            if tap_aliases.setdefault(name, net) != net:
                raise CircuitError(
                    f'Wire "{name}" is already a tap on another net.', line
                )
            c.wire_taps.append(WireTap(name, net, component, pin))

        if len({e.label.casefold() for e in parts}) != len(parts):
            raise CircuitError("Use unique battery and component labels.")
        # Materialize resolved group parts, preserve the first root's boundary
        # for group-level calculations, and replace deferred whole-group probes
        # with their terminal pairs and real source lines.
        c.components.extend(replace(e, p=resolve(e.p), n=resolve(e.n)) for e in parts)
        c.terminals = tuple(map(resolve, nodes[0]))
        c.probes = [
            replace(
                probe,
                targets=tuple(map(resolve, nodes[probe.line])),
                line=c.groups[probe.line].line,
            )
            if not probe.targets
            else probe
            for probe in c.probes
        ]
        c.grouped = True


# Follow the entire alias chain, retaining visited names to turn cycles into source errors.
def resolve_alias(node, aliases):
    """Follow wire aliases to their final net and report cycles."""
    # Follow aliases until a real net or identity mapping is reached. An
    # identity alias is harmless; a longer cycle must fail instead of hanging
    # connection finalization.
    seen = set()
    while node in aliases and node != aliases[node]:
        if node in seen:
            raise CircuitError("Circular wire aliases.")
        seen.add(node)
        node = aliases[node]
    return node
