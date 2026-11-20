"""Data shared by the compiler, solvers, and schematic."""
# The graph stores electrical identity, not pixel positions or Qt objects.
# Component ordering is shared with solver result arrays, while named aliases and source
# locations let reports and schematic gestures refer back to the same electrical objects.

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
import math

Scalar = float | complex


class CircuitError(ValueError):
    def __init__(self, message: str, line: int | None = None, code: str = "", *, path=None):
        self.message = message
        self.line = line
        self.code = code
        self.path = path
        location = f"Line {line}. " if line else ""
        super().__init__(f"{path.name}: {location}{message}" if path else location + message)


@dataclass(frozen=True)
class Pulse:
    low: float
    high: float
    delay: float
    rise: float
    fall: float
    width: float
    period: float

    def at(self, time):
        if time <= self.delay:
            return self.low
        phase = (time - self.delay) % self.period
        if phase < self.rise:
            fraction = phase / self.rise
        elif phase < self.rise + self.width:
            fraction = 1
        elif phase < self.rise + self.width + self.fall:
            fraction = (self.rise + self.width + self.fall - phase) / self.fall
        else:
            fraction = 0
        return self.low + (self.high - self.low) * fraction

    def spice(self):
        values = (self.low, self.high, self.delay, self.rise, self.fall, self.width, self.period)
        return "PULSE(" + " ".join(f"{value:.15g}" for value in values) + ")"


@dataclass(frozen=True)
class VoltageLimit:
    """A voltage bound relative to return, optionally tracking either rail."""

    constant: float = 0.0
    vplus: float = 0.0
    vminus: float = 0.0

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.constant, self.vplus, self.vminus)):
            raise CircuitError("Common-mode voltage bounds must be finite.")

    def at(self, high=0.0, low=0.0):
        return self.constant + self.vplus * high + self.vminus * low

    def source(self):
        terms = [name if coefficient == 1 else f"{coefficient:.8g} * {name}"
                 for name, coefficient in (("vplus", self.vplus), ("vminus", self.vminus)) if coefficient]
        if self.constant or not terms:
            terms.append(f"{self.constant:.8g}V")
        return " + ".join(terms).replace("+ -", "- ")


def validate_common_model(model):
    for key in ("common_min", "common_max"):
        value = getattr(model, key)
        if value is not None and not isinstance(value, VoltageLimit):
            raise CircuitError(f"Invalid {key.replace('_', '.')} voltage bound.")
    for key in ("common_rejection", "common_impedance"):
        value = getattr(model, key)
        if value is None:
            continue
        try:
            valid = math.isfinite(value) and (value >= 0 if key == "common_rejection" else value > 0)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise CircuitError(f"Invalid {key.replace('_', '.')}.")


@dataclass(frozen=True)
class OpAmpModel:
    """Optional behavioral limits; None preserves ideal gain/bandwidth/slew."""

    gain: float | None = None
    bandwidth: float | None = None  # Gain-bandwidth product, Hz.
    slew: float | None = None       # Maximum output slope, V/s.
    offset: float = 0.0             # Input-referred voltage added to in+.
    supply_current: float = 0.0     # Constant current from V+ to V-.
    common_min: VoltageLimit | None = None
    common_max: VoltageLimit | None = None
    common_rejection: float | None = None  # CMRR in dB; None is ideal rejection.
    common_impedance: float | None = None  # Each input to output return, ohms.
    output_min: VoltageLimit | None = None
    output_max: VoltageLimit | None = None

    def __post_init__(self):
        for key in ("gain", "bandwidth", "slew", "offset", "supply_current"):
            value = getattr(self, key)
            if key in {"gain", "bandwidth", "slew"} and value is None:
                continue
            try:
                valid = math.isfinite(value) and (value > 0 if key in {"gain", "bandwidth", "slew"}
                                                 else value >= 0 if key == "supply_current" else True)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise CircuitError(f"Invalid op-amp {key.replace('_', '.')}.")
        validate_common_model(self)
        for key in ("output_min", "output_max"):
            value = getattr(self, key)
            if value is not None and not isinstance(value, VoltageLimit):
                raise CircuitError(f"Invalid {key.replace('_', '.')} voltage bound.")

    @property
    def inverse_gain(self):
        return 0.0 if self.gain is None else 1.0 / self.gain

    @property
    def required_rails(self):
        limits = (self.common_min, self.common_max, self.output_min, self.output_max)
        return (bool(self.supply_current or any(limit and limit.vplus for limit in limits)),
                bool(self.supply_current or any(limit and limit.vminus for limit in limits)))


