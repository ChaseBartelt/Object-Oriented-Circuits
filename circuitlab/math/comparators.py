"""Numerical open-collector comparator macros with preserved display topology.

The source graph retains its transistor schematic. Only the numerical copy
replaces an explicitly configured comparator's members with a delayed sink
and bias load. Interior transistor values are therefore unavailable, rather
than fabricated measurements of a circuit that was not electrically solved.
"""

from dataclasses import replace
import math

import numpy as np

from circuitlab.model.graph import CircuitError, Component, MathEquation


OFF_RESISTANCE = 1e12


def _root(instance):
    return "@comparator:" + instance.name


def _equation_nodes(equation):
    nodes = {equation.output, equation.reference, *equation.inputs}
    pending = [equation.tree]
    while pending:
        tree = pending.pop()
        if tree and tree[0] in {"v", "bit"}:
            nodes.update(tree[1:3])
        pending.extend(child for child in tree if isinstance(child, tuple))
    return nodes


def _validate_connections(circuit, instance, parts):
    """Recheck the binding when callers mutate an already compiled graph."""
    output = parts[instance.output_name]
    if (output.kind != "Q" or output.p != instance.collector or output.n != instance.emitter):
        raise CircuitError("Comparator output transistor terminals no longer match its numerical model.",
                           instance.line, "ComparatorModel", path=instance.path)
    members = set(instance.members)
    allowed = instance.internal_nodes | {instance.plus, instance.minus, instance.collector,
                                         instance.emitter, instance.vplus, instance.vminus, "0"}
    if any(set(parts[name].terminal_nets) - allowed for name in members):
        raise CircuitError("Comparator source terminals no longer match its numerical model.",
                           instance.line, "ComparatorModel", path=instance.path)
    outside = set()
    for part in circuit.components:
        if part.name not in members:
            outside.update(part.terminal_nets)
            if part.behavior is not None:
                outside.update(_equation_nodes(part.behavior))
    for equation in circuit.behavior:
        outside.update(_equation_nodes(equation))
    outside.update(circuit.resolve_wire(net) for net in circuit.connector_pins)
    outside.update(tap.net for tap in circuit.wire_taps)
    if instance.internal_nodes & outside:
        raise CircuitError("Behavioral comparator internals cannot be connected externally; use the declared method pins.",
                           instance.line, "ComparatorModel", path=instance.path)


def _parts(instance, mode):
    model = instance.model
    resistance = model.output_low_at4ma / .004
    if (not math.isfinite(resistance) or resistance <= 0
            or resistance >= OFF_RESISTANCE or not math.isfinite(1 / resistance)):
        raise CircuitError("Comparator output.low.at4mA must describe a finite positive on-resistance below its off-resistance.",
                           instance.line, "ComparatorModel", path=instance.path)
    root = _root(instance)
    control = root + ":control"
    # The delayed signal means collector HIGH/open. Consequently its rising
    # edge uses delay.rise, although the output transistor is turning OFF.
    difference = ("binary", "+", ("v", instance.plus, instance.minus), ("c", model.offset))
    high = ("compare", (">=",), difference, ("c", 0.0))
    delayed = ("state", "transition", instance.name + "#high", high,
               ("c", model.rise), ("c", model.fall))
    sink = ("binary", "-", ("c", 1.0), ("binary", "*", ("c", 2.0), delayed))
    equation = MathEquation(root + ":driver", control, instance.emitter, sink,
                            tuple(dict.fromkeys((instance.plus, instance.minus))),
                            line=instance.line, path=instance.path, owner=instance.owner)
    parts = [
        Component(instance.output_name, "S", instance.collector, instance.emitter, resistance,
                  label=instance.name, in_plus=control, in_minus=instance.emitter,
                  threshold=0.0, off_resistance=OFF_RESISTANCE),
        Component(equation.name, "B", control, instance.emitter, behavior=equation),
    ]
    if model.supply_current:
        parts.append(Component(root + ":supply", "I", instance.vplus, instance.vminus,
                               model.supply_current if mode == "dc" else 0.0,
                               dc=model.supply_current, dc_only=True))
    return parts


def expand_comparators(circuit):
    """Replace configured members only in a private graph used for solving."""
    if not circuit.comparators:
        return circuit
    members = set()
    hidden = []
    parts = {part.name: part for part in circuit.components}
    for instance in circuit.comparators:
        if instance.output_name not in instance.members or not set(instance.members) <= parts.keys():
            raise CircuitError("Comparator numerical model no longer matches its source components.",
                               instance.line, "ComparatorModel", path=instance.path)
        if members.intersection(instance.members):
            raise CircuitError("Comparator numerical models may not overlap.", instance.line,
                               "ComparatorModel", path=instance.path)
        _validate_connections(circuit, instance, parts)
        members.update(instance.members)
        hidden.extend(_parts(instance, circuit.mode))
    return replace(circuit, components=[part for part in circuit.components if part.name not in members] + hidden,
                   comparators=[])


