"""Data shared by the compiler, solvers, and schematic."""

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain

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

    @property
    def is_source(self):
        return self.kind in "VI" and self.equation != "V = I x 0 = 0"

    @property
    def terminal_nets(self):
        if self.kind == "H":
            return tuple(net for _, net in self.package_pins)
        if self.kind == "Q":
            return self.base, self.p, self.n
        if self.kind == "P":
            return self.p, self.wiper, self.n
        if self.kind == "O" and self.vplus and self.vminus:
            return self.in_plus, self.in_minus, self.p, self.n, self.vplus, self.vminus
        if self.kind in {"O", "S"}:
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

    def resolve_wire(self, name: str) -> str:
        return next((tap.net for tap in self.wire_taps if tap.name == name), name)

    def require_models(self):
        missing = next((instance for instance in self.instances if not instance.model_available), None)
        if missing:
            raise CircuitError(
                f"{missing.name} is available as a schematic pinout. A simulation model is required.",
                missing.line, "UnavailableChipModel", path=missing.path,
            )

    def require_values(self):
        """Reject schematic placeholders before any numerical interpretation."""
        missing = [(part.label or part.name, self.component_lines.get(part.name), self.component_files.get(part.name))
                   for part in self.components if part.value is None]
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
            (net for e in self.components if e.kind == "H" for net in e.terminal_nets),
        )
        return tuple(dict.fromkeys(nets))


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