@dataclass(frozen=True)
class MathEquation:
    """A solver-only voltage equation, separate from visible components."""

    name: str
    output: str
    reference: str
    tree: tuple
    inputs: tuple[str, ...]
    digital: bool = False
    line: int | None = None
    path: object = None
    owner: int | None = None
    priority: int = 0


@dataclass(frozen=True)
class ComparatorModel:
    offset: float = 0.0
    delay_rise: float | None = None
    delay_fall: float | None = None
    response_time: float = 0.0
    common_min: VoltageLimit | None = None
    common_max: VoltageLimit | None = None
    output_type: str = "open_collector"
    output_low_at4ma: float = 0.1
    supply_current: float = 0.0

    def __post_init__(self):
        for key in ("offset", "delay_rise", "delay_fall", "response_time", "output_low_at4ma", "supply_current"):
            value = getattr(self, key)
            if key in {"delay_rise", "delay_fall"} and value is None:
                continue
            if (not math.isfinite(value) or (key != "offset" and value < 0)
                    or (key == "output_low_at4ma" and value == 0)):
                raise CircuitError(f"Invalid comparator {key.replace('_', '.')}.")
        for limit in (self.common_min, self.common_max):
            if limit is not None and not isinstance(limit, VoltageLimit):
                raise CircuitError("Comparator common-mode limits require voltage bounds.")
        if self.output_type != "open_collector":
            raise CircuitError("Comparator output.type must be open_collector.")

    @property
    def rise(self):
        return self.response_time if self.delay_rise is None else self.delay_rise

    @property
    def fall(self):
        return self.response_time if self.delay_fall is None else self.delay_fall


@dataclass(frozen=True)
class ComparatorInstance:
    name: str
    owner: int
    output_name: str
    plus: str
    minus: str
    collector: str
    emitter: str
    vplus: str
    vminus: str
    members: tuple[str, ...]
    internal_nodes: frozenset[str]
    model: ComparatorModel
    line: int | None = None
    path: object = None
    explicit_supply: bool = False


@dataclass(frozen=True)
class AudioAmpModel:
    """Behavioral audio core: gain network, midrail bias and supply draw."""

    gain_default: float = 20.0
    gain_max: float = 200.0
    gain_resistor: float = 1350.0
    output_bias: float = 0.5
    supply_current: float = 0.004
    bypass_resistance: float = 50000.0
    output_type: str = "power_amplifier"

    def __post_init__(self):
        positive = (self.gain_default, self.gain_max, self.gain_resistor, self.bypass_resistance)
        try:
            valid = (all(math.isfinite(v) and v > 0 for v in positive)
                     and self.gain_max > self.gain_default and 0 <= self.output_bias <= 1
                     and math.isfinite(self.supply_current) and self.supply_current >= 0
                     and self.output_type == "power_amplifier")
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise CircuitError("Invalid audio amplifier model: require 0 < gain.default < gain.max, "
                               "positive gain/bypass resistances, bias from 0 to 1, and nonnegative supply current.")


