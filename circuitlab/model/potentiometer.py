"""Electrical branches of an ideal linear three-terminal potentiometer."""
# Expand each three-terminal potentiometer into high-to-wiper and wiper-to-low branches.
# The returned index map lets numerical results be folded back into the original component
# order without losing terminal current or total power information.

from math import isfinite

from circuitlab.model.graph import CircuitError, Component


# At either endpoint, retain an exact short rather than a tiny invented resistance.
# Generated leg names are checked against the shared component-name set.
def potentiometer_legs(part, *, used=None):
    """Return high->wiper and wiper->low branches; zero-length legs are ideal shorts.

    Supply a shared set of case-folded names to avoid collisions across a circuit.
    A leg whose two terminals share a net has no electrical effect and may be omitted.
    """
    if part.value is None:
        raise CircuitError(f"Enter a value for {part.label or part.name} before simulation.",
                           code="MissingComponentValue")
    if part.value.imag or not isfinite(part.value.real) or part.value.real <= 0:
        raise CircuitError(f"{part.name} requires a finite positive total resistance.")
    if not isfinite(part.position) or not 0 <= part.position <= 1:
        raise CircuitError(f"{part.name} requires a wiper position from 0% to 100%.")
    if not part.wiper:
        raise CircuitError(f"{part.name} requires high, wiper, and low terminals.")
    used = set() if used is None else used
    legs = []
    for suffix, p, n, fraction in (
        ("high", part.p, part.wiper, 1 - part.position),
        ("low", part.wiper, part.n, part.position),
    ):
        value = part.value.real * fraction
        if fraction and not value:
            raise CircuitError(f"{part.name} leg resistance is outside the numeric range.")
        kind = "R" if value else "V"
        base = f"{kind}_{part.name}_{suffix}"
        name, count = base, 2
        while name.casefold() in used:
            name = f"{base}_{count}"
            count += 1
        used.add(name.casefold())
        legs.append(Component(name, kind, p, n, value, label=f"{part.label or part.name} {suffix}",
                              equation="Given" if value else "V = I x 0 = 0", order=part.order))
    return tuple(legs)


def expand_potentiometers(circuit):
    """Return solver components and the two branch indices for each original pot."""
    if not any(part.kind == "P" for part in circuit.components):
        return circuit.components, {}
    used = {part.name.casefold() for part in circuit.components}
    if len(used) != len(circuit.components):
        raise CircuitError("Give each component a unique name before simulating.", code="InvalidNetwork")
    parts, mapping = [], {}
    for part in circuit.components:
        if part.kind != "P":
            parts.append(part)
            continue
        indices = []
        try:
            legs = potentiometer_legs(part, used=used)
        except CircuitError as error:
            raise CircuitError(error.message, circuit.component_lines.get(part.name), error.code,
                               path=circuit.component_files.get(part.name)) from None
        for leg in legs:
            indices.append(len(parts) if leg.p != leg.n else None)
            if leg.p != leg.n:
                parts.append(leg)
        mapping[part.name] = tuple(indices)
    return parts, mapping
