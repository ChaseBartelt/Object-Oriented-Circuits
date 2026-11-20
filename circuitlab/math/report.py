"""Presentation of the solver's quantities, equations, and measurements."""
# Numerical results remain separate from their presentation labels and equations.
# These functions translate quantities and component-ordered arrays into rows and captions,
# including separate DC/AC information when both operating points are available.

import cmath
from math import degrees, pi, sqrt

from circuitlab.math.numbers import format_value
from circuitlab.math.quantities import QUANTITIES
from circuitlab.language.syntax import UNITS
from circuitlab.model.graph import AudioAmpModel, OpAmpModel


# Build display text from a DC offset and an already-converted AC peak.
# omega is angular frequency and phase is reported in degrees. This
# formats a solved waveform; it does not evaluate or simulate it.
def cosine(dc, peak, omega, phase, unit):
    # Without an AC amplitude, the waveform is just its constant offset.
    # Use normal scalar formatting instead of showing a meaningless phase.
    if not peak:
        return format_value(dc, unit)
    offset = f"{dc:.8g} + " if dc else ""
    return f"{offset}{peak:.8g} cos({omega:.8g} t {phase:+.8g}°) {unit}"


# Use the public component ordering and terminal-current mapping so expanded devices
# still expose meaningful quantities under their original schematic identity.
def pin_rows(result, pins):
    """Report node voltage and the terminal quantities of every incident part."""
    # Pin inspection requires a solved network. A schematic-only result or
    # standalone quantity calculation has no node/branch arrays to inspect.
    if not result or not result.network:
        return []
    rows = []
    is_ac = result.circuit.mode in {"ac", "mixed"}
    mixed = result.dc_network is not None
    # pins maps electrical node names to user-facing captions. Ignore pins
    # that are absent from this solved graph, preserving the caller's order.
    for node, name in pins.items():
        if node not in result.network.nodes:
            if node in result.network.unavailable_nodes:
                rows.append((name,"Behavioral comparator interior","Not resolved by the numerical macromodel"))
            continue

        # Each row is (caption, explanation, rendered value). Prefix it with the
        # selected pin caption so several inspected nodes can share one table.
        def add(label, meaning, value):
            rows.append((f"{name} · {label}", meaning, value))

        add("Node", "Schematic connection", node)
        # A mixed result keeps two solved networks: DC offset and AC RMS phasor.
        # Report both separately; an ordinary DC/AC result contributes only its
        # main network. Stored node voltages already use the solver's references.
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
            # Convert the RMS magnitude to a sinusoidal peak. Phase is omitted at
            # zero amplitude because it is undefined, and frequency appears only
            # when the network actually carries one.
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
            # Resolve branch positions through returned component names. Backend
            # expansion, such as splitting a potentiometer into legs, must already
            # be folded into these public names and terminal-current mappings.
            indices = {part_name: i for i, part_name in enumerate(network.names)}
            for part in result.circuit.components:
                part_name = (part.label or part.name).replace("_", " ")
                if part.name in network.unavailable_components:
                    continue
                if part.name in network.modeled_terminals:
                    for role,net,amps in network.modeled_terminals[part.name]:
                        if net == node:
                            add(f"{tag}{part_name} {role} current",f"Behavioral comparator: into {role}",
                                format_value(amps,"A",ac))
                    continue
                # A potentiometer has three distinct terminal currents entering the
                # device. Use its high/wiper/low mapping rather than assigning one branch
                # current to all terminals. Tied nets can legitimately match several pins.
                if part.kind == "P":
                    for terminal, net, amps in zip(("high", "wiper", "low"), part.terminal_nets,
                                                   network.terminal_current[part.name]):
                        if node == net:
                            add(f"{tag}{part_name} {terminal} current", f"Into {terminal}",
                                format_value(amps, "A", ac))
                    continue
                # When native BJT metadata is available, the branch current is collector
                # current. Base current is separate; emitter current follows KCL as
                # -collector-base. All three signs mean current entering the device.
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
                # Ideal op-amp input pins draw zero current. If this same net also touches
                # an output branch terminal, the generic branch rows below still apply.
                if part.kind == "A":
                    values = network.terminal_current.get(part.name, ())
                    roles = ("in+", "in−", "output", "V+", "V-", "gain1", "gain8", "bypass")
                    for role, net, amps in zip(roles, part.terminal_nets, values):
                        if net == node:
                            add(f"{tag}{part_name} {role} current", f"Into {role}",
                                format_value(amps, "A", ac))
                    continue
                if part.kind == "O":
                    if part.opamp and part.opamp.supply_current:
                        terminal = network.terminal_current.get(part.name)
                        if terminal:
                            for role, net, amps in zip(("V+", "V-"), (part.vplus, part.vminus), terminal[-2:]):
                                if net == node:
                                    add(f"{tag}{part_name} {role} current", f"Into {role}",
                                        format_value(amps, "A", ac))
                    for input_index, (terminal, net) in enumerate((
                        ("in+", part.in_plus),
                        ("in−", part.in_minus),
                    )):
                        if net == node:
                            inputs = network.terminal_current.get(part.name)
                            loaded = part.opamp and part.opamp.common_impedance is not None and inputs
                            add(
                                f"{tag}{part_name} {terminal} current",
                                "Common input impedance" if loaded else "Ideal op-amp input",
                                format_value(inputs[input_index], "A", ac) if loaded else "0 A",
                            )
                if node not in (part.p, part.n):
                    continue
                i = indices[part.name]
                # Backend branch voltage/current point p->n. Reverse both at terminal n
                # so the displayed current points away from the inspected node into the
                # component, and the drop is this terminal minus the opposite terminal.
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
                # Whole-component absorbed power does not change sign when inspecting
                # the opposite terminal. Positive real power is absorption; a supplying
                # source can have negative real power.
                power = network.branch_power[i]
                add(
                    f"{tag}{part_name} real power",
                    "Absorbed by component",
                    format_value(power.real, "W"),
                )
                # AC branch power is P+jQ from RMS phasors. Display its real component,
                # signed reactive component, and apparent magnitude as different measures.
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
        # Reconstruct node v(t) from the AC phasor and optional DC node offset.
        # Frequency is required for a time expression, even when a phasor-only
        # steady-state circuit could be solved without a specified frequency.
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