def fold_comparators(network, original):
    """Restore public branch order and explicitly identify unresolved interiors."""
    if not original.comparators:
        return network
    indices = {name: i for i, name in enumerate(network.names)}
    node_indices = {node: i for i, node in enumerate(network.nodes)}
    outputs = {instance.output_name: instance for instance in original.comparators}
    members = set().union(*(instance.members for instance in original.comparators))
    names = tuple(part.name for part in original.components)
    visible = set(names)
    base_current = {name: value for name, value in network.base_current.items()
                    if name in visible and name not in members}
    terminal_current = {name: value for name, value in network.terminal_current.items()
                        if name in visible and name not in members}
    modeled = {name: value for name, value in getattr(network, "modeled_components", {}).items() if name in visible}
    modeled_terminals = {name: value for name, value in getattr(network, "modeled_terminals", {}).items() if name in visible}
    voltages, currents, powers = [], [], []
    for part in original.components:
        instance = outputs.get(part.name)
        if instance is not None:
            index = indices[part.name]
            sink = network.branch_current[index]
            supply_name = _root(instance) + ":supply"
            supply = network.branch_current[indices[supply_name]] if supply_name in indices else 0.0
            power = network.branch_power[index]
            # Include all private macro branches, particularly the shared bias
            # load allocated to this channel, exactly once in public power.
            power += sum(network.branch_power[i] for name, i in indices.items()
                         if name.startswith(_root(instance) + ":"))
            voltages.append(network.branch_voltage[index])
            currents.append(sink)
            powers.append(power)
            base_current[part.name] = 0.0
            terminal_current[part.name] = (0.0, sink, -sink)
            modeled[part.name] = "Behavioral open-collector comparator output and quiescent supply draw"
            modeled_terminals[part.name] = (
                ("collector", instance.collector, sink),
                ("emitter", instance.emitter, -sink),
                ("V+", instance.vplus, supply),
                ("V-", instance.vminus, -supply),
                ("in+", instance.plus, 0.0),
                ("in-", instance.minus, 0.0),
            )
        elif part.name in members:
            # These entries preserve indexing only. Every reporting/probing
            # entry point must honor unavailable_components before reading them.
            voltages.append(0.0)
            currents.append(0.0)
            powers.append(0.0)
        else:
            index = indices[part.name]
            voltages.append(network.branch_voltage[index])
            currents.append(network.branch_current[index])
            powers.append(network.branch_power[index])
    nodes = original.simulation_nodes
    if any(node not in node_indices for node in nodes):
        missing = next(node for node in nodes if node not in node_indices)
        raise CircuitError(f'No numerical comparator value is available for node "{missing}".', code="ComparatorModel")
    unavailable = frozenset(getattr(network, "unavailable_components", ())) | (members - outputs.keys())
    unavailable_nodes = frozenset(getattr(network, "unavailable_nodes", ())) | set().union(
        *(instance.internal_nodes for instance in original.comparators))
    return replace(network, nodes=nodes, names=names,
                   voltage=network.voltage[[node_indices[node] for node in nodes]],
                   branch_voltage=np.asarray(voltages), branch_current=np.asarray(currents),
                   branch_power=np.asarray(powers), references={n: ref for n, ref in network.references.items() if n in nodes},
                   base_current=base_current, terminal_current=terminal_current,
                   unavailable_components=frozenset(unavailable), unavailable_nodes=frozenset(unavailable_nodes),
                   modeled_components=modeled, modeled_terminals=modeled_terminals)


def validate_comparator_ranges(circuit, values):
    """Validate common-mode bounds at accepted DC or transient endpoints."""
    for instance in circuit.comparators:
        model = instance.model
        limits = (model.common_min, model.common_max)
        if not any(limit is not None for limit in limits):
            continue
        reference = values[instance.emitter]
        high = values[instance.vplus] - reference if any(v and v.vplus for v in limits) else 0.0
        low = values[instance.vminus] - reference if any(v and v.vminus for v in limits) else 0.0
        lower = model.common_min.at(high, low).real if model.common_min else -math.inf
        upper = model.common_max.at(high, low).real if model.common_max else math.inf
        common = ((values[instance.plus] + values[instance.minus]) / 2 - reference).real
        if ((model.common_min is not None and not math.isfinite(lower))
                or (model.common_max is not None and not math.isfinite(upper))):
            raise CircuitError("Comparator common-mode bounds exceed the numeric range.",
                               instance.line, "NumericalNetwork", path=instance.path)
        tolerance = 1e-9 * max(1.0, abs(common), *(abs(v) for v in (lower, upper) if math.isfinite(v)))
        message = ""
        if lower > upper + tolerance:
            message = f"{instance.name}: common.min exceeds common.max at these supply voltages."
        elif common < lower - tolerance or common > upper + tolerance:
            message = (f"{instance.name}: input common-mode voltage {common:.8g} V is outside "
                       f"the declared range [{lower:.8g}, {upper:.8g}] V.")
        if message:
            raise CircuitError(message, instance.line, "CommonModeRange", path=instance.path)
