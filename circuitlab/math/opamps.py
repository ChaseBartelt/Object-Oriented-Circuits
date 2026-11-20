"""Common-mode input loading, rejection, and operating-range validation."""

from dataclasses import replace
import math

from circuitlab.model.graph import CircuitError


def common_inverse(model):
    """Convert voltage CMRR in dB to an input-referred error coefficient."""
    return 0.0 if model is None or model.common_rejection is None else 10 ** (-model.common_rejection / 20)


def has_output_limits(model):
    return model is not None and (model.output_min is not None or model.output_max is not None)


def validate_output_limits(circuit, values):
    """Check the declared output interval at an accepted DC/time endpoint."""
    for part in circuit.components:
        model = part.opamp if part.kind == "O" else None
        if not has_output_limits(model):
            continue
        reference = values[part.n]
        limits = (model.output_min, model.output_max)
        high = values[part.vplus] - reference if any(v and v.vplus for v in limits) else 0.0
        low = values[part.vminus] - reference if any(v and v.vminus for v in limits) else 0.0
        lower = model.output_min.at(high, low).real if model.output_min else -math.inf
        upper = model.output_max.at(high, low).real if model.output_max else math.inf
        if ((model.output_min is not None and not math.isfinite(lower))
                or (model.output_max is not None and not math.isfinite(upper))):
            raise CircuitError("Output bounds exceed the numeric range.",
                               circuit.component_lines.get(part.name), "NumericalNetwork",
                               path=circuit.component_files.get(part.name))
        tolerance = 1e-9 * max(1, *(abs(v) for v in (lower, upper) if math.isfinite(v)))
        if lower > upper + tolerance:
            raise CircuitError(f"{part.label or part.name}: output.min exceeds output.max at these supply voltages.",
                               circuit.component_lines.get(part.name), "OutputLimitRange",
                               path=circuit.component_files.get(part.name))


def common_inputs(part):
    """Input impedance is per input, to the device's output return."""
    model = part.opamp
    if part.kind == "O" and model and model.common_impedance is not None:
        return ((part.in_plus, part.n, model.common_impedance),
                (part.in_minus, part.n, model.common_impedance))
    return ()


def common_results(network, parts, *, ac=False):
    """Fold input shunt powers/currents into the original amplifier result."""
    loaded = [part for part in parts if common_inputs(part)]
    if not loaded:
        return network
    indices = {name: i for i, name in enumerate(network.names)}
    values = dict(zip(network.nodes, network.voltage))
    powers, terminals = network.branch_power.copy(), dict(network.terminal_current)
    for part in loaded:
        i = indices[part.name]
        inputs = [(values[p] - values[n]) / resistance for p, n, resistance in common_inputs(part)]
        powers[i] += sum((values[p] - values[n]) * amps.conjugate()
                         for (p, n, _), amps in zip(common_inputs(part), inputs))
        out = network.branch_current[i]
        iq = 0 if ac else part.opamp.supply_current
        terminals[part.name] = (*inputs, out, -out - sum(inputs),
                               *((iq, -iq) if part.vplus else ()))
    return replace(network, branch_power=powers, terminal_current=terminals)


def validate_common_ranges(circuit, values):
    """Reject operation outside a declared range; do not invent clipping physics.

    Values are DC voltages or one accepted instantaneous transient endpoint.
    AC phasors are not common-mode bias voltages and must not be checked here.
    """
    for part in circuit.components:
        model = part.opamp if part.kind == "O" else None
        if model is None or (model.common_min is None and model.common_max is None):
            continue
        reference = values[part.n]
        limits = (model.common_min, model.common_max)
        high = values[part.vplus] - reference if any(v and v.vplus for v in limits) else 0.0
        low = values[part.vminus] - reference if any(v and v.vminus for v in limits) else 0.0
        lower = model.common_min.at(high, low) if model.common_min else -math.inf
        upper = model.common_max.at(high, low) if model.common_max else math.inf
        common = (values[part.in_plus] + values[part.in_minus]) / 2 - reference
        lower, upper, common = lower.real, upper.real, common.real
        if ((model.common_min is not None and not math.isfinite(lower))
                or (model.common_max is not None and not math.isfinite(upper))):
            raise CircuitError("Common-mode bounds exceed the numeric range.",
                               circuit.component_lines.get(part.name), "NumericalNetwork",
                               path=circuit.component_files.get(part.name))
        tolerance = 1e-9 * max(1, abs(common), *(abs(v) for v in (lower, upper) if math.isfinite(v)))
        message = ""
        if lower > upper + tolerance:
            message = f"{part.label or part.name}: common.min exceeds common.max at these supply voltages."
        elif common < lower - tolerance or common > upper + tolerance:
            message = (f"{part.label or part.name}: input common-mode voltage {common:.8g} V is outside "
                       f"the declared range [{lower:.8g}, {upper:.8g}] V.")
        if message:
            raise CircuitError(message, circuit.component_lines.get(part.name), "CommonModeRange",
                               path=circuit.component_files.get(part.name))
