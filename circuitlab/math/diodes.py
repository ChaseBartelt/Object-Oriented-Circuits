"""Smooth generic diode I/V laws, analytic slopes and depletion charge."""

import math
from circuitlab.model.diode import THERMAL_VOLTAGE


def limited_exponential(value):
    # Linear continuation avoids overflow while keeping current and slope consistent.
    limit = 40.0
    if value <= limit:
        result = math.exp(value)
        return result, result
    base = math.exp(limit)
    return base * (1 + value - limit), base


def diode_current(model, voltage):
    scale = THERMAL_VOLTAGE * model.ideality
    forward, slope = limited_exponential(math.log(model.saturation) + voltage / scale)
    reverse, reverse_slope = limited_exponential(math.log(model.breakdown_current) + (-voltage - model.breakdown) / scale)
    current = forward - model.saturation - reverse - model.photocurrent
    gradient = (slope + reverse_slope) / scale
    if model.kind == "tunnel":
        x = voltage / model.peak_voltage
        if x <= 0:
            tunnel_slope = math.e * model.peak_current / model.peak_voltage
            current += tunnel_slope * voltage
            gradient += tunnel_slope
        else:
            decay = math.exp(1 - x)
            current += model.peak_current * x * decay
            gradient += model.peak_current / model.peak_voltage * (1 - x) * decay
    return current, gradient


def diode_charge(model, voltage):
    # Reverse junction capacitance falls with bias. Forward bias uses the finite
    # zero-bias capacitance; this generic model has no reverse-recovery storage.
    if voltage >= 0:
        return model.capacitance * voltage, model.capacitance
    base = 1 - voltage / model.potential
    charge = model.capacitance * model.potential / (1 - model.grading) * (1 - base ** (1 - model.grading))
    return charge, model.capacitance * base ** (-model.grading)
