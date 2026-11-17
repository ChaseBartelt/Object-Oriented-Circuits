"""Expand numbered digital pins while keeping vector-width syntax distinct."""

import re

from circuitlab.model.graph import CircuitError


MAX_PIN_INDEX = 4095
MAX_BANK_PINS = 4096
DECIMAL = r"[0-9]+(?:_[0-9]+)*"
BANK = re.compile(rf"([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*({DECIMAL})\s*:\s*({DECIMAL})\s*\]")


def expand_pin_banks(raw, line):
    """Expand SW[3:0] to SW3, SW2, SW1, SW0; leave data[4] unchanged."""
    pins = []
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            pins.append(item)
        else:
            match = BANK.fullmatch(item)
            if not match:
                raise CircuitError("Use name[high:low] with nonnegative decimal pin indices.", line)
            bounds = [bound.replace("_", "").lstrip("0") or "0" for bound in match.groups()[1:]]
            if any(len(bound) > 4 for bound in bounds):
                raise CircuitError(f"Pin indices must be between 0 and {MAX_PIN_INDEX}.", line)
            high, low = map(int, bounds)
            if high > MAX_PIN_INDEX or low > MAX_PIN_INDEX:
                raise CircuitError(f"Pin indices must be between 0 and {MAX_PIN_INDEX}.", line)
            if high < low:
                raise CircuitError("Write pin ranges in descending order: name[high:low].", line)
            if len(pins) + high - low + 1 > MAX_BANK_PINS:
                raise CircuitError(f"A pin list cannot expand beyond {MAX_BANK_PINS} pins.", line)
            pins.extend(f"{match[1]}{index}" for index in range(high, low - 1, -1))
        if len(pins) > MAX_BANK_PINS:
            raise CircuitError(f"A pin list cannot expand beyond {MAX_BANK_PINS} pins.", line)
    return ", ".join(pins)
