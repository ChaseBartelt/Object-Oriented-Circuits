"""Data shared by the compiler, solvers, and schematic."""

from dataclasses import dataclass, field

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
class Component:
    """An oriented branch, an ideal op amp, or a bipolar transistor."""

    name: str
    kind: str
    p: str
    n: str
    value: Scalar = 0
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

    @property
    def is_source(self):
        return self.kind in "VI" and self.equation != "V = I x 0 = 0"

    @property
    def terminal_nets(self):
        if self.kind == "H":
            return tuple(net for _, net in self.package_pins)
        if self.kind == "Q":
            return self.base, self.p, self.n
        if self.kind == "O":
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

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [
                    "0",
                    *(e.p for e in self.components if e.kind != "H"),
                    *(e.n for e in self.components if e.kind != "H"),
                    *(e.in_plus for e in self.components if e.kind == "O"),
                    *(e.in_minus for e in self.components if e.kind == "O"),
                    *(e.base for e in self.components if e.kind == "Q"),
                    *(net for e in self.components if e.kind == "H" for net in e.terminal_nets),
                ]
            )
        )


def ground_references(grounds, connections):
    """Map connected nodes to their voltage reference, if they have one."""
    neighbors = {}
    for terminals in connections:
        terminals = set(terminals)
        for node in terminals:
            neighbors.setdefault(node, set()).update(terminals - {node})
    seen, references = set(), {}
    for node in neighbors:
        if node in seen:
            continue
        domain = [node]
        seen.add(node)
        for member in domain:
            for neighbor in neighbors[member] - seen:
                seen.add(neighbor)
                domain.append(neighbor)
        returns = grounds.intersection(domain)
        if returns:
            reference = "0" if "0" in returns else min(returns)
            references.update(dict.fromkeys(domain, reference))
    return references
