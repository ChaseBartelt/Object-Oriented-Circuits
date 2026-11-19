"""Safe SI scalar parsing and engineering notation."""
# Parse electrical scalars through a restricted numeric syntax rather than evaluating source.
# Convert engineering prefixes at input/output boundaries; stored numbers use catalog units.
# Phasor helpers use degrees for angles and radians internally for complex exponentials.

import cmath
import math
import re

from circuitlab.model.graph import CircuitError

# Prefixes are case-sensitive: m is milli, M is mega. Accept ASCII u,
# the micro sign, and Greek mu as equivalent micro prefixes. The empty
# prefix leaves a value in the requested base unit.
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
# An unsigned decimal/scientific coefficient: 12, 12., .5, or 1.2e-3.
# A leading sign and any imaginary suffix belong to the surrounding
# complex grammar; the decimal point must be matched literally with \.
NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
# Accept a real number with an optional signed imaginary term, or a pure
# imaginary term. A missing imaginary coefficient means 1 (j, -i, 3+j).
# fullmatch() below requires the whole normalized number to fit this grammar.
COMPLEX = re.compile(
    rf"(?:[+-]?{NUMBER}(?:[+-](?:{NUMBER})?[ij])?|[+-]?(?:{NUMBER})?[ij])"
)


# Interpret prefixes and requested units at the boundary, then reject non-finite values
# before they can enter matrix arithmetic or equation propagation.
def scalar(text, unit=""):
    """Parse a finite real, rectangular, or polar value in the requested unit."""
    # Normalize the typographic minus sign before interpreting numeric signs.
    # No arithmetic expressions or source code are evaluated by this parser.
    text = text.replace("−", "-").strip()
    # Percent is a dimensionless real fraction: 50% becomes 0.5. Reuse scalar
    # validation for its numeric portion, then require a real result.
    if not unit and text.endswith("%"):
        return real(scalar(text[:-1])) / 100
    prefix = ""
    # Look for a final unit/prefix suffix after a numeric, imaginary, or angle
    # ending. A bare i/j can itself be part of the number, so it is excluded
    # from suffix removal below. An absent suffix means the requested unit
    # is implicit; for example, scalar("5", "V") already means 5 volts.
    match = re.search(
        r"(?:(?<=[0-9ij°])|(?<=deg))\s*([A-Za-zµμΩ°][A-Za-zµμΩ°/]*)$", text
    )
    if match and match[1] not in {"i", "j"}:
        raw = match[1]
        # For polar input, a final deg/degree symbol describes the angle inside
        # the number. Leave it in place for the angle parser instead of treating
        # it as the physical unit of the complete phasor.
        if raw not in {"°", "deg"} or not re.search(r"[<∠]", text):
            text = text[: match.start()].strip()
            # Normalize unit spelling before checking it: Ω/ohm/ohms become ohm,
            # and the degree symbol becomes deg. Preserve prefix case when doing so.
            suffix = re.sub(
                r"(?i)ohms?$", "ohm", raw.replace("Ω", "ohm").replace("°", "deg")
            )
            # An exact unit suffix needs no scaling. Otherwise try one recognized
            # prefix followed by the expected unit or nothing: both 2kΩ and 2k work
            # when ohm is requested. A suffix from another unit must still be rejected.
            if suffix != unit:
                prefix = suffix[0]
                # A prefix alone is meaningful only with an expected physical unit.
                # Degrees and rad/s are deliberately not prefixed here; callers must
                # supply those quantities in their specified angular units.
                if (
                    not unit
                    or unit in {"deg", "rad/s"}
                    or prefix not in SCALES
                    or suffix[1:] not in {"", unit}
                ):
                    raise CircuitError(
                        f"Expected unit {unit or 'dimensionless'}, received {raw}."
                    )
    # Either < or ∠ separates polar magnitude and phase. A trailing unit
    # suffix was handled first, so its prefix will scale the whole phasor.
    # For exactly two pieces, parse each as a real scalar; malformed extra
    # separators cannot pass the rectangular fullmatch branch instead.
    parts = re.split(r"[<∠]", text)
    if len(parts) == 2:
        magnitude = real(scalar(parts[0]))
        # Keep polar magnitude nonnegative rather than silently shifting its
        # phase by 180 degrees. The angle itself may be negative or wrap by turns.
        if magnitude < 0:
            raise CircuitError("Polar magnitude must be nonnegative.")
        # Polar angles use degrees, with an optional deg or degree-symbol suffix.
        # Strip that suffix before the recursive dimensionless numeric parse.
        angle = real(scalar(re.sub(r"\s*(?:deg|°)\s*$", "", parts[1])))
        value = phasor(magnitude, angle)
    else:
        # Allow whitespace around signs without permitting arbitrary embedded
        # whitespace. Rewrite engineering-style j2 into Python-style 2j, including
        # a trailing signed term such as 3-j2.
        text = re.sub(r"\s*([+-])\s*", r"\1", text)
        text = re.sub(r"(^|[+-])j([0-9.eE+-]+)$", r"\1\2j", text)
        if not COMPLEX.fullmatch(text):
            raise CircuitError(
                f"Invalid number {text!r}. Use a finite scalar, such as 1e-3 or 3-4j."
            )
        # Only convert after grammar validation. Python's complex constructor
        # then handles decimal/scientific conversion; i and j are equivalent here.
        value = complex(text.replace("i", "j"))
    # Apply the engineering multiplier once, after parsing either number form.
    # Check finiteness after scaling too: a syntactically valid exponent or
    # a large prefix can overflow the available floating-point range.
    value *= SCALES[prefix]
    if not cmath.isfinite(value):
        raise CircuitError("Every number must be finite and within the numeric range.")
    # Return a real scalar when the imaginary part is exactly zero. Do not
    # discard a small nonzero imaginary component using a tolerance.
    return value.real if value.imag == 0 else value