@dataclass(frozen=True)
class Component:
    """An electrical part with its oriented terminals and model parameters."""

    name: str
    kind: str
    p: str
    n: str
    # None is an unspecified component value, never a zero or open circuit.
    value: Scalar | None = 0
    dc: Scalar = 0
    label: str = ""
    equation: str = "Given"
    in_plus: str = ""
    in_minus: str = ""
    order: int = field(default=0, compare=False, repr=False)
    base: str = ""
    transistor_type: str = ""
    model: str = ""
    package_pins: tuple[tuple[str, str], ...] = ()
    threshold: float = 0.0
    off_resistance: float = 0.0
    pulse: Pulse | None = None
    wiper: str = ""
    position: float = 0.5
    # Native two-terminal schematic switches (kind K); kind S is controlled.
    closed: bool = False
    # Optional schematic supply pins on ideal op amps; no rail clipping model.
    vplus: str = ""
    vminus: str = ""
    opamp: OpAmpModel | None = None
    behavior: MathEquation | None = None
    audio: AudioAmpModel | None = None
    gain1: str = ""
    gain8: str = ""
    bypass: str = ""
    dc_only: bool = False  # Private bias sources remain DC during AC time simulation.

    @property
    def is_source(self):
        return self.kind in "VI" and self.equation != "V = I x 0 = 0"

    # Expose all meaningful terminal identities to schematic and connector code. Optional power
    # labels can appear here even when the ideal numerical model does not consume them.
    @property
    def terminal_nets(self):
        if self.kind == "A":
            return (self.in_plus, self.in_minus, self.p, self.vplus, self.vminus,
                    self.gain1, self.gain8, self.bypass)
        if self.kind == "B" and self.behavior:
            return tuple(dict.fromkeys((self.p, self.n, *self.behavior.inputs)))
        if self.kind == "H":
            return tuple(net for _, net in self.package_pins)
        if self.kind == "Q":
            return self.base, self.p, self.n
        if self.kind == "P":
            return self.p, self.wiper, self.n
        if self.kind == "O" and self.vplus and self.vminus:
            return self.in_plus, self.in_minus, self.p, self.n, self.vplus, self.vminus
        if self.kind in {"O", "S", "G"}:
            return self.in_plus, self.in_minus, self.p, self.n
        return self.p, self.n


@dataclass(frozen=True)
class Probe:
    """A measurement of nodes or a component requested by the source text."""

    kind: str
    targets: tuple[str, ...]
    line: int | None = None


@dataclass(frozen=True)
class WireTap:
    """A named alias attached to a specific component terminal in the drawing."""

    name: str
    net: str
    component: str
    terminal: int


@dataclass(frozen=True)
class MethodInstance:
    """An expanded method and its named connections to the caller."""

    kind: str
    name: str
    parent: int | None
    pins: tuple[tuple[str, str], ...]
    body: tuple[str, ...] = ()
    line: int | None = None
    arguments: tuple[tuple[str, str], ...] = ()
    path: object = None
    model_available: bool = True
    builtin_gate: bool = False
    signal_bindings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ChipProperty:
    name: str
    value: Scalar
    unit: str = ""
    scope: str = ""
    owner: int | None = None
    line: int | None = None
    path: object = None


@dataclass(frozen=True)
class ModelIssue:
    """Unresolved numerical behavior retained by a topology-only compilation."""

    message: str
    line: int | None = None
    path: object = None
    parameter: str = ""
    raw: str = ""
    owner: int | None = None


