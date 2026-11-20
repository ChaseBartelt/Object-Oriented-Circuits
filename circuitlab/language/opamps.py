"""Validate optional op-amp declaration fields in base SI units."""

import re
from dataclasses import dataclass, field

from circuitlab.math.numbers import real, scalar
from circuitlab.model.graph import AudioAmpModel, CircuitError, ComparatorModel, OpAmpModel, VoltageLimit


# Restrict dotted names to model fields. Ordinary assignments keep their
# existing grammar, so node names and digital expressions are unaffected.
MODEL_ASSIGNMENT = re.compile(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)\s*=\s*(.*)")


@dataclass
class ModelSetting:
    targets: tuple[str, ...]
    fields: dict
    origins: dict
    override: bool
    external: bool
    line: int
    path: object = None
    owner: int | None = None
    locals: tuple[str, ...] = ()
    raws: dict = field(default_factory=dict)


def parameter_name(name):
    name = name.casefold().replace(".", "_")
    return "bandwidth" if name == "gbw" else name


def opamp_parameter(name, raw):
    """Parse one explicit override; missing fields use OpAmpModel defaults."""
    units = {"gain": "", "bandwidth": "Hz", "slew": "V/s",
             "offset": "V", "supply_current": "A"}
    if name not in units:
        raise CircuitError(f'Unknown op-amp parameter "{name.replace("_", ".")}". Use ' +
                           ", ".join(key.replace("_", ".") for key in units) + ".")
    if name in {"gain", "bandwidth", "slew"} and raw.strip().casefold() == "ideal":
        return None
    # The SI scalar parser scales a unit prefix, not the denominator of V/us.
    slope = re.fullmatch(r"(.+?)\s*V\s*/\s*(ms|us|µs|μs|ns)", raw.strip(), re.I)
    if name == "slew" and slope:
        seconds = {"ms": 1e-3, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ns": 1e-9}
        value = real(scalar(slope[1], "V")) / seconds[slope[2].casefold()]
        # Revalidate the scaled value to reject overflow as a CircuitError.
        value = real(scalar(str(value), "V/s"))
    else:
        value = real(scalar(raw, units[name]))
    if name in {"gain", "bandwidth", "slew"} and value <= 0:
        raise CircuitError(f"Op-amp {name} must be positive or ideal.")
    if name == "supply_current" and value < 0:
        raise CircuitError("Op-amp supply.current must be nonnegative.")
    return value


def opamp_model(fields):
    return OpAmpModel(**fields)


def model_parameter(name, raw):
    if name in {"delay_rise", "delay_fall", "response_time", "output_low_at4ma"}:
        value = real(scalar(raw, "V" if name == "output_low_at4ma" else "s"))
        if value < 0 or (name == "output_low_at4ma" and value == 0):
            raise CircuitError(f"Invalid comparator {name.replace('_', '.')}.")
        return value
    if name in {"common_min", "common_max", "output_min", "output_max"}:
        return voltage_limit(raw)
    if name in {"common_rejection", "common_impedance"}:
        if raw.strip().casefold() == "ideal":
            return None
        value = real(scalar(raw, "dB" if name == "common_rejection" else "ohm"))
        if (name == "common_rejection" and value < 0) or (name == "common_impedance" and value <= 0):
            raise CircuitError(f"Invalid {name.replace('_', '.')}.")
        return value
    if name in {"gain_default", "gain_max", "gain_resistor", "output_bias", "bypass_resistance"}:
        unit = "ohm" if name.endswith("resistor") or name.endswith("resistance") else ""
        value = real(scalar(raw, unit))
        if (name != "output_bias" and value <= 0) or (name == "output_bias" and not 0 <= value <= 1):
            raise CircuitError(f"Invalid audio amplifier {name.replace('_', '.')}.")
        return value
    if name == "output_type":
        if raw.strip().casefold() not in {"power_amplifier", "open_collector"}:
            raise CircuitError("output.type must be power_amplifier or open_collector.")
        return raw.strip().casefold()
    return opamp_parameter(name, raw)


def voltage_limit(raw):
    """Reuse the safe math grammar, limited to affine supply expressions."""
    if raw.strip().casefold() == "ideal":
        return None
    from circuitlab.language.math_blocks import UNIT_LITERAL, compile_equation
    from circuitlab.language.syntax import Binding
    from circuitlab.math.behavioral import linear_form

    expression = re.sub(r"\b(vplus|vminus)\b", lambda m: f"V(__rail_{m[0].lower()})", raw, flags=re.I)
    try:
        if any(m[2] not in {"p", "n", "u", "µ", "μ", "m", "k", "M", "G"}
               and not m[2].endswith("V") for m in UNIT_LITERAL.finditer(expression)):
            raise CircuitError("Voltage units required.")
        equation = compile_equation("V(__limit)=" + expression, False, Binding(), None, "limit")
        affine = linear_form(equation.tree)
        if affine is None:
            raise CircuitError("Non-affine voltage bound.")
        constant, coefficients = affine
        if coefficients.keys() - {"0", "__rail_vplus", "__rail_vminus"}:
            raise CircuitError("Unknown voltage-bound reference.")
        return VoltageLimit(constant, coefficients.get("__rail_vplus", 0), coefficients.get("__rail_vminus", 0))
    except CircuitError:
        raise CircuitError("Use a voltage or an affine rail expression, e.g. vminus + 3V or vplus, for common/output.min/max.") from None


def needs_supplies(model):
    return model.supply_current or any(limit and (limit.vplus or limit.vminus)
                                       for limit in (model.common_min, model.common_max, model.output_min, model.output_max))


def resolve_model(settings, name, audio=False, comparator=False):
    """Resolve specificity, rejecting same-priority conflicts independent of order."""
    model_class = ComparatorModel if comparator else AudioAmpModel if audio else OpAmpModel
    allowed = set(model_class.__dataclass_fields__)
    candidates = {}
    for setting in settings:
        if name.casefold() not in setting.targets:
            continue
        priority = ((1, -len(setting.targets), int(setting.external))
                    if setting.override or setting.external else (0, 0, 0))
        for key, value in setting.fields.items():
            target = "gain_default" if audio and key == "gain" else key
            line, path = setting.origins.get(key, (setting.line, setting.path))
            if target not in allowed:
                kind = "a comparator" if comparator else "an eight-pin audio amplifier" if audio else "opamp"
                raise CircuitError(f'Parameter "{key.replace("_", ".")}" is not supported by {kind}.', line, path=path)
            values = candidates.setdefault(target, {})
            if priority in values and values[priority] != value:
                raise CircuitError(f'Conflicting model overrides for {name}.{key.replace("_", ".")}.', line, path=path)
            values[priority] = value
    fields = {key: choices[max(choices)] for key, choices in candidates.items()}
    return model_class(**fields)
