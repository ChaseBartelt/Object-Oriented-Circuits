"""Presentation of the solver's quantities, equations, and measurements."""

import cmath
from math import degrees, pi, sqrt

from circuitlab.math.numbers import format_value
from circuitlab.math.quantities import QUANTITIES
from circuitlab.language.syntax import UNITS


def cosine(dc, peak, omega, phase, unit):
    if not peak:
        return format_value(dc, unit)
    offset = f"{dc:.8g} + " if dc else ""
    return f"{offset}{peak:.8g} cos({omega:.8g} t {phase:+.8g}°) {unit}"


def pin_rows(result, pins):
    """Report node voltage and the terminal quantities of every incident part."""
    if not result or not result.network:
        return []
    rows = []
    is_ac = result.circuit.mode in {"ac", "mixed"}
    mixed = result.dc_network is not None
    for node, name in pins.items():
        if node not in result.network.nodes:
            continue

        def add(label, meaning, value):
            rows.append((f"{name} · {label}", meaning, value))

        add("Node", "Schematic connection", node)
        for network, ac in ((result.dc_network, False), (result.network, is_ac)):
            if network is None:
                continue
            tag = "AC " if ac else "DC " if mixed else ""
            v = network.voltage[network.nodes.index(node)]
            add(
                tag + ("voltage RMS" if ac else "voltage"),
                f"V({node}) − V(0)",
                format_value(v, "V", ac),
            )
            if ac:
                add(
                    "AC voltage peak",
                    "√2 × |V RMS|",
                    format_value(sqrt(2) * abs(v), "V"),
                )
                if v:
                    add(
                        "AC voltage phase",
                        "arg(V RMS)",
                        format_value(degrees(cmath.phase(v)), "deg"),
                    )
                if network.frequency is not None:
                    add(
                        "AC frequency",
                        "Circuit frequency",
                        format_value(network.frequency, "Hz"),
                    )
            indices = {part_name: i for i, part_name in enumerate(network.names)}
            for part in result.circuit.components:
                part_name = (part.label or part.name).replace("_", " ")
                if part.kind == "P":
                    for terminal, net, amps in zip(("high", "wiper", "low"), part.terminal_nets,
                                                   network.terminal_current[part.name]):
                        if node == net:
                            add(f"{tag}{part_name} {terminal} current", f"Into {terminal}",
                                format_value(amps, "A", ac))
                    continue
                if part.kind == "Q" and part.name in network.base_current:
                    collector = network.branch_current[indices[part.name]]
                    base = network.base_current[part.name]
                    for terminal, net, amps in (
                        ("base", part.base, base), ("collector", part.p, collector),
                        ("emitter", part.n, -collector - base),
                    ):
                        if node == net:
                            add(f"{part_name} {terminal} current", f"Into {terminal}",
                                format_value(amps, "A"))
                    continue
                if part.kind == "O":
                    for terminal, net in (
                        ("in+", part.in_plus),
                        ("in−", part.in_minus),
                    ):
                        if net == node:
                            add(
                                f"{tag}{part_name} {terminal} current",
                                "Ideal op-amp input",
                                "0 A",
                            )
                if node not in (part.p, part.n):
                    continue
                i = indices[part.name]
                direction = 1 if node == part.p else -1
                add(
                    f"{tag}{part_name} current away",
                    f"From {node} into component",
                    format_value(direction * network.branch_current[i], "A", ac),
                )
                add(
                    f"{tag}{part_name} voltage drop",
                    "This terminal − opposite terminal",
                    format_value(direction * network.branch_voltage[i], "V", ac),
                )
                power = network.branch_power[i]
                add(
                    f"{tag}{part_name} real power",
                    "Absorbed by component",
                    format_value(power.real, "W"),
                )
                if ac:
                    add(
                        f"AC {part_name} reactive power",
                        "Absorbed by component",
                        format_value(power.imag, "var"),
                    )
                    add(
                        f"AC {part_name} apparent power",
                        "|P + jQ|",
                        format_value(abs(power), "VA"),
                    )
        network = result.network
        if is_ac and network.frequency is not None:
            dc_network = result.dc_network
            v_dc = dc_network.voltage[dc_network.nodes.index(node)].real if mixed else 0
            v_ac = network.voltage[network.nodes.index(node)]
            add(
                "v(t)",
                "DC voltage + AC waveform" if mixed else "AC waveform",
                cosine(
                    v_dc,
                    sqrt(2) * abs(v_ac),
                    2 * pi * network.frequency,
                    degrees(cmath.phase(v_ac)),
                    "V",
                ),
            )
    return rows


