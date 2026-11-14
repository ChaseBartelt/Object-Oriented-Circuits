"""Data shared by the compiler, solvers, and schematic."""

from dataclasses import dataclass, field

Scalar = float | complex


class CircuitError(ValueError):
    def __init__(self, message: str, line: int | None = None, code: str = ""):
        self.line = line
        self.code = code
        super().__init__(f"Line {line}. {message}" if line else message)


@dataclass(frozen=True)
class Component:
    """An oriented branch or an ideal op amp with separate sensing pins."""

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

    @property
    def is_source(self):
        return self.kind in "VI" and self.equation != "V = I x 0 = 0"


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

    def resolve_wire(self, name: str) -> str:
        return next((tap.net for tap in self.wire_taps if tap.name == name), name)

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [
                    "0",
                    *(e.p for e in self.components),
                    *(e.n for e in self.components),
                    *(e.in_plus for e in self.components if e.kind == "O"),
                    *(e.in_minus for e in self.components if e.kind == "O"),
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