@dataclass
class Circuit:
    """The compiled graph shared by the solver, schematic, and exporters."""

    mode: str
    known: dict[str, Scalar] = field(default_factory=dict)
    equations: dict[str, str] = field(default_factory=dict)
    components: list[Component] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    port: tuple[str, str] | None = None
    model: str = "generic"
    named_wires: set[str] = field(default_factory=set)
    grouped: bool = False
    terminals: tuple[str, str] | None = None
    groups: list = field(default_factory=list, repr=False)
    reduce_groups: bool = False
    legacy_measure: str = ""
    grounds: set[str] = field(default_factory=lambda: {"0"})
    wire_taps: list[WireTap] = field(default_factory=list)
    instances: list[MethodInstance] = field(default_factory=list)
    owners: dict[str, int] = field(default_factory=dict)
    component_lines: dict[str, int] = field(default_factory=dict)
    component_files: dict = field(default_factory=dict)
    connector_pins: tuple[str, ...] = ()
    behavior: list[MathEquation] = field(default_factory=list)
    properties: list[ChipProperty] = field(default_factory=list)
    model_issues: list[ModelIssue] = field(default_factory=list)
    comparators: list[ComparatorInstance] = field(default_factory=list)

    def resolve_wire(self, name: str) -> str:
        return next((tap.net for tap in self.wire_taps if tap.name == name), name)

    @property
    def simulation_nodes(self):
        hidden = set().union(*(part.internal_nodes for part in self.comparators))
        return tuple(node for node in self.nodes if node not in hidden)

    @property
    def modeled_members(self):
        return {name for part in self.comparators for name in part.members}

    def require_models(self):
        if self.model_issues:
            issue = self.model_issues[0]
            raise CircuitError("Numerical model is incomplete: " + issue.message,
                               issue.line, "UnsupportedModelProperty", path=issue.path)
        missing = next((instance for instance in self.instances if not instance.model_available), None)
        if missing:
            raise CircuitError(
                f"{missing.name} is available as a schematic pinout. A simulation model is required.",
                missing.line, "UnavailableChipModel", path=missing.path,
            )

    def require_values(self):
        """Reject schematic placeholders before any numerical interpretation."""
        missing = [(part.label or part.name, self.component_lines.get(part.name), self.component_files.get(part.name))
                   for part in self.components if part.value is None and part.name not in self.modeled_members]
        if not missing:
            for group in self.groups:
                for index, (kind, name, value) in enumerate(group.entries):
                    if value is None:
                        line, path = group.entry_sources.get(index, (group.line, None))
                        missing.append((name or kind, line, path))
        if missing:
            names = ", ".join(name for name, _, _ in missing)
            raise CircuitError(
                f"Enter a value for {names} before simulation. Parts without values are schematic placeholders.",
                missing[0][1], "MissingComponentValue", path=missing[0][2],
            )

    # Build the numerical node order consistently with branch arrays. Supply-only identifiers on
    # ideal op amps are omitted unless another electrical component actually uses those nets.
    @property
    def nodes(self) -> tuple[str, ...]:
        # Preserve terminal-category order without building an intermediate list.
        nets = chain(
            ("0",),
            (e.p for e in self.components if e.kind != "H"),
            (e.n for e in self.components if e.kind != "H"),
            (e.in_plus for e in self.components if e.kind in {"O", "S"}),
            (e.in_minus for e in self.components if e.kind in {"O", "S"}),
            (e.base for e in self.components if e.kind == "Q"),
            (e.wiper for e in self.components if e.kind == "P"),
            (net for e in self.components if e.kind == "O" and e.opamp
             for net, required in zip((e.vplus, e.vminus), e.opamp.required_rails) if required),
            (net for e in self.components if e.kind == "H" for net in e.terminal_nets),
            (net for e in self.components if e.kind == "B" for net in e.terminal_nets),
            (net for e in self.components if e.kind in {"A", "G"} for net in e.terminal_nets),
            (net for equation in self.behavior
             for net in (equation.output, equation.reference, *equation.inputs)),
        )
        return tuple(dict.fromkeys(nets))


# Traverse connected terminal sets and assign one reference per connected domain.
# An explicitly named ground does not automatically short every domain to node 0.
def ground_references(grounds, connections):
    """Map connected nodes to their voltage reference, if they have one."""
    neighbors = defaultdict(set)
    for terminals in connections:
        terminals = set(terminals)
        for node in terminals:
            neighbors[node].update(terminals - {node})
    seen, references = set(), {}
    for node in neighbors:
        if node in seen:
            continue
        domain = [node]
        seen.add(node)
        for member in domain:
            unseen = neighbors[member] - seen
            seen.update(unseen)
            domain.extend(unseen)
        returns = grounds.intersection(domain)
        if returns:
            reference = "0" if "0" in returns else min(returns)
            references.update(dict.fromkeys(domain, reference))
    return references