def result_rows(result):
    """Build the display rows for inputs, derived values, and network results."""
    c = result.circuit
    ac = c.mode in {"ac", "mixed"}
    info = QUANTITIES["ac" if ac else c.mode]
    given, derived = [], []

    def add(label, equation, value, unit="", polar=False, provided=False):
        rendered = value if isinstance(value, str) else format_value(value, unit, polar)
        (given if provided else derived).append(
            (label.replace("_", " "), equation, rendered)
        )

    for key, (name, unit) in info.items():
        provided = key in c.known
        values = c.known if provided else result.calculated
        if key in values:
            add(
                f"{key} · {name}",
                c.equations.get(key, "Given") if provided else c.equations[key],
                values[key],
                unit,
                ac and key in {"V", "I", "Z", "ZL", "ZC"},
                provided,
            )
    for reading in result.readings:
        add(*reading)
    if not c.components and ac:
        values = c.known | result.calculated
        if "w" in values and not any(
            "conflict" in note.lower() for note in result.diagnostics
        ):
            for key in "VI":
                label, pk, phase = key.lower() + "(t)", key + "pk", "phase" + key
                unit = "V" if key == "V" else "A"
                if pk in values and not values[pk]:
                    add(label, f"{label} = 0", 0, unit)
                elif pk in values and phase in values:
                    add(
                        label,
                        f"{label} = {pk} x cos(wt + {phase})",
                        cosine(0, values[pk], values["w"], values[phase], unit),
                    )
    for network, is_ac in ((result.dc_network, False), (result.network, ac)):
        if not network:
            continue
        prefix = ("AC " if is_ac else "DC ") if c.mode == "mixed" else ""
        basis = " RMS" if is_ac else ""
        active = [p for p in c.components if p.kind in "VI" and p.value]
        ref = (
            active[0].value
            if is_ac and len(active) == 1 and active[0].kind == "V"
            else None
        )
        for p in c.components:
            name = p.label or p.name
            if p.kind == "P":
                add(name + " total resistance", "R_high + R_low", p.value, "ohm", provided=True)
                add(name + " position", "Fraction from low to high", 100 * p.position, "%", provided=True)
                continue
            if p.kind == "Q":
                add(name + " model", "Generic Ebers–Moll", p.transistor_type.upper(), provided=True)
                continue
            if p.kind == "S":
                add(name + " threshold", "V(control) - V(reference)", p.threshold, "V", provided=True)
                continue
            if p.kind == "O":
                add(
                    prefix + name + " model",
                    "V(in+) = V(in-)",
                    "Ideal op amp",
                    provided=True,
                )
                continue
            source = p.kind in "VI"
            label = name if c.grouped else f"{p.name} ({p.p} → {p.n})"
            if c.grouped and p.kind == "V":
                label += " voltage" + basis
            elif not c.grouped and source:
                label += basis
            label = prefix + label
            dc_source = not is_ac and source and c.mode == "mixed"
            add(
                label,
                "Given" if dc_source else p.equation,
                p.dc if dc_source else p.value,
                UNITS[p.kind],
                is_ac and (source or p.kind == "Z"),
                True,
            )
        for node, value in zip(network.nodes, network.voltage):
            if node != "0" and (not c.grouped or node in c.named_wires):
                add(
                    f"{prefix}Node {node}{basis}",
                    "Σ I_node = 0 (nodal equations)",
                    value,
                    "V",
                    is_ac,
                )
        for p, v, i, power in zip(
            c.components,
            network.branch_voltage,
            network.branch_current,
            network.branch_power,
        ):
            name = prefix + (p.label if c.grouped else p.name)
            if p.kind == "P":
                add(name + " high-low voltage" + basis, "V(high) - V(low)", v, "V", is_ac)
                for terminal, amps in zip(("high", "wiper", "low"), network.terminal_current[p.name]):
                    add(name + f" {terminal} current" + basis, f"Into {terminal}", amps, "A", is_ac)
                add(name + " power", "P_high_leg + P_low_leg", power.real, "W")
                continue
            if p.kind == "Q":
                base = network.base_current[p.name]
                add(name + " collector-emitter voltage", "V(C) − V(E)", v, "V")
                add(name + " base current", "Into base", base, "A")
                add(name + " collector current", "Into collector", i, "A")
                add(name + " emitter current", "−I(B) − I(C)", -base - i, "A")
                add(name + " power", "V(CE) × I(C) + V(BE) × I(B)", power, "W")
                continue
            if p.kind == "O":
                add(
                    name + " output voltage" + basis,
                    f"Vout = V(out) - V({p.n})",
                    v,
                    "V",
                    is_ac,
                )
                add(
                    name + " current into output" + basis,
                    "KCL at the output",
                    i,
                    "A",
                    is_ac,
                )
                add(name + " input currents", "I(in+) = I(in-) = 0", "0 A")
                continue
            if p.kind != "V":
                add(name + " voltage" + basis, "Vbranch = V+ - V-", v, "V", is_ac)
            if p.kind != "I":
                formulas = {
                    "R": "I = Vbranch / R",
                    "C": "I = j x 2πf x C x Vbranch"
                    if is_ac
                    else "I = 0 (DC capacitor)",
                    "L": "I = Vbranch / (j x 2πf x L)"
                    if is_ac
                    else "Σ I_node = 0 with V_L = 0",
                    "X": "I = Vbranch / (jX)",
                    "Z": "I = Vbranch / Z",
                }
                add(
                    name + " current" + basis,
                    formulas.get(p.kind, "Σ I_node = 0 (KCL)"),
                    i,
                    "A",
                    is_ac,
                )
            if is_ac:
                power_text = " / ".join(
                    format_value(number, unit)
                    for number, unit in (
                        (power.real, "W"),
                        (power.imag, "var"),
                        (abs(power), "VA"),
                    )
                )
                add(
                    name + " power (P / Q / |S|)",
                    "P + jQ = V x conj(I), |S| = |P + jQ|",
                    power_text,
                )
                if p.kind in "RCLXZ":
                    add(
                        name + " voltage peak",
                        "Vpk = √2 x |Vbranch|",
                        sqrt(2) * abs(v),
                        "V",
                    )
                    if ref and v:
                        phase = degrees(cmath.phase(v / ref))
                        add(
                            name + " phase vs source",
                            "Phase = ∠(Vbranch / Vsource)",
                            phase,
                            "deg",
                        )
                        if network.frequency:
                            add(
                                name + " delay (principal)",
                                "Delay = -phase / (360 x f)",
                                -phase / (360 * network.frequency),
                                "s",
                            )
            else:
                add(name + " power", "P = V x I", power, "W")
                if p.kind == "L":
                    add(
                        name + " stored energy",
                        "E_L = (1/2) L I^2",
                        0.5 * p.value * abs(i) ** 2,
                        "J",
                    )
        if network.equivalent:
            for key, label, equation, unit in (
                ("voltage", "Open-circuit voltage", "V_OC = V+ - V-", "V"),
                ("impedance", "Equivalent impedance", "Z_eq = V_test / 1 A", "ohm"),
                ("current", "Short-circuit current", "I_SC = V_OC / Z_eq", "A"),
            ):
                value = network.equivalent[key]
                if value is not None:
                    add(
                        prefix + label + (basis if key != "impedance" else ""),
                        equation,
                        value,
                        unit,
                        is_ac,
                    )
    if result.dc_network and result.network:
        omega = 2 * pi * result.network.frequency
        for k, part in enumerate(c.components):
            for key, attribute, unit in (
                ("V", "branch_voltage", "V"),
                ("I", "branch_current", "A"),
            ):
                dc = getattr(result.dc_network, attribute)[k].real
                ph = getattr(result.network, attribute)[k]
                label = f"{key.lower()}(t)"
                equation = f"{label} = {key}_DC"
                if ph:
                    equation += f" + √2 x |{key}| x cos(wt + ∠{key})"
                add(
                    f"{part.label or part.name} {label}",
                    equation,
                    cosine(
                        dc, sqrt(2) * abs(ph), omega, degrees(cmath.phase(ph)), unit
                    ),
                )
    return given + derived


