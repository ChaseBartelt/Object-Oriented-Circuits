"""Actionable connection diagnostics shared by expansion and graph building."""

import re

from circuitlab.math.numbers import scalar
from circuitlab.model.diode import DIODE_NAME
from circuitlab.model.graph import CircuitError


COMPONENT_NAME = re.compile(rf"(?:{DIODE_NAME}|[RCLXZVIPS][A-Za-z0-9_]*)", re.I)


def missing_connections(name, raw):
    if name[0].upper() == "X":
        return (f'Reactance "{name}" needs a series or parallel block: '
                f'series(wire1, wire2) {{ {name} = {raw}; }}. '
                "For an explicit reactance connection, use complex Z, such as "
                "Z1(j10) between(wire1, wire2), in AC mode.")
    wires = "high, wiper, low" if name[0].upper() == "P" else "wire1, wire2"
    message = (f'Component "{name}" has a value but no wire connections. '
               f'Use {name}({raw}) between({wires})')
    if name[0].upper() != "P":
        message += f", or put {name} = {raw} inside a series or parallel block"
    return message + "."


def component_call(name, raw):
    """Recognize a value call without mistaking an unknown chip's wire arguments."""
    if not COMPONENT_NAME.fullmatch(name):
        return False
    if not raw.strip() or any(character.isdigit() for character in name):
        return True
    if name[0].upper() == "S" and raw.strip().casefold() in {"open", "closed", "on", "off"}:
        return True
    try:
        scalar(raw, {"R": "ohm", "C": "F", "L": "H", "X": "ohm", "Z": "ohm",
                     "V": "V", "I": "A", "P": "ohm"}.get(name[0].upper(), ""))
    except CircuitError:
        return False
    return True


def contextual_component_error(name, error):
    if f'"{name}"' in error.message or error.message.startswith(f"{name}:"):
        return error
    return CircuitError(f'Component "{name}": {error.message}', error.line, error.code, path=error.path)
