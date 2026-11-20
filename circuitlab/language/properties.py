"""Infer finite custom model properties without exposing host-language objects."""

import math
import re

from circuitlab.math.numbers import SCALES, real, scalar
from circuitlab.model.graph import CircuitError


PROPERTIES_BRACE = re.compile(r"properties\s*\{", re.I)
PROPERTY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")
PROPERTY_DECLARATION = re.compile(
    r"property\s+([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)"
    r"\s*(?::\s*([A-Za-zµμΩ°]+(?:/[A-Za-zµμ]+)?))?\s*=(?!=)\s*(.+)", re.I)
PROPERTY_UNITS = ("rad/s", "V/s", "ohm", "Hz", "deg", "dB", "Nm", "VA", "var",
                  "V", "A", "F", "H", "s", "W", "J", "C", "S")


def property_value(raw, unit=None):
    """Infer a base SI unit for defaults; validate overrides in that same unit."""
    text = raw.strip()
    if unit == "bool" or (unit is None and text.casefold() in {"true", "false"}):
        if text.casefold() not in {"true", "false", "0", "1"}:
            raise CircuitError("Use true/false or 0/1 for this Boolean property.")
        return text.casefold() in {"true", "1"}, "bool"
    slope = re.fullmatch(r"(.+?)\s*V\s*/\s*(ms|us|µs|μs|ns)", text)
    if slope and unit in {None, "V/s"}:
        seconds = {"ms": 1e-3, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ns": 1e-9}
        value = real(scalar(slope[1], "V")) / seconds[slope[2]]
        if not math.isfinite(value):
            raise CircuitError("Custom properties must be finite real numbers.")
        return value, "V/s"
    if unit is None:
        unit = ""
        suffix = re.search(r"[A-Za-zµμΩ°]+(?:/[A-Za-zµμ]+)?$", text)
        if suffix:
            spelling = re.sub(r"(?i)ohms?$", "ohm", suffix[0].replace("Ω", "ohm").replace("°", "deg"))
            for candidate in PROPERTY_UNITS:
                if spelling == candidate or (spelling.endswith(candidate) and spelling[:-len(candidate)] in SCALES):
                    unit = candidate
                    break
    # A bare engineering prefix describes magnitude, not physical dimensions.
    prefix = re.fullmatch(r"(.+?)\s*([pnuµμmkMG])", text) if not unit else None
    value = real(scalar(prefix[1])) * SCALES[prefix[2]] if prefix else real(scalar(text, unit))
    if not math.isfinite(value):
        raise CircuitError("Custom properties must be finite real numbers.")
    return value, unit


def validate_property_name(name):
    if (not PROPERTY_NAME.fullmatch(name) or any(part.startswith("__") for part in name.split("."))
            or name in {"V", "pi", "e", "t", "time", "true", "false"}):
        raise CircuitError("Use a property identifier or dotted name, excluding reserved math names.")


def declared_property_value(raw, annotation=None, *, unit=None, base_annotation=None):
    """Apply an optional declaration type without changing inferred-unit overrides."""
    def declared_unit(kind):
        if kind is None:
            return None
        if kind.casefold() in {"int", "float", "number"}:
            return ""
        if kind.casefold() == "bool":
            return "bool"
        if kind not in PROPERTY_UNITS:
            raise CircuitError("Use int, float, bool, or a base SI unit in a property declaration.")
        return kind
    explicit = declared_unit(annotation)
    if unit is not None and explicit is not None and explicit != unit:
        raise CircuitError("A property override cannot change the declared/inferred unit.")
    value, inferred = property_value(raw, unit if unit is not None else explicit)
    integer = (base_annotation or annotation or "").casefold() == "int"
    if integer:
        if not float(value).is_integer() or abs(value) > 2 ** 53 - 1:
            raise CircuitError("An int property requires an exactly representable integer.")
        value = int(value)
    return value, inferred