def component_labels(circuit):
    """Format schematic captions with component values and source information."""
    labels = {}
    frequency = circuit.known.get("f")
    if frequency is None and circuit.known.get("w"):
        frequency = circuit.known["w"] / (2 * pi)
    if frequency is None and circuit.known.get("T"):
        frequency = 1 / circuit.known["T"]
    for p in circuit.components:
        name = p.label or p.name
        labels[p.name] = label = [name.replace("_", " ")]
        if p.kind in {"O", "H", "S"}:
            continue
        if p.kind == "Q":
            label.append(p.transistor_type.upper())
            if p.model:
                label.append(p.model)
            continue
        unit = UNITS[p.kind]
        if p.kind == "P":
            label.append((format_value(p.value, "ohm") if p.value is not None else "? Ω")
                         + f" · {100 * p.position:g}%")
            continue
        if p.value is None:
            if p.kind != "Z":
                label.append("? " + ("Ω" if unit == "ohm" else unit))
        elif p.pulse is not None:
            label.append(f"{format_value(p.pulse.low, 'V')} to {format_value(p.pulse.high, 'V')}")
        elif p.equation == "V = I x 0 = 0":
            label.append("short")
        elif p.kind == "Z":
            # Keep schematic captions compact; result tables also show polar form.
            label.append(format_value(p.value, unit, polar=True).split(" · ")[-1])
        elif p.kind not in "VI" or circuit.mode == "dc":
            label.append(format_value(p.value, unit))
        else:
            levels = []
            if circuit.mode == "mixed" and (p.dc or not p.value):
                levels.append(format_value(p.dc, unit) + " DC")
            if p.value or not levels:
                levels.append(
                    f"{format_value(sqrt(2) * abs(p.value), unit)} peak, {degrees(cmath.phase(p.value)):.4g}°"
                )
            label.append(" + ".join(levels))
            if p.value and frequency is not None:
                label.append(format_value(frequency, "Hz"))
    return labels
