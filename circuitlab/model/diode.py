"""Generic room-temperature diode presets shared by simulation and export."""

from dataclasses import dataclass
import math

from circuitlab.model.graph import CircuitError

THERMAL_VOLTAGE = 0.02585
DIODE_NAME = r"D(?:[A-Za-z0-9_]*|\.[A-Za-z][A-Za-z0-9_]*)"
ALIASES = {
    "rect": "rectifier", "rectifier": "rectifier", "silicon": "rectifier",
    "tun": "tunnel", "tunnel": "tunnel", "esaki": "tunnel",
    "schot": "schottky", "schott": "schottky", "schottky": "schottky",
    "zener": "zener", "led": "led", "pin": "pin",
    "var": "varactor", "varactor": "varactor", "varicap": "varactor",
    "photo": "photo", "photodiode": "photo", "avalanche": "avalanche", "tvs": "avalanche",
}


@dataclass(frozen=True)
class DiodeModel:
    kind: str = "rectifier"
    saturation: float = 1e-9
    ideality: float = 2.0
    resistance: float = 1.0
    breakdown: float = 100.0
    breakdown_current: float = 1e-3
    capacitance: float = 10e-12
    potential: float = 0.7
    grading: float = 0.5
    photocurrent: float = 0.0
    peak_current: float = 5e-3
    peak_voltage: float = 0.07

    def __post_init__(self):
        if self.kind not in set(ALIASES.values()):
            raise CircuitError("Unknown diode type.")
        for key, value in vars(self).items():
            if key == "kind":
                continue
            positive = key not in {"resistance", "capacitance", "photocurrent", "grading"}
            if not math.isfinite(value) or (value <= 0 if positive else value < 0):
                raise CircuitError(f"Invalid diode {key}: use a finite {'positive' if positive else 'nonnegative'} value.")
        if self.grading >= 1:
            raise CircuitError("Diode grading m must be less than 1.")


def diode_preset(name):
    """Typed names carry an optional numeric instance suffix: D.Schot1."""
    import re
    kind = "rectifier"
    if "." in name:
        tag = re.sub(r"[0-9_]+$", "", name.split(".", 1)[1]).casefold()
        if tag not in ALIASES:
            raise CircuitError("Unknown diode type. Use D.rect, D.Tun, D.Schot, D.Zener, D.LED, D.PIN, D.Varactor, D.Photo or D.Avalanche.")
        kind = ALIASES[tag]
    values = {
        "schottky": dict(saturation=1e-7, ideality=1.05, resistance=0.2, breakdown=40, capacitance=50e-12),
        "zener": dict(ideality=1.7, breakdown=5.1, capacitance=50e-12),
        "led": dict(saturation=1e-20, resistance=5, breakdown=5, capacitance=20e-12),
        "tunnel": dict(saturation=1e-9, ideality=1.5, resistance=0.5, breakdown=10, capacitance=5e-12),
        "pin": dict(capacitance=0.5e-12),
        "varactor": dict(saturation=1e-12, capacitance=100e-12, grading=0.5, breakdown=30),
        "photo": dict(saturation=1e-12, photocurrent=10e-6, capacitance=5e-12),
        "avalanche": dict(breakdown=12, capacitance=100e-12),
    }.get(kind, {})
    return DiodeModel(kind=kind, **values)
