"""Shared numerical result and backend contracts, independent of solver dispatch."""

from dataclasses import dataclass, field, replace
from typing import Protocol

import numpy as np

from circuitlab.model.graph import Circuit


@dataclass
class Network:
    """Solved values indexed by nodes and by the circuit's component order."""

    nodes: tuple[str, ...]
    names: tuple[str, ...]
    voltage: np.ndarray
    branch_voltage: np.ndarray
    branch_current: np.ndarray
    branch_power: np.ndarray
    frequency: float | None
    equivalent: dict | None = None
    equivalent_issue: str = ""
    references: dict[str, str] = field(default_factory=dict)
    base_current: dict[str, float] = field(default_factory=dict)
    terminal_current: dict[str, tuple] = field(default_factory=dict)


def fold_potentiometers(network, circuit, mapping):
    """Keep public branch arrays in source component order after electrical expansion."""
    if not mapping:
        return network
    indices = {name: i for i, name in enumerate(network.names)}
    nodes = {name: i for i, name in enumerate(network.nodes)}
    voltages, currents, powers = [], [], []
    terminal_current = dict(network.terminal_current)
    for part in circuit.components:
        if part.kind == "P":
            high, low = mapping[part.name]
            high_current = network.branch_current[high] if high is not None else 0
            low_current = network.branch_current[low] if low is not None else 0
            voltages.append(network.voltage[nodes[part.p]] - network.voltage[nodes[part.n]])
            currents.append(high_current)
            powers.append(sum(network.branch_power[i] for i in (high, low) if i is not None))
            terminal_current[part.name] = (high_current, low_current - high_current, -low_current)
        else:
            i = indices[part.name]
            voltages.append(network.branch_voltage[i])
            currents.append(network.branch_current[i])
            powers.append(network.branch_power[i])
    return replace(network, names=tuple(part.name for part in circuit.components),
                   branch_voltage=np.asarray(voltages), branch_current=np.asarray(currents),
                   branch_power=np.asarray(powers), terminal_current=terminal_current)


class SimulationBackend(Protocol):
    def solve(
        self,
        circuit: Circuit,
        *,
        ac: bool,
        frequency: float | None,
        port: tuple[str, str] | None = None,
    ) -> Network:
        """Solve one linear operating point from a resolved circuit graph."""
        ...
