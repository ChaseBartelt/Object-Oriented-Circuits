"""Build electrical graphs from expanded OOC statements.

Device/group state and connection finalization live here. Document expansion
and file linking are separate callers, so this module does not import them.
"""

import re
from dataclasses import dataclass, field, replace
from math import sqrt

from circuitlab.model.graph import (
    Circuit,
    CircuitError,
    Component,
    Probe,
    Pulse,
    WireTap,
    ground_references,
)
from circuitlab.math.equations import solve_known
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
    OP,
    PART_PLACEMENT,
    PLACEMENT,
    PULSE,
    SECTION,
    SOURCE_WIRE,
    SWITCH_CALL,
    TRANSISTOR,
    TRANS_CALL,
    TRANS_POSITIONAL,
    UNITS,
    WAVE,
    analysis_mode,
    parse_potentiometer,
    parse_wires,
)


@dataclass
class Group:
    """A series or parallel block before its terminals have been resolved."""

    mode: str
    line: int
    parent: int | None = None
    name: str = ""
    output: str = ""
    returned: bool = False
    # Entries hold (kind, name, value). G and B reference a nested group
    # or battery by index. P reserves a zero-volt current probe.
    entries: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    braced: bool = False
    taps: list = field(default_factory=list)
    ground: str | None = None
    branch_grounds: dict[int, str] = field(default_factory=dict)
    entry_order: dict[int, int] = field(default_factory=dict)
    entry_sources: dict[int, tuple[int, object]] = field(default_factory=dict)


@dataclass
class Battery:
    """Collect source values and wire names until the battery block closes."""

    label: str
    line: int
    braced: bool
    known: dict = field(default_factory=dict)
    wires: list[str] = field(default_factory=list)
    ground: str | None = None


def _ac_source(raw, unit):
    raw = raw.strip()
    basis = re.search(rf"{unit}(pk|rms)$", raw, re.I)
    if not basis:
        return scalar(raw, unit), "Given"
    value = scalar(raw[: -len(basis[1])], unit)
    if basis[1].lower() == "pk":
        return value / sqrt(2), f"{unit}_RMS = {unit}pk / √2"
    return value, "Given"


def _source(raw, kind, mode, group=False):
    unit = UNITS[kind]
    if mode == "mixed":
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
    known = dict(known)
    if "V" in known and "phaseV" in known and not known["V"].imag and known["V"] >= 0:
        set_known(known, "Vmag", known.pop("V"))
    if "V" not in known:
        known.setdefault("phaseV", 0)
    values, diagnostics, equations = solve_known("ac", known)
    if any("conflict" in d.lower() for d in diagnostics):
        raise CircuitError(f"Voltage for {label}: {' '.join(diagnostics)}")
    if "V" not in values:
        raise CircuitError(f"Enter the voltage for {label}.")
    equation = equations.get("V", "Given")
    if "V" in equations and "Vpk" in known and "Vmag" not in known:
        equation = "V_RMS = (Vpk / √2) ∠ phaseV"
    return values["V"], equation


def compile_program(program, mode, *, render_only=False):
    """Read elaborated statements and resolve their electrical graph."""
    mode = mode.lower()
    if mode == "auto":
        mode = analysis_mode(program.mode_source)
    if mode not in {"dc", "ac", "mixed", "transient"}:
        raise CircuitError(f"Unknown analysis mode {mode!r}.")
    compiler = Compiler(mode)
    connectors = program.connectors
    compiler.circuit.named_wires.update(connectors)
    compiler.circuit.connector_pins = tuple(connectors)
    compiler.circuit.instances = list(program.instances)
    owners, lines = {}, {}
    paths = {}
    for order, (text, line, binding) in enumerate(program.rows, 1):
        compiler.order = order
        lines[order] = line
        if binding.path is not None:
            paths[order] = binding.path
        if binding.instance is not None:
            owners[order] = binding.instance
        try:
            compiler.read(text, line, binding)
        except CircuitError as error:
            if error.line is not None:
                raise
            raise CircuitError(str(error), line, error.code, path=binding.path) from None
    circuit = compiler.finish(render_only)
    for group in circuit.groups:
        group.entry_sources = {index: (lines[order], paths.get(order))
                               for index, order in group.entry_order.items()}
    for pin, line in connectors.items():
        if circuit.resolve_wire(pin) not in circuit.nodes:
            raise CircuitError(f'Connector pin "{pin}" is not a net in this file.', line)
    circuit.owners, circuit.component_lines, circuit.component_files = (
        {part.name: origins[part.order] for part in circuit.components if part.order in origins}
        for origins in (owners, lines, paths)
    )
    return circuit


