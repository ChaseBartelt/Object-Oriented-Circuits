"""Shared numerical result and backend contracts, independent of solver dispatch."""

from dataclasses import dataclass, field
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
