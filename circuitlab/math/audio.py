"""Private electrical realization of the eight-terminal audio amplifier."""

from dataclasses import replace

import numpy as np

from circuitlab.model.graph import AudioAmpModel, Component, MathEquation


def audio_parts(part):
    """Build an ideal audio driver with an external gain and bypass network.

    Open gain pins give gain_default. Shunting the internal gain resistor
    raises gain toward gain_max; an external capacitor retains its actual
    frequency/time response. This is a behavioral model, not a silicon model:
    the driver has no clipping, output resistance, or load-dependent rail draw.
    """
    model = part.audio or AudioAmpModel()
    root = f"@audio:{part.name}"
    bias = root + ":bias"
    v = lambda p, n: ("v", p, n)
    mul = lambda value, tree: ("binary", "*", ("c", value), tree)
    # These tuples use the same restricted expression IR as user equations.
    bias_tree = mul(model.output_bias, v(part.vplus, part.vminus))
    differential = ("binary", "-", v(part.in_plus, part.in_minus), v(part.gain1, part.gain8))
    output_tree = ("binary", "+", v(part.bypass, part.vminus), mul(model.gain_max, differential))

    def source(name, p, n, tree, inputs):
        equation = MathEquation(name, p, n, tree, tuple(dict.fromkeys(inputs)))
        return Component(name, "B", p, n, behavior=equation)

    parts = [source(part.name, part.p, part.vminus, output_tree,
                    (part.bypass, part.vminus, part.in_plus, part.in_minus, part.gain1, part.gain8)),
             source(root + ":bias_source", bias, part.vminus, bias_tree, (part.vplus, part.vminus)),
             Component(root + ":bypass", "R", bias, part.bypass, model.bypass_resistance)]
    # Do not stamp redundant zero-volt branches when a gain pin is already
    # wired to return or both gain pins are externally shorted together.
    if part.gain8 != part.vminus:
        parts.append(Component(root + ":gain_return", "V", part.gain8, part.vminus, 0))
    if part.gain1 != part.gain8:
        gm = (model.gain_max / model.gain_default - 1) / model.gain_max / model.gain_resistor
        parts.extend((Component(root + ":gain", "R", part.gain1, part.gain8, model.gain_resistor),
                      Component(root + ":feedback", "G", part.gain8, part.gain1, gm,
                                in_plus=part.p, in_minus=part.bypass)))
    if model.supply_current:
        parts.append(Component(root + ":supply", "I", part.vplus, part.vminus,
                               model.supply_current, dc=model.supply_current, dc_only=True))
    return parts


def expand_audio(circuit):
    """Keep private parts and nodes out of the source graph and drawing."""
    if not any(part.kind == "A" for part in circuit.components):
        return circuit
    parts = []
    for part in circuit.components:
        expanded = audio_parts(part) if part.kind == "A" else (part,)
        if circuit.mode != "dc" and part.kind == "A":
            # Quiescent current is a DC offset, with zero AC amplitude.
            expanded = [replace(p, value=0) if p.kind == "I" else p for p in expanded]
        parts.extend(expanded)
    return replace(circuit, components=parts)


def fold_audio(network, original):
    """Restore public part/node order, power, and eight terminal currents."""
    if not any(part.kind == "A" for part in original.components):
        return network
    indices = {name: i for i, name in enumerate(network.names)}
    node_indices = {net: i for i, net in enumerate(network.nodes)}
    voltages, currents, powers = [], [], []
    terminal_current = {name: values for name, values in network.terminal_current.items()
                        if not name.startswith("@audio:")}
    for part in original.components:
        index = indices[part.name]
        voltages.append(network.branch_voltage[index])
        currents.append(network.branch_current[index])
        if part.kind != "A":
            powers.append(network.branch_power[index])
            continue
        root = f"@audio:{part.name}"
        members = [i for name, i in indices.items() if name == part.name or name.startswith(root + ":")]
        powers.append(sum(network.branch_power[i] for i in members))
        current = lambda suffix: network.branch_current[indices[root + suffix]] if root + suffix in indices else 0
        gain = current(":gain") - current(":feedback")
        gain_return, bypass, supply = current(":gain_return"), -current(":bypass"), current(":supply")
        out = network.branch_current[index]
        terminal_current[part.name] = (0, 0, out, supply,
                                      -out - gain_return - bypass - supply,
                                      gain, -gain + gain_return, bypass)
    public_nodes = original.nodes
    return replace(network, nodes=public_nodes, names=tuple(p.name for p in original.components),
                   voltage=network.voltage[[node_indices[n] for n in public_nodes]],
                   branch_voltage=np.asarray(voltages), branch_current=np.asarray(currents),
                   branch_power=np.asarray(powers), terminal_current=terminal_current,
                   references={net: ref for net, ref in network.references.items() if net in public_nodes})
