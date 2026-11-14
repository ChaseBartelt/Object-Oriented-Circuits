"""Shared .ckt grammar, source rows, and automatic analysis mode detection."""

import re
from dataclasses import dataclass, field

from .graph import CircuitError
from .numbers import NUMBER

FREQUENCY = {"f", "T", "w"}
AC_VOLTAGE = {"V", "Vmag", "Vpk", "Vpp"}
UNITS = dict(R="ohm", C="F", L="H", X="ohm", V="V", I="A")
SECTION = re.compile(
    r"(series|parallel|circuit|gate|chip)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(([^()]*)\)\s*)?\{",
    re.I,
)
DEFAULT_TERMINALS = ("a", "b")
NET_METHODS = {"circuit", "gate", "chip"}
OVERRIDE = re.compile(r"@override", re.I)
INLINE = re.compile(r"(series|parallel)\s*(?:\(([^()]*)\)\s*)?\{", re.I)
BATTERY_BRACE = re.compile(r"battery\s+([A-Za-z][A-Za-z0-9_]*)\s*\{", re.I)
TRANSISTOR = re.compile(r"transistor\s+([A-Za-z][A-Za-z0-9_]*)\s*\{", re.I)
TRANS_CALL = re.compile(r"trans([A-Za-z0-9_]+)", re.I)
TRANS_POSITIONAL = re.compile(r"transistor\s+([A-Za-z][A-Za-z0-9_]*)\s*\(([^()]*)\)", re.I)
CHIP = re.compile(r"chip\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)\s*\{", re.I)
CHIP_START = re.compile(
    r"chip[ \t]+(?:[^ \t({]+[ \t]*(?=[({])|[A-Za-z_][A-Za-z0-9_]*$)", re.I
)
BJT_PINS = ("base", "collector", "emitter")
BJT_TYPES = {"npn", "pnp"}
BJT_MODEL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
BATTERY_MARKER = re.compile(r"\(\s*battery(?:\s+([^()=]+))?\s*\)", re.I)
BATTERY_ALIAS = re.compile(r"^\s*(Vrms|rad\s*/\s*s)\s*=", re.I)
MAIN = re.compile(r"main\s*(?:\(\s*\))?\s*\{", re.I)
CALL = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*\(((?:[^()]|\([^()]*\))*)\)")
CONNECTOR = re.compile(r"connector\s*\(([^()]*)\)\s*;?", re.I)
INCLUDE = re.compile(r'include\s*\(\s*"([^"\r\n]+)"\s*\)\s*;?', re.I)
OP = re.compile(r"op[0-9]+", re.I)
MARKER = re.compile(
    r"(\(\s*(?:end\s+)?(?:parallel|series|battery(?:\s+[^()=]+)?|opamp(?:\s+[A-Za-z][A-Za-z0-9_]*)?)\s*\))",
    re.I,
)
WIRE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. \t-]*")
ASSIGNMENT = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*)")
PIN_DIRECTION = re.compile(r"[\w, \t]+\s*=\s*(input|output)", re.I)
PARAMETER_TARGET = re.compile(r"[RCLXVI][A-Za-z0-9_]*", re.I)
PART_ARGUMENT = re.compile(r"([RCL][A-Za-z0-9_]*)\s*\(([^()]+)\)", re.I)
PART_PLACEMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+between\s*\(([^()]*)\)", re.I)
PLACEMENT = re.compile(
    r"([A-Za-z][A-Za-z0-9_]*)\s*\(([^()]+)\)\s+between\s*\(([^()]*)\)", re.I
)
SOURCE_WIRE = re.compile(
    r"([VI][A-Za-z0-9_]*)\s*=\s*([^:;]+)\s*:\s*wire\s*=\s*([^;]*)\s*;?", re.I
)
WAVE = re.compile(
    rf"([+-]?{NUMBER})\s*\*?\s*cos\s*\(\s*({NUMBER})\s*\*?\s*t\s*"
    rf"((?:[+-]\s*{NUMBER}\s*(?:°|deg)?)?)\s*\)\s*"
    r"([A-Za-zµμ]+|\[\s*[A-Za-zµμ]+\s*\])?"
)
AC_SOURCE_SUFFIX = re.compile(r"(?:\d\s*|\s)[VA](?:rms|pk|pp)\s*$", re.I)


@dataclass(frozen=True)
class Binding:
    """Map method parameters to caller nets and qualify local names per call."""

    scope: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    in_main: bool = False
    values: dict[str, str] = field(default_factory=dict)
    instance: int | None = None

    def wire(self, wire):
        if not self.scope or wire == "0":
            return wire
        return self.mapping.get(wire, f"{self.scope}.{wire}")

    def part(self, name):
        return f"{name}_{self.scope}" if self.scope else name


SourceRow = tuple[str, int, Binding]


def analysis_mode(source):
    """Infer the source mode from explicit units, frequency, and DC/AC pairs."""
    source = re.sub(r"#.*", "", source)
    # Transistor fields name nets and models, even if they end in Vpk or Arms.
    source = re.sub(TRANSISTOR.pattern + r"[^}]*}", "", source, flags=re.I)
    rows = re.split(r"[;{}\r\n]+", source)
    if any("|" in row for row in rows):
        return "mixed"
    for row in rows:
        if (
            re.search(r"^\s*(?:f|T|w|Vpk|Vpp|phaseV|phaseI|Vdc)\s*=", row)
            or BATTERY_ALIAS.match(row)
            or AC_SOURCE_SUFFIX.search(row)
        ):
            return "ac"
        placement = PLACEMENT.fullmatch(row.strip()) or SOURCE_WIRE.fullmatch(
            row.strip()
        )
        if (
            placement
            and placement[1][0].upper() in "VI"
            and AC_SOURCE_SUFFIX.search(placement[2])
        ):
            return "ac"
    return "dc"


def has_chip(source):
    """Recognize chip files with input/output declarations for Verilog export."""
    in_chip = False
    for row, _, _ in source_rows(source):
        if CHIP.fullmatch(row):
            in_chip = True
        elif row == "}":
            in_chip = False
        elif in_chip and PIN_DIRECTION.fullmatch(row):
            return True
        elif not in_chip and CHIP_START.match(row) and not SECTION.fullmatch(row):
            return True
    return False


def source_rows(source, *, legacy_markers=True):
    """Split statements while retaining the original line for diagnostics."""
    depth = 0
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if depth or any(
            header.match(line)
            for header in (SECTION, MAIN, INLINE, BATTERY_BRACE, TRANSISTOR, CHIP)
        ):
            depth = max(0, depth + line.count("{") - line.count("}"))
            line = line.replace(";", "\n").replace("{", "{\n").replace("}", "\n}\n")
        # Legacy parenthesized blocks allow several assignments on the same line.
        if legacy_markers and MARKER.search(line):
            line = MARKER.sub(r"\n\1\n", line)
            line = re.sub(
                r"[ \t]+((?:[RCLXVI][A-Za-z0-9_]*|measure|wire|ground|f|T|w|phase[VI]|rad\s*/\s*s)[ \t]*=)",
                r"\n\1",
                line,
                flags=re.I,
            )
        for item in line.splitlines():
            if item.strip():
                yield item.strip(), number, Binding()


def parse_wires(raw, count=None):
    """Normalize and validate a comma-separated list of node names."""
    if count == 0 and not raw.strip():
        return ()
    values = tuple(re.sub(r"[ \t]+", " ", value.strip()) for value in raw.split(","))
    if (count is not None and len(values) != count) or not all(
        WIRE.fullmatch(v) for v in values
    ):
        raise CircuitError(f"Use {count or 'valid'} wire names.")
    return values
