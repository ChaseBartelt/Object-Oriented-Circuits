"""Select the immediate digital instances and connections at one hierarchy level."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import re
from types import MappingProxyType

from circuitlab.layout.hierarchy import METHOD_PREFIX


MAX_BLOCKS, MAX_CONNECTIONS = 120, 400
ACRONYMS = {"alu", "cpu", "io", "mmio", "ram", "rom"}


def _title(name):
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).split()
    return " ".join(word.upper() if word.casefold() in ACRONYMS else word for word in words)


@dataclass(frozen=True)
class LogicPort:
    """A formal port and the exact signal to probe in the running simulation."""

    label: str
    live: str
    width: int
    direction: str


@dataclass(frozen=True)
class LogicBlock:
    """A named child chip that can be opened independently of its siblings."""

    name: str
    title: str
    instance: int
    ports: tuple[LogicPort, ...]
    kind: str = "chip"
    width: int = 0

    @property
    def caption(self):
        inputs = sum(port.direction == "input" for port in self.ports)
        return f"{inputs} inputs · {len(self.ports) - inputs} outputs"

    @property
    def detail(self):
        return "\n".join([
            self.title,
            *(f"{port.label}  {port.direction}, {port.width} bit · {port.live}" for port in self.ports),
        ])

@dataclass(frozen=True)
class LogicHierarchyView:
    blocks: Mapping[str, LogicBlock]
    edge_signals: Mapping[tuple[str, str], tuple[str, ...]]
    notice: str

    @property
    def edges(self):
        return tuple(self.edge_signals)


def logic_hierarchy(simulation, instances, scope=None):
    """Describe sibling chips and the live signals each producer sends to a consumer.

    Parent-level inputs and outputs are available through their child port
    bindings. A scope without children returns an empty view so the caller can
    open the signal-level detail.
    """
    children = [(index, instance) for index, instance in enumerate(instances)
                if instance.parent == scope and instance.port_directions]
    totals = Counter(instance.name for _, instance in children)
    occurrence = Counter()
    blocks = {}
    drivers, readers = defaultdict(list), defaultdict(list)
    for index, instance in children:
        occurrence[instance.name] += 1
        name = f"{METHOD_PREFIX}{index}"
        title = _title(instance.name)
        if totals[instance.name] > 1:
            title += f" {occurrence[instance.name]}"
        directions = dict(instance.port_directions)
        ports = []
        for label, bound in instance.pins:
            live = simulation.source_bindings.get(bound, bound)
            direction = directions.get(label)
            if direction not in {"input", "output"} or live not in simulation.signals:
                continue
            ports.append(LogicPort(label, live, simulation.signals[live], direction))
            (drivers if direction == "output" else readers)[live].append(name)
        blocks[name] = LogicBlock(name, title, index, tuple(ports))

    edge_signals = defaultdict(dict)
    for live, sources in drivers.items():
        for source in sources:
            for target in readers[live]:
                edge_signals[source, target][live] = None
    notice = ""
    if len(blocks) > MAX_BLOCKS or len(edge_signals) > MAX_CONNECTIONS:
        notice = (f"{len(blocks)} chips / {len(edge_signals)} connections exceed the hierarchy view limit "
                  f"({MAX_BLOCKS} chips / {MAX_CONNECTIONS} connections).")
    return LogicHierarchyView(MappingProxyType(blocks), MappingProxyType({
        edge: tuple(signals) for edge, signals in edge_signals.items()
    }), notice)
