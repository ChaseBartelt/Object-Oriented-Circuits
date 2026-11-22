"""Parse optional diode forward/breakdown values and explicit model parameters."""

from dataclasses import replace
import math

from circuitlab.math.numbers import scalar, real
from circuitlab.model.graph import CircuitError
from circuitlab.model.diode import THERMAL_VOLTAGE, diode_preset

PARAMETERS = {
    "is": ("saturation", "A"), "n": ("ideality", ""), "rs": ("resistance", "ohm"),
    "bv": ("breakdown", "V"), "ibv": ("breakdown_current", "A"),
    "cj": ("capacitance", "F"), "cjo": ("capacitance", "F"),
    "vj": ("potential", "V"), "m": ("grading", ""), "light": ("photocurrent", "A"),
    "ip": ("peak_current", "A"), "vp": ("peak_voltage", "V"),
}


def parse_diode(name, raw):
    model, fields, forward = diode_preset(name), {}, None
    for index, item in enumerate(raw.split(",") if raw.strip() else ()):
        item = item.strip()
        if "=" not in item:
            if index or not item:
                raise CircuitError("Use a diode voltage followed by named parameters, such as D.Zener1(5.1V, rs=2ohm).")
            value = real(scalar(item, "V"))
            if not math.isfinite(value) or value <= 0:
                raise CircuitError("Use a positive diode voltage.")
            if model.kind in {"zener", "avalanche"}:
                fields["breakdown"] = value
            else:
                forward = value
            continue
        key, value = (part.strip() for part in item.split("=", 1))
        if key.casefold() not in PARAMETERS:
            raise CircuitError(f'Unknown diode parameter "{key}". Use ' + ", ".join(PARAMETERS) + ".")
        field, unit = PARAMETERS[key.casefold()]
        if field in fields:
            raise CircuitError(f'Diode parameter "{key}" is already set.')
        fields[field] = real(scalar(value, unit))
    model = replace(model, **fields)
    if forward is not None:
        if "saturation" in fields:
            raise CircuitError("Specify a forward voltage or is, rather than both.")
        # A bare forward voltage is specified at a documented 1 mA reference.
        junction = forward - 0.001 * model.resistance
        exponent = junction / (model.ideality * THERMAL_VOLTAGE)
        if not 0 < exponent < 700:
            raise CircuitError("Diode forward voltage is incompatible with rs and n.")
        model = replace(model, saturation=0.001 / math.expm1(exponent))
    return model