# Require a genuinely real numeric result for contexts such as angles and
# percentages. This helper rejects an imaginary component instead of
# silently projecting a complex value onto its real axis.
def real(value):
    if value.imag:
        raise CircuitError("Enter a real value.")
    return value.real


# Construct magnitude*exp(j*angle), with angle supplied in degrees.
# Modulo reduction puts negative and multi-turn angles on the same circle.
def phasor(magnitude, angle):
    angle %= 360
    # Return exact unit directions at cardinal angles instead of tiny cosine/
    # sine residuals. Other angles use the complex exponential in radians;
    # the dictionary's exact direction is selected when an angle matches.
    quadrants = {0: 1, 90: 1j, 180: -1, 270: -1j}
    return magnitude * quadrants.get(angle, cmath.exp(1j * math.radians(angle)))


# Map an angle to [-180, 180), so equivalent phases have a short wrapped
# difference. In particular, +180 is represented as -180.
def principal(angle):
    return (angle + 180) % 360 - 180


# Repeated givens are allowed only when numerically consistent; spelling a value twice
# must not silently change the circuit already described.
def set_known(known, key, value):
    """Accept a repeated assignment only if it agrees with the first."""
    if key in known:
        old = known[key]
        # Parsed real phase-prefixed quantities compare modulo 360 degrees.
        # For example, -90 and 270 agree; the angular tolerance is 1e-7 degrees.
        if key.startswith("phase") and not (old.imag or value.imag):
            conflict = abs(principal(old % 360 - value % 360)) > 1e-7
        # Other givens use relative tolerance based on the larger magnitude.
        # There is no fixed absolute tolerance here: a tiny nonzero given does
        # not automatically agree with zero.
        else:
            conflict = abs(old - value) > 1e-9 * max(abs(old), abs(value))
        if conflict:
            raise CircuitError(f"Conflicting entries for {key}.")
    # Store only the first given. A consistent repetition leaves its original
    # value intact rather than replacing it with a slightly different number.
    else:
        known[key] = value


# Derived-value comparisons can retain an external reference scale, such
# as |Z| when testing a nearly zero real part after cancellation. Combine
# relative tolerance with 32 ULPs (floating-point spacings near that scale)
# to allow arithmetic roundoff without a unit-independent absolute epsilon.
def close(a, b, scale=0):
    scale = max(abs(a), abs(b), abs(scale))
    return abs(a - b) <= 1e-9 * scale + 32 * math.ulp(scale)


def format_value(value, unit="", polar=False, engineering=True):
    """Format a scalar using engineering prefixes and optional phasor notation."""
    # Angle displays use degrees directly, with eight significant digits;
    # they do not acquire an engineering prefix.
    if unit == "deg":
        return f"{value:.8g}°"
    prefix = ""
    # Choose one prefix for each three-decade range using |value|, so signs
    # and phasor angles do not affect scaling. The nonzero guard avoids log(0);
    # dimensionless and rad/s displays retain their original numeric scale.
    if engineering and value and unit not in {"", "rad/s"}:
        # Index 4 is the base unit. floor(log10(|value|)/3) selects the decade
        # group; clamping limits prefixes to pico..giga. Values outside that range
        # still display using the nearest supported prefix, not a new prefix.
        prefix = ("p", "n", "u", "m", "", "k", "M", "G")[
            max(0, min(7, math.floor(math.log10(abs(value)) / 3) + 4))
        ]
    # Formatting reverses the parse multiplier: divide by the selected scale.
    # Render ASCII u as the micro sign and ohm as Ω for human-readable labels.
    scaled = value / SCALES[prefix]
    label = (prefix.replace("u", "µ") + unit.replace("ohm", "Ω")).strip()
    suffix = f" {label}" if label else ""
    # A nonzero phasor shows both magnitude/angle and rectangular components,
    # using the same scale and units for each form. Zero has no useful phase
    # and falls through to the ordinary zero display.
    if polar and scaled:
        phase = math.degrees(cmath.phase(scaled))
        return f"{abs(scaled):.8g} ∠ {phase:.8g}°{suffix} · {scaled.real:.8g} {'−' if scaled.imag < 0 else '+'} j{abs(scaled.imag):.8g}{suffix}"
    # This branch displays the real part only; polar=True above includes
    # the imaginary component too. "or 0" removes a real negative-zero label,
    # and .8g means eight significant digits, not eight decimal places.
    return f"{scaled.real or 0:.8g}{suffix}"