class Compiler:
    """Collect circuit statements, then resolve groups, taps, and returns."""

    def __init__(self, mode):
        self.circuit = Circuit(mode)
        self.order = 0
        self.mode = mode
        self.active = None
        self.amp = None
        self.battery = None
        self.amps = []
        self.transistor = None
        self.transistors = []
        self.sources = []
        self.names = set()
        self.standalone = False
        self.standalone_batteries = []
        self.offset = False
        self.method_parts = False
        self.inline_chain = None

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

    @property
    def group(self):
        return self.circuit.groups[self.active] if self.active is not None else None

    def read(self, text, line, binding):
        """Consume a statement in the current device, source, or group context."""
        text = text.removesuffix(";").rstrip()
        if self.transistor:
            self.transistor_field(text, binding)
            return
        call = CALL.fullmatch(text.rstrip(";").strip())
        amp_name = re.fullmatch(
            r"\(\s*opamp\s+([A-Za-z][A-Za-z0-9_]*)\s*\)", text, re.I
        )
        if (call and OP.fullmatch(call[1])) or amp_name:
            if self.amp or self.battery or self.group or self.mode == "transient":
                raise CircuitError(
                    "Use op amps at the top level or in main, in a steady-state mode."
                )
            if amp_name:
                self.amp = (amp_name[1], line, {})
            else:
                try:
                    pins = map(binding.wire, parse_wires(call[2], 3))
                    self.add_amp(binding.part(call[1]), *pins)
                except CircuitError:
                    raise CircuitError(
                        "Use op1(in+, in-, out); with three wire names."
                    ) from None
            return
        amp_end = bool(re.fullmatch(r"\(\s*end\s+opamp\s*\)", text, re.I))
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
            if self.active == self.inline_chain:
                self.active = None
            return
        if group and group.returned and not self.battery:
            raise CircuitError("Close the section with } immediately after return.")
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
                    group.taps.append((len(group.entries), name, line))
                else:
                    group.wires.append((len(group.entries), name))
            else:
                raise CircuitError(
                    "Put wire connections inside a battery, series, or parallel block."
                )
            return
        braced_battery = BATTERY_BRACE.fullmatch(text)
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
        if self.battery:
            text = BATTERY_ALIAS.sub(
                lambda match: ("Vmag" if match[1].lower() == "vrms" else "w") + " =",
                text,
            )
            self.battery_value(ASSIGNMENT.fullmatch(text), binding)
            return
        assignment = ASSIGNMENT.fullmatch(text)
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

    def add_amp(self, name, plus, minus, out):
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
            )
        )
        self.circuit.named_wires.update((plus, minus, out))

    def add_transistor(self, name, line, base, collector, emitter, polarity, model=""):
        part = Component(
            name, "Q", collector, emitter, label=name, order=self.order, base=base,
            transistor_type=polarity, model=model,
        )
        self.transistors.append((part, line))
        self.circuit.named_wires.update((base, collector, emitter))

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
        if standalone:
            self.standalone = True
            self.standalone_batteries.append(wires)
            self.start_group("series", line)
        self.add_source(kind, binding.part(label), value, dc, equation, wires)
        if standalone:
            self.active = None
        self.battery = None

    def battery_value(self, assignment, binding):
        if not assignment or not assignment[2] or "_" in assignment[1]:
            raise CircuitError(
                "Use a source value, wire name, or AC frequency inside the battery block."
            )
        key, raw = assignment.groups()
        label, known = self.battery.label, self.battery.known
        if key == "measure" and raw.upper() == "P":
            self.circuit.probes.append(Probe("P", (binding.part(label),)))
            return
        if (
            key != "Vdc"
            and label.upper().startswith("V")
            and key.casefold() == label.casefold()
        ):
            key = "V"
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
        if key == "Vdc":
            value = real(value)
            self.offset = True
        set_known(self.circuit.known if key in FREQUENCY else known, key, value)

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

    def component_name(self, name, binding):
        name = binding.part(name)
        if name.casefold() in self.names:
            raise CircuitError(f'Duplicate component name "{name}".')
        self.names.add(name.casefold())
        return name

    def group_entry(self, assignment, binding):
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
        if kind in "VI":
            label = (
                name
                or f"{'Battery' if kind == 'V' else 'Current source'} {len(self.sources) + 1}"
            )
            values = _source(raw, kind, self.mode, group=True) if raw.strip() else (None, 0, "Unspecified value")
            self.add_source(kind, label, *values)
            return
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
        kind, *targets = raw.split()
        kind = kind.upper()
        if kind in {"V", "R"} and len(targets) == 2:
            self.circuit.probes.append(Probe(kind, tuple(map(binding.wire, targets))))
        elif kind in {"V", "R"} and not targets and self.group:
            self.circuit.probes.append(Probe(kind, (), self.active))
        elif kind in {"I", "P"} and len(targets) == 1:
            self.circuit.probes.append(Probe(kind, (binding.part(targets[0]),)))
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

    def add_node_component(self, name, raw, p, n, binding):
        kind = name[0].upper()
        if kind == "Z":
            value = self.impedance_value(raw)
        if self.mode == "transient":
            raise CircuitError(
                "Use known quantities with model = rc or model = rl for a series step response."
            )
        if kind not in "RCLZVI":
            raise CircuitError(
                f'Component "{name}" needs a name beginning R, C, L, Z, V, or I.'
            )
        pulse = None
        if not raw.strip():
            value, dc, equation = None, 0, "Unspecified value"
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

    def finish(self, render_only):
        """Choose graph elaboration or legacy group reduction after parsing."""
        c = self.circuit
        if self.amp:
            raise CircuitError("Missing (end opamp).", self.amp[1])
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
        needs_graph = bool(
            self.sources
            or c.mode == "mixed"
            or c.probes
            or c.named_wires
            or any(kind == "P" for g in c.groups for kind, _, _ in g.entries)
        )
        if c.groups and (needs_graph or render_only):
            if c.legacy_measure and not render_only:
                raise CircuitError(
                    "Battery circuits already show the voltage and current for each component."
                )
            self.elaborate(needs_graph)
        elif c.groups:
            c.reduce_groups = True
        labels = {name.casefold() for e in c.components for name in (e.name, e.label)}
        for amp in self.amps:
            if amp.name.casefold() in labels:
                raise CircuitError("Give each op amp a unique component name.")
            labels.add(amp.name.casefold())
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
        if self.amps and c.grounds != {"0"}:
            self.connect_amp_returns()
        if render_only and not c.components and not c.instances:
            raise CircuitError(
                "Enter a circuit with component rows or series/parallel blocks to render it."
            )
        return c

    def connect_amp_returns(self):
        """Find each ideal op amp's output reference after wiring is resolved."""
        c = self.circuit
        branches = [
            part.terminal_nets if part.kind in {"Q", "S", "P"} else (part.p, part.n)
            for part in c.components
            if part.kind != "O"
        ]
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
        c.components = [
            replace(part, n=local.get(part.p, signal.get(part.p, "0")))
            if part.kind == "O"
            else part
            for part in c.components
        ]

    def connect_inline_chain(self):
        """Connect bare main blocks between the single supply and its return."""
        group = self.circuit.groups[self.inline_chain]
        supplies = [
            (part.n, part.p) if part.p in self.circuit.grounds else (part.p, part.n)
            for part in self.circuit.components
            if part.kind in "VI"
            and ((part.p in self.circuit.grounds) != (part.n in self.circuit.grounds))
        ]
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
        last = self.circuit.groups[group.entries[-1][2]]
        ground = last.ground if last.ground is not None else ground
        group.wires.extend(((0, supply), (len(group.entries), ground)))
        self.circuit.named_wires.update((supply, ground))

    def resolve_tap_names(self):
        c = self.circuit
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
            )
            for part in c.components
        ]
        c.wire_taps = [
            replace(tap, net=resolve_alias(tap.net, aliases)) for tap in c.wire_taps
        ]
        c.named_wires.update(tap.net for tap in c.wire_taps)
        if c.terminals:
            c.terminals = tuple(resolve_alias(node, aliases) for node in c.terminals)

    def elaborate(self, needs_graph):
        """Assign terminals to nested groups, then apply wire names and taps."""
        c = self.circuit
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
        nodes = {}
        aliases = {}
        taps = []
        entries = {}
        prefix = "@" if c.named_wires else ""
        roots = [i for i, g in enumerate(c.groups) if g.parent is None]
        for i in roots:
            nodes[i] = (f"@start{i + 1}", f"@return{i + 1}") if prefix else ("in", "0")

        def alias(node, wire, line):
            existing = node if node in c.grounds else aliases.get(node)
            if existing is not None and existing != wire:
                raise CircuitError(
                    f'Wire names "{existing}" and "{wire}" refer to the same junction.',
                    line,
                )
            aliases[node] = wire

        parts = []
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
            if group.mode == "series":
                junctions = [
                    first,
                    *(f"{prefix}g{i + 1}_{j}" for j in range(1, count)),
                    last,
                ]
                endpoints = list(zip(junctions, junctions[1:]))
                for position, name in group.wires:
                    alias(junctions[position], name, group.line)
            else:
                endpoints = [(first, last)] * count
                for position, name in group.wires:
                    if position not in {0, count}:
                        raise CircuitError(
                            "Put parallel wires before the first or after the last branch.",
                            group.line,
                        )
                    alias(first if position == 0 else last, name, group.line)
                for j, (kind, _, _) in enumerate(group.entries):
                    if kind != "P":
                        continue
                    if j + 1 == count or group.entries[j + 1][0] == "P":
                        raise CircuitError(
                            "Put a current probe immediately before the branch to measure."
                        )
                    node = f"{prefix}probe{i + 1}_{j + 1}"
                    endpoints[j], endpoints[j + 1] = (first, node), (node, last)
                for j, ground in group.branch_grounds.items():
                    if group.entries[j][0] == "P":
                        raise CircuitError(
                            "Put a branch ground after the component being measured.",
                            group.line,
                        )
                    endpoints[j] = endpoints[j][0], ground
            for position, name, line in group.taps:
                entry = min(position, count - 1)
                terminal = int(position == count)
                taps.append((i, entry, terminal, name, line))
            for j, (kind, name, value) in enumerate(group.entries):
                p, n = endpoints[j]
                if kind == "G":
                    nodes[value] = p, n
                    continue
                reversed_source = False
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
                        )
                    )
                entries[i, j] = parts[-1], reversed_source
        if prefix:
            for i in roots:
                p, n = nodes[i]
                aliases.setdefault(p, "in")
                aliases.setdefault(n, "0")

        def resolve(node):
            return resolve_alias(node, aliases)

        def terminal(group, entry, side):
            while c.groups[group].entries[entry][0] == "G":
                group = c.groups[group].entries[entry][2]
                entry = len(c.groups[group].entries) - 1 if side else 0
            part, reversed_source = entries[group, entry]
            pin = side ^ reversed_source
            return part, pin, (part.p, part.n)[pin]

        positions = []
        for group, entry, side, name, line in taps:
            part, pin, raw = terminal(group, entry, side)
            node = resolve(raw)
            if node.startswith("@") and name not in c.grounds:
                alias(node, name, line)
            positions.append((name, raw, part.name, pin, line))

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


def resolve_alias(node, aliases):
    """Follow wire aliases to their final net and report cycles."""
    seen = set()
    while node in aliases and node != aliases[node]:
        if node in seen:
            raise CircuitError("Circular wire aliases.")
        seen.add(node)
        node = aliases[node]
    return node
