"""Shared numerical result and backend contracts, independent of solver dispatch."""
# Backends return the same public result structure, independent of the analysis dispatcher.
# Internal device expansion must be folded back to the original component indices before
# UI probes or reports index branch voltages, currents, and powers.

from dataclasses import dataclass, field, replace
from typing import Protocol

import numpy as np

from circuitlab.model.graph import Circuit


# Keep this data contract independent of solver selection and GUI code.
# The dataclass stores results; it does not validate array shapes or solve
# equations. Each backend is responsible for consistent indexing and units.
@dataclass
class Network:
    """Solved values indexed by nodes and by the circuit's component order."""

    # voltage[k] belongs to nodes[k]. These node identities and their chosen
    # reference origins remain unchanged when expanded devices are folded.
    nodes: tuple[str, ...]
    # All branch_* arrays use names[k] at the same position. Internal solves
    # can name expanded legs; public results restore original component order.
    names: tuple[str, ...]
    # Node voltages are in volts. AC voltage/current entries are RMS phasors;
    # DC entries can be real arrays or complex arrays with zero imaginary parts.
    voltage: np.ndarray
    # For an ordinary branch this is V(p)-V(n). A folded pot stores its
    # high-to-low end voltage, not either individual leg voltage.
    branch_voltage: np.ndarray
    # Ordinary branch current is positive p->n. A folded pot uses its current
    # entering the high terminal; a native BJT uses collector current.
    # Additional terminal currents are supplied by the metadata maps below.
    branch_current: np.ndarray
    # Power is signed absorbed power in W for DC, or complex P+jQ for RMS AC.
    # For multi-terminal devices it includes all modeled terminal contributions;
    # it need not equal the single stored branch_voltage*conj(branch_current).
    branch_power: np.ndarray
    # Optional shared frequency metadata in Hz. The surrounding analysis
    # determines DC/AC interpretation; frequency alone is not a mode flag.
    frequency: float | None
    # Optional port result with voltage (Vth), impedance (Zth), and current
    # (Vth/Zth). current can be None when no unique finite Norton current exists.
    equivalent: dict | None = None
    # Explain a limited/unavailable port equivalent without discarding a valid
    # main operating point. An issue can coexist with partial equivalent data.
    equivalent_issue: str = ""
    # Node -> reference-node name for resolved voltage domains. Separate domains
    # need not share node 0. Each default_factory=dict creates a fresh map
    # when a Network is constructed, keeping its initial metadata independent.
    references: dict[str, str] = field(default_factory=dict)
    # Native BJT base current keyed by component name, positive into the base.
    # Together with collector current, it determines emitter current by KCL.
    base_current: dict[str, float] = field(default_factory=dict)
    # Optional multi-terminal metadata. For a pot the tuple order is
    # (high, wiper, low), with every current positive entering the device.
    terminal_current: dict[str, tuple] = field(default_factory=dict)


# Sum both leg powers but retain each terminal current, using the same external sign
# conventions as the original high/wiper/low component.
def fold_potentiometers(network, circuit, mapping):
    """Keep public branch arrays in source component order after electrical expansion."""
    # No expansion needs undoing: preserve the original result object directly.
    if not mapping:
        return network
    # mapping[pot_name] gives (high_leg_index, low_leg_index) into this expanded
    # network. Other components are recovered by name because preceding pot
    # expansions can shift their numerical branch positions.
    indices = {name: i for i, name in enumerate(network.names)}
    nodes = {name: i for i, name in enumerate(network.nodes)}
    voltages, currents, powers = [], [], []
    # Preserve metadata for other devices while adding/replacing pot entries.
    # Copy the dictionary so folding does not mutate the input result's map.
    terminal_current = dict(network.terminal_current)
    # Build all replacement branch arrays in the original source component
    # order. Reports/probes can then zip them with circuit.components safely.
    for part in circuit.components:
        if part.kind == "P":
            high, low = mapping[part.name]
            # None marks an omitted leg whose two terminals already share one net.
            # That redundant leg contributes zero here. A zero-resistance endpoint
            # leg between distinct nets is retained as a 0-V branch and still has
            # a solved current; it is not represented by None merely because R=0.
            high_current = network.branch_current[high] if high is not None else 0
            low_current = network.branch_current[low] if low is not None else 0
            # Read the physical end-to-end voltage from the node solution, so this
            # also works when a leg was omitted or an end is tied to the wiper.
            voltages.append(network.voltage[nodes[part.p]] - network.voltage[nodes[part.n]])
            # Use the high-terminal entering current as the pot's single branch-current
            # convention. A loaded wiper means the low-leg current can differ from it.
            currents.append(high_current)
            # Sum the two leg powers instead of multiplying end voltage by high current.
            # A driven wiper can dissipate power even when the high and low pins are
            # on the same net and their end-to-end voltage is zero.
            powers.append(sum(network.branch_power[i] for i in (high, low) if i is not None))
            # Leg directions are high->wiper (Ih) and wiper->low (Il). Convert to
            # currents entering the complete device: (Ih, Il-Ih, -Il). Their sum is
            # zero by KCL, for real DC currents and complex AC phasors alike.
            terminal_current[part.name] = (high_current, low_current - high_current, -low_current)
        # Non-pot components retain their solved voltage/current/power values;
        # only their positions in the replacement arrays are restored.
        else:
            i = indices[part.name]
            voltages.append(network.branch_voltage[i])
            currents.append(network.branch_current[i])
            powers.append(network.branch_power[i])
    # Return a new dataclass with new branch arrays and terminal-current map.
    # replace() is shallow: unchanged fields, including node voltages and other
    # metadata, are shared with the original result rather than deep-copied.
    # np.asarray retains complex AC values when constructing the arrays.
    return replace(network, names=tuple(part.name for part in circuit.components),
                   branch_voltage=np.asarray(voltages), branch_current=np.asarray(currents),
                   branch_power=np.asarray(powers), terminal_current=terminal_current)


# A structural typing interface: a compatible backend supplies solve() with
# this contract without needing to inherit from this Protocol. This does
# not select a solver or perform runtime validation of returned arrays.
class SimulationBackend(Protocol):
    # Receive an already compiled graph. Keyword-only ac chooses the solution
    # domain; frequency may be None where the model permits it. An optional
    # two-node port requests equivalent data when supported by the backend.
    # Concrete implementations return the shared Network described above.
    def solve(
        self,
        circuit: Circuit,
        *,
        ac: bool,
        frequency: float | None,
        port: tuple[str, str] | None = None,
    ) -> Network:
        """Solve one linear operating point from a resolved circuit graph."""
        # Interface placeholder only; numerical implementations live in backends.
        ...
