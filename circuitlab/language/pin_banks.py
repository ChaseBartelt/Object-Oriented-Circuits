"""Expand numbered digital pins while keeping vector-width syntax distinct."""
# A range such as SW[3:0] expands into separate scalar pins in the written order.
# A width such as data[4] remains one vector signal; these two notations must not be conflated.

import re

from circuitlab.model.graph import CircuitError


# Separate limits bound an individual numeric suffix and the combined pin
# list size. A full range [4095:0] uses the entire 4096-entry list budget.
MAX_PIN_INDEX = 4095
MAX_BANK_PINS = 4096
# Indices are unsigned decimal integers with optional underscores between
# digit groups. Signs, hexadecimal notation, expressions, and missing
# bounds do not match this grammar.
DECIMAL = r"[0-9]+(?:_[0-9]+)*"
# A colon is required: name[high:low] is a scalar-pin bank, whereas
# name[width] is left for the ordinary vector declaration parser.
# Capture the bank identifier separately so its spelling is preserved.
BANK = re.compile(rf"([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*({DECIMAL})\s*:\s*({DECIMAL})\s*\]")


# Discover possible bank bases from already known scalar names. Callers
# use these bases to recognize selectors such as SW[...] instead of
# mistaking them for new width-bearing signal declarations.
def pin_bank_names(pins):
    """Recognize every possible bank prefix, including HEX0 in HEX00."""
    # Find the trailing digit run and split it at every possible position,
    # always leaving at least one digit as the index. HEX00 therefore implies
    # both HEX and HEX0. The set deduplicates prefixes; this does not require
    # a complete/contiguous bank or infer its width. Names without trailing
    # digits contribute no candidates.
    return {pin[:index] for pin in pins if (suffix := re.search(r"\d+$", pin))
            for index in range(suffix.start(), len(pin))}


def expand_pin_banks(raw, line):
    """Expand SW[3:0] to SW3, SW2, SW1, SW0; leave data[4] unchanged."""
    # Accumulate textual entries in source order. This helper expands ranges;
    # callers still validate identifiers, duplicate names, and declarations.
    # line is carried into every range/size error for source diagnostics.
    pins = []
    # Process a comma-separated declaration/header list one item at a time.
    # Strip surrounding whitespace without changing the item's identifier case.
    for item in raw.split(","):
        item = item.strip()
        # Forward ordinary scalar names and vector widths without interpreting
        # their brackets. Each unexpanded item occupies one list entry; its vector
        # width is not a count of numbered scalar pins here.
        if ":" not in item:
            pins.append(item)
        else:
            # Any item containing a colon must be a complete bank declaration.
            # Full matching prevents partially valid ranges or extra dimensions
            # from being expanded as if their remaining text were harmless.
            match = BANK.fullmatch(item)
            if not match:
                raise CircuitError("Use name[high:low] with nonnegative decimal pin indices.", line)
            # Remove decimal separators and insignificant leading zeros before numeric
            # conversion. An all-zero bound becomes "0", not empty text, and generated
            # pin suffixes use canonical integers rather than zero-padded spellings.
            bounds = [bound.replace("_", "").lstrip("0") or "0" for bound in match.groups()[1:]]
            # Reject oversized normalized decimal strings before int() so very long
            # input yields this module's source diagnostic rather than an integer
            # conversion-limit exception. Four digits is only a prefilter: values
            # such as 9999 still need the explicit numeric limit check below.
            if any(len(bound) > 4 for bound in bounds):
                raise CircuitError(f"Pin indices must be between 0 and {MAX_PIN_INDEX}.", line)
            high, low = map(int, bounds)
            if high > MAX_PIN_INDEX or low > MAX_PIN_INDEX:
                raise CircuitError(f"Pin indices must be between 0 and {MAX_PIN_INDEX}.", line)
            # Accept descending ranges, including a single member such as flag[5:5].
            # Do not reorder an ascending request: report it so source pin order
            # remains explicit and predictable for positional connections.
            if high < low:
                raise CircuitError("Write pin ranges in descending order: name[high:low].", line)
            # Include both endpoints when counting new pins, plus everything already
            # emitted by earlier scalars, vectors, or ranges. Check before extending
            # the list so a large combined expansion is rejected before allocation.
            if len(pins) + high - low + 1 > MAX_BANK_PINS:
                raise CircuitError(f"A pin list cannot expand beyond {MAX_BANK_PINS} pins.", line)
            # Walk from high down through low inclusively. Joining the unchanged bank
            # identifier with each integer turns SW[3:0] into SW3, SW2, SW1, SW0;
            # these are separate scalar identifiers, not slices of a vector signal.
            pins.extend(f"{match[1]}{index}" for index in range(high, low - 1, -1))
        # The range branch was checked in advance; this guard also covers plain
        # items appended above. Repeated names count as entries here even though
        # their eventual duplicate-name validation belongs to the caller.
        if len(pins) > MAX_BANK_PINS:
            raise CircuitError(f"A pin list cannot expand beyond {MAX_BANK_PINS} pins.", line)
    # Return normalized source text rather than a declaration object. Later
    # parsers can reuse their existing comma-separated pin-list handling.
    return ", ".join(pins)