# Assemble provided values, derived equations, operating-point quantities, and explicit
# probe readings into the shared display-row format.
def result_rows(result):
    """Build the display rows for inputs, derived values, and network results."""
    c = result.circuit
    ac = c.mode in {"ac", "mixed"}
    # Mixed analysis uses the AC quantity catalog for phasor labels/units;
    # its separate network passes below supply the DC and AC operating points.
    info = QUANTITIES["ac" if ac else c.mode]
    # Accumulate provided definitions and derived readings separately, then
    # return all provided rows first. Each group retains its insertion order.
    given, derived = [], []

    # One row builder centralizes (label, equation, formatted text). Existing
    # strings, including waveforms and P/Q summaries, bypass numeric formatting.
    # Replace underscores only in visible labels, leaving solver identities intact.
    def add(label, equation, value, unit="", polar=False, provided=False):
        rendered = value if isinstance(value, str) else format_value(value, unit, polar)
        (given if provided else derived).append(
            (label.replace("_", " "), equation, rendered)
        )

    # Follow catalog order rather than dictionary order of whatever the solver
    # happened to derive first. A supplied quantity takes precedence for its row;
    # derivation text comes from the analysis/compiler's saved equations.
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
    # Explicit measure/probe requests were evaluated upstream and already
    # carry the add() arguments, including their unit and phasor display flag.
    for reading in result.readings:
        add(*reading)
    # Quantity-only AC calculations have no network node rows. If the solved
    # quantities provide an angular rate, peak, and phase, show a corresponding
    # v(t)/i(t) expression without constructing a fictitious component network.
    if not c.components and ac:
        values = c.known | result.calculated
        # Do not present a derived waveform as consistent while the quantity
        # solver is reporting conflicting inputs.
        if "w" in values and not any(
            "conflict" in note.lower() for note in result.diagnostics
        ):
            for key in "VI":
                label, pk, phase = key.lower() + "(t)", key + "pk", "phase" + key
                unit = "V" if key == "V" else "A"
                # A known zero peak uniquely gives a zero waveform without needing phase.
                # A nonzero peak requires its phase rather than assuming one.
                if pk in values and not values[pk]:
                    add(label, f"{label} = 0", 0, unit)
                elif pk in values and phase in values:
                    add(
                        label,
                        f"{label} = {pk} x cos(wt + {phase})",
                        cosine(0, values[pk], values["w"], values[phase], unit),
                    )
    # Process DC and AC networks independently, adding prefixes in mixed
    # analysis. RMS applies to AC voltage/current rows, not to impedance or
    # real/reactive power values.
    for network, is_ac in ((result.dc_network, False), (result.network, ac)):
        if not network:
            continue
        prefix = ("AC " if is_ac else "DC ") if c.mode == "mixed" else ""
        basis = " RMS" if is_ac else ""
        # A relative phase reference is unambiguous here only for a single nonzero
        # AC source that is a voltage source. With multiple sources or just a
        # current source, omit the extra phase-vs-source/delay interpretation.
        active = [p for p in c.components if p.kind in "VI" and p.value]
        ref = (
            active[0].value
            if is_ac and len(active) == 1 and active[0].kind == "V"
            else None
        )
        for p in c.components:
            name = p.label or p.name
            if p.name in network.unavailable_components:
                add(name,"Internal schematic component","Resolved by the comparator macromodel",provided=True)
                continue
            if p.name in network.modeled_components:
                add(name+" model","Behavioral output stage",network.modeled_components[p.name],provided=True)
                continue
            # Report original potentiometer resistance and position, not generated
            # leg definitions. Position is stored as a fraction and displayed as percent.
            if p.kind == "P":
                add(name + " total resistance", "R_high + R_low", p.value, "ohm", provided=True)
                add(name + " position", "Fraction from low to high", 100 * p.position, "%", provided=True)
                continue
            # Native transistor results use the generic Ebers-Moll model. The given
            # model row identifies its NPN/PNP polarity rather than a passive value.
            if p.kind == "Q":
                add(name + " model", "Generic Ebers–Moll", p.transistor_type.upper(), provided=True)
                continue
            # A controlled switch has a control threshold; a manual K switch has an
            # open/closed state. Show those model meanings instead of treating either
            # symbol as an ordinary user-entered resistance caption.
            if p.kind == "S":
                add(name + " threshold", "V(control) - V(reference)", p.threshold, "V", provided=True)
                continue
            if p.kind == "K":
                add(name + " state", "Manual switch", "Closed" if p.closed else "Open", provided=True)
                continue
            # The op-amp definition is an ideal voltage constraint. Its output voltage
            # and solved current are reported separately in the branch section.
            if p.kind == "A":
                add(prefix + name + " model", "Eight-pin gain/bypass model", "Behavioral audio amplifier", provided=True)
                for key, unit in (("gain_default", ""), ("gain_max", ""), ("gain_resistor", "ohm"),
                                  ("output_bias", ""), ("supply_current", "A"), ("bypass_resistance", "ohm")):
                    add(prefix + name + " " + key.replace("_", "."), "Declared model/default",
                        getattr(p.audio or AudioAmpModel(), key), unit, provided=True)
                continue
            if p.kind == "O":
                if p.opamp is not None and p.opamp != OpAmpModel():
                    add(prefix + name + " model", "Vout = A(s) × (V(in+) − V(in−) + offset)",
                        "Behavioral op amp", provided=True)
                    for key, unit in (("gain", ""), ("bandwidth", "Hz"), ("slew", "V/s"),
                                      ("offset", "V"), ("supply_current", "A")):
                        value = getattr(p.opamp, key)
                        add(prefix + name + " " + key.replace("_", "."), "Declared model/default",
                            "Ideal/unlimited" if value is None else value, unit, provided=True)
                    for key in ("common_min", "common_max"):
                        limit = getattr(p.opamp, key)
                        add(prefix + name + " " + key.replace("_", "."), "Model validity range",
                            limit.source() if limit is not None else "Unrestricted", provided=True)
                    for key in ("output_min", "output_max"):
                        limit = getattr(p.opamp, key)
                        add(prefix + name + " " + key.replace("_", "."), "Output saturation bound",
                            limit.source() if limit is not None else "Unlimited", provided=True)
                    for key, unit in (("common_rejection", "dB"), ("common_impedance", "ohm")):
                        value = getattr(p.opamp, key)
                        add(prefix + name + " " + key.replace("_", "."), "Declared model/default",
                            "Ideal/infinite" if value is None else value, unit, provided=True)
                    continue
                add(
                    prefix + name + " model",
                    "V(in+) = V(in-)",
                    "Ideal op amp",
                    provided=True,
                )
                continue
            # Grouped circuits use their friendly part labels; explicit networks add
            # p->n orientation to definitions. Mark AC source amplitudes as RMS.
            source = p.kind in "VI"
            label = name if c.grouped else f"{p.name} ({p.p} → {p.n})"
            if c.grouped and p.kind == "V":
                label += " voltage" + basis
            elif not c.grouped and source:
                label += basis
            label = prefix + label
            # Mixed source offsets live in p.dc, while p.value holds the RMS phasor.
            # Use the appropriate value/equation for this pass without changing the
            # component shared by both solved networks.
            dc_source = not is_ac and source and c.mode == "mixed"
            add(
                label,
                "Given" if dc_source else p.equation,
                p.dc if dc_source else p.value,
                UNITS[p.kind],
                is_ac and (source or p.kind == "Z"),
                True,
            )
        # Hide standard node 0. For grouped circuits also hide implementation-only
        # nets, keeping explicitly named wires; explicit networks show other nodes.
        # These rows expose solved values rather than re-solving nodal equations.
        for node, value in zip(network.nodes, network.voltage):
            if node != "0" and (not c.grouped or node in c.named_wires):
                add(
                    f"{prefix}Node {node}{basis}",
                    "Σ I_node = 0 (nodal equations)",
                    value,
                    "V",
                    is_ac,
                )
        # These arrays must follow original circuit.components order. Device
        # expansion is folded by the backend before reporting reaches this zip.
        # Special devices need their own terminal/voltage meanings below.
        for p, v, i, power in zip(
            c.components,
            network.branch_voltage,
            network.branch_current,
            network.branch_power,
        ):
            name = prefix + (p.label if c.grouped else p.name)
            if p.name in network.unavailable_components:
                continue
            if p.name in network.modeled_components:
                add(name+" collector-emitter voltage", "Behavioral comparator output",v,"V",is_ac)
                add(name+" collector current", "Current into open collector",i,"A",is_ac)
                add(name+" power", "Collector sink + comparator bias draw",power,"W",is_ac)
                continue
            if p.kind == "P":
                # A pot's high-low voltage is not enough to reconstruct its three currents.
                # Use stored terminal currents and the backend's sum of both leg powers,
                # which also handles a loaded wiper correctly.
                add(name + " high-low voltage" + basis, "V(high) - V(low)", v, "V", is_ac)
                for terminal, amps in zip(("high", "wiper", "low"), network.terminal_current[p.name]):
                    add(name + f" {terminal} current" + basis, f"Into {terminal}", amps, "A", is_ac)
                add(name + " power", "P_high_leg + P_low_leg", power.real, "W")
                continue
            if p.kind == "Q":
                # Collector and base currents determine emitter current by KCL. BJT power
                # already includes Vce*Ic plus Vbe*Ib, preserving the third terminal's
                # contribution instead of treating the transistor as a two-terminal load.
                base = network.base_current[p.name]
                add(name + " collector-emitter voltage", "V(C) − V(E)", v, "V")
                add(name + " base current", "Into base", base, "A")
                add(name + " collector current", "Into collector", i, "A")
                add(name + " emitter current", "−I(B) − I(C)", -base - i, "A")
                add(name + " power", "V(CE) × I(C) + V(BE) × I(B)", power, "W")
                continue
            # Op-amp branch voltage is output relative to its assigned return p.n.
            # Branch current enters the output terminal toward that return; it can
            # be negative while the amplifier supplies a load.
            if p.kind in {"O", "A"}:
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
                if p.kind == "O" and p.opamp and p.opamp.common_impedance is not None:
                    terminal = network.terminal_current[p.name]
                    for label, amps in zip(("in+", "in−"), terminal[:2]):
                        add(name + " " + label + " current", "Common input impedance", amps, "A", is_ac)
                else:
                    add(name + " input currents", "I(in+) = I(in-) = 0", "0 A")
                if p.kind == "A":
                    add(name + " power", "Sum of terminal powers", power, "W", is_ac)
                elif p.opamp and (p.opamp.supply_current or p.opamp.common_impedance is not None):
                    add(name + " power", "Output branch + input/supply draw", power, "W", is_ac)
                continue
            # A voltage source already has its specified voltage in the definitions;
            # a current source already has its specified current. Suppress those
            # duplicate rows while retaining their other solved branch quantity.
            if p.kind != "V":
                add(name + " voltage" + basis, "Vbranch = V+ - V-", v, "V", is_ac)
            if p.kind != "I":
                # These equations explain the reported current; they do not recalculate it.
                # In DC a capacitor is open, while an inductor has zero voltage and its
                # current is determined by the surrounding KCL constraints.
                formulas = {
                    "R": "I = Vbranch / R",
                    "K": "I = Vbranch / R_switch",
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
                # Summarize complex power as three scalar values with distinct units:
                # P in W, signed Q in var, and |P+jQ| in VA. Pass the assembled string
                # through add() unchanged rather than formatting it as a single number.
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
                # Passive branch voltage peaks use sqrt(2)*|RMS phasor|. An available
                # single voltage-source reference also permits phase relative to that source.
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
                        # This converts principal phase to a phase-equivalent signed delay.
                        # It is ambiguous by whole periods and is not a broadband group-delay
                        # calculation or a unique absolute propagation time.
                        if network.frequency:
                            add(
                                name + " delay (principal)",
                                "Delay = -phase / (360 x f)",
                                -phase / (360 * network.frequency),
                                "s",
                            )
            else:
                add(name + " power", "P = V x I", power, "W")
                # A DC inductor can store energy even though its branch voltage is zero.
                # Use its solved current in E=0.5*L*|I|**2.
                if p.kind == "L":
                    add(
                        name + " stored energy",
                        "E_L = (1/2) L I^2",
                        0.5 * p.value * abs(i) ** 2,
                        "J",
                    )
        # Expose the backend's Thevenin/Norton equivalent without recomputing it.
        # A zero-impedance port can leave current=None; omit that undefined row.
        # Only voltage/current receive an RMS label, while impedance retains ohms.
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
    # A mixed result combines aligned DC offsets and AC branch phasors into
    # v(t)/i(t) text at the shared AC frequency. Keep the same original part
    # index across both arrays; zero AC amplitude becomes a constant expression.
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
    # Definitions are displayed before calculated rows even when a definition
    # was discovered during a later network pass.
    return given + derived


# Keep captions compact and avoid substituting numeric values for devices whose
# meaning comes from terminal roles or switch state rather than a passive-unit value.
def component_labels(circuit):
    """Format schematic captions with component values and source information."""
    # Return {component_id: [caption_line, ...]}. Schematic rendering can call
    # this before solving, so frequency is taken from supplied f/w/T values
    # and unspecified values retain placeholders rather than requiring results.
    labels = {}
    frequency = circuit.known.get("f")
    if frequency is None and circuit.known.get("w"):
        frequency = circuit.known["w"] / (2 * pi)
    if frequency is None and circuit.known.get("T"):
        frequency = 1 / circuit.known["T"]
    for p in circuit.components:
        name = p.label or p.name
        # Keep the true component name as the lookup key. Only the displayed
        # caption uses a preferred label and spaces instead of underscores.
        labels[p.name] = label = [name.replace("_", " ")]
        # Manual switches show their interactive state, not the finite numerical
        # resistance used internally to model the selected contact state.
        if p.kind == "K":
            label.append("Closed" if p.closed else "Open")
            continue
        # Op amps, package pinouts, and controlled switches keep their name-only
        # caption here; their symbol/pin roles carry meaning beyond a scalar value.
        if p.kind in {"O", "H", "S", "A"}:
            continue
        # A schematic may show a named SPICE model even when the native solver
        # cannot run it. Preserve polarity and optional model name as caption lines.
        if p.kind == "Q":
            label.append(p.transistor_type.upper())
            if p.model:
                label.append(p.model)
            continue
        unit = UNITS[p.kind]
        # Keep a pot's total resistance (or its unknown placeholder) together
        # with wiper position, rather than displaying generated resistor legs.
        if p.kind == "P":
            label.append((format_value(p.value, "ohm") if p.value is not None else "? Ω")
                         + f" · {100 * p.position:g}%")
            continue
        # Unknown theoretical impedances are name-only; other unspecified values
        # show a question mark with their expected unit. Unknown remains distinct
        # from a real zero, open, or short.
        if p.value is None:
            if p.kind != "Z":
                label.append("? " + ("Ω" if unit == "ohm" else unit))
        # Pulse-voltage captions show their low/high levels instead of pretending
        # the source has one fixed DC or sinusoidal amplitude.
        elif p.pulse is not None:
            label.append(f"{format_value(p.pulse.low, 'V')} to {format_value(p.pulse.high, 'V')}")
        # Compiler-generated zero-volt connection elements represent ideal shorts;
        # label that role rather than presenting them as ordinary zero-valued supplies.
        elif p.equation == "V = I x 0 = 0":
            label.append("short")
        # The shared polar formatter also supplies a rectangular representation.
        # Select its final "real +/- jimag unit" segment to retain reactance in a
        # compact caption; the fuller report can still show magnitude and phase.
        elif p.kind == "Z":
            # Keep schematic captions compact; result tables also show polar form.
            label.append(format_value(p.value, unit, polar=True).split(" · ")[-1])
        elif p.kind not in "VI" or circuit.mode == "dc":
            label.append(format_value(p.value, unit))
        else:
            # AC source definitions store RMS phasors, while captions display peak
            # magnitude and angle. In mixed mode include a nonzero DC offset, or the
            # DC level alone when the AC part is zero.
            levels = []
            if circuit.mode == "mixed" and (p.dc or not p.value):
                levels.append(format_value(p.dc, unit) + " DC")
            # Retain at least one level even for a zero-amplitude source. Avoid adding
            # an unnecessary zero AC level beside an already displayed DC-only level.
            if p.value or not levels:
                levels.append(
                    f"{format_value(sqrt(2) * abs(p.value), unit)} peak, {degrees(cmath.phase(p.value)):.4g}°"
                )
            label.append(" + ".join(levels))
            # Append frequency as a separate caption line only for an active AC part.
            # A DC-only mixed source does not need a sinusoidal frequency caption.
            if p.value and frequency is not None:
                label.append(format_value(frequency, "Hz"))
    return labels
