"""Safe SI scalar parsing and engineering notation."""

import cmath
import math
import re

from circuitlab.model.graph import CircuitError

SCALES = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
}
NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
COMPLEX = re.compile(
    rf"(?:[+-]?{NUMBER}(?:[+-](?:{NUMBER})?[ij])?|[+-]?(?:{NUMBER})?[ij])"
)


def scalar(text, unit=""):
    """Parse a finite real, rectangular, or polar value in the requested unit."""
    text = text.replace("−", "-").strip()
    if not unit and text.endswith("%"):
        return real(scalar(text[:-1])) / 100
    prefix = ""
    match = re.search(
        r"(?:(?<=[0-9ij°])|(?<=deg))\s*([A-Za-zµμΩ°][A-Za-zµμΩ°/]*)$", text
    )
    if match and match[1] not in {"i", "j"}:
        raw = match[1]
        if raw not in {"°", "deg"} or not re.search(r"[<∠]", text):
            text = text[: match.start()].strip()
            suffix = re.sub(
                r"(?i)ohms?$", "ohm", raw.replace("Ω", "ohm").replace("°", "deg")
            )
            if suffix != unit:
                prefix = suffix[0]
                if (
                    not unit
                    or unit in {"deg", "rad/s"}
                    or prefix not in SCALES
                    or suffix[1:] not in {"", unit}
                ):
                    raise CircuitError(
                        f"Expected unit {unit or 'dimensionless'}, received {raw}."
                    )
    parts = re.split(r"[<∠]", text)
    if len(parts) == 2:
        magnitude = real(scalar(parts[0]))
        if magnitude < 0:
            raise CircuitError("Polar magnitude must be nonnegative.")
        angle = real(scalar(re.sub(r"\s*(?:deg|°)\s*$", "", parts[1])))
        value = phasor(magnitude, angle)
    else:
        text = re.sub(r"\s*([+-])\s*", r"\1", text)
        text = re.sub(r"(^|[+-])j([0-9.eE+-]+)$", r"\1\2j", text)
        if not COMPLEX.fullmatch(text):
            raise CircuitError(
                f"Invalid number {text!r}. Use a finite scalar, such as 1e-3 or 3-4j."
            )
        value = complex(text.replace("i", "j"))
    value *= SCALES[prefix]
    if not cmath.isfinite(value):
        raise CircuitError("Every number must be finite and within the numeric range.")
    return value.real if value.imag == 0 else value


def real(value):
    if value.imag:
        raise CircuitError("Enter a real value.")
    return value.real


def phasor(magnitude, angle):
    angle %= 360
    quadrants = {0: 1, 90: 1j, 180: -1, 270: -1j}
    return magnitude * quadrants.get(angle, cmath.exp(1j * math.radians(angle)))


def principal(angle):
    return (angle + 180) % 360 - 180


def set_known(known, key, value):
    """Accept a repeated assignment only if it agrees with the first."""
    if key in known:
        old = known[key]
        if key.startswith("phase") and not (old.imag or value.imag):
            conflict = abs(principal(old % 360 - value % 360)) > 1e-7
        else:
            conflict = abs(old - value) > 1e-9 * max(abs(old), abs(value))
        if conflict:
            raise CircuitError(f"Conflicting entries for {key}.")
    else:
        known[key] = value


def close(a, b, scale=0):
    scale = max(abs(a), abs(b), abs(scale))
    return abs(a - b) <= 1e-9 * scale + 32 * math.ulp(scale)


def format_value(value, unit="", polar=False, engineering=True):
    """Format a scalar using engineering prefixes and optional phasor notation."""
    if unit == "deg":
        return f"{value:.8g}°"
    prefix = ""
    if engineering and value and unit not in {"", "rad/s"}:
        prefix = ("p", "n", "u", "m", "", "k", "M", "G")[
            max(0, min(7, math.floor(math.log10(abs(value)) / 3) + 4))
        ]
    scaled = value / SCALES[prefix]
    label = (prefix.replace("u", "µ") + unit.replace("ohm", "Ω")).strip()
    suffix = f" {label}" if label else ""
    if polar and scaled:
        phase = math.degrees(cmath.phase(scaled))
        return f"{abs(scaled):.8g} ∠ {phase:.8g}°{suffix} · {scaled.real:.8g} {'−' if scaled.imag < 0 else '+'} j{abs(scaled.imag):.8g}{suffix}"
    return f"{scaled.real or 0:.8g}{suffix}"
