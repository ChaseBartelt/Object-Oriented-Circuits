"""Transistor-level implementations of the built-in gate calls."""
# Built-in gate calls expand into the same transistor/component statements as user methods.
# Shared signature/arity helpers drive argument checking and help, keeping displayed pin order
# consistent with the circuits actually instantiated.

# Registry of supported built-in names and their default logical meanings.
# Callers recognize/case-normalize names before using the helpers below;
# those helpers are not independent unknown-name validators. The method
# layer can also select a user override instead of this default circuitry.
GATE_HELP = {
    "not": "Inverts its input.",
    "buf": "Drives the same logic level as its input.",
    "and": "High when all inputs are high.",
    "nand": "Low when all inputs are high.",
    "or": "High when any input is high.",
    "nor": "High when all inputs are low. With one input it acts as an inverter.",
    "xor": "High when exactly one input is high.",
    "xnor": "High when the two inputs match.",
}


# Describe formal pin order for help and argument errors. Supply/return
# come first, y is always last, and the middle positions are logic inputs.
def gate_signature(name):
    if name in {"not", "buf"}:
        return "vcc, gnd, a, y"
    # NOR permits one input, yielding an inverter. AND/NAND/OR require at
    # least two; XOR/XNOR use a fixed two-input implementation.
    if name == "nor":
        return "vcc, gnd, a, [b, ...], y"
    if name in {"and", "nand", "or"}:
        return "vcc, gnd, a, b, ..., y"
    return "vcc, gnd, a, b, y"


# Offer both explicit-ground syntax and compatible shorthand. The caller's
# binder inserts an omitted return from its enclosing gnd pin, or node 0;
# this function only builds explanatory text.
def gate_call_help(name):
    explicit = f"{name}({gate_signature(name)})"
    inputs = "a" if name in {"not", "buf", "nor"} else "a, b"
    shorthand = f"{name}(vcc, {inputs}, y)"
    # Grouping variadic inputs in parentheses makes omitted-ground calls
    # unambiguous; a flat positional call can otherwise match the full pin form.
    if name in {"and", "nand", "or", "nor"}:
        shorthand += f" or {name}(vcc, (a, b, ...), y)"
    return f"{explicit}\nWith ground omitted: {shorthand}"


# Count logic inputs only, excluding vcc, gnd, and y. Keep accepted arities
# consistent with the signatures and with gate_body()'s chosen topology.
def gate_accepts(name, input_count):
    if name in {"not", "buf"}:
        return input_count == 1
    if name in {"xor", "xnor"}:
        return input_count == 2
    return input_count >= (1 if name == "nor" else 2)


def gate_body(name, input_count):
    """Return method pins and the component statements for one gate."""
    # Return statements written in formal/local names; method expansion binds
    # them into a private scope per call. Inputs are a, b, then in3, in4, ...;
    # no Boolean evaluation or circuit solving occurs while building this list.
    inputs = tuple("ab"[i] if i < 2 else f"in{i + 1}" for i in range(input_count))
    body = []

    # A 2.2k resistor raises the output toward vcc when the pull-down is off.
    # The separate 10p capacitor stores node voltage during time simulation;
    # it is open in DC steady state. These are actual electrical components,
    # rather than a hardcoded Boolean output or propagation-delay assignment.
    def pullup(stage, out):
        body.extend((
            f"RC{stage}(2.2k) between(vcc, {out})",
            f"CNode{stage}(10p) between({out}, gnd)",
        ))

    # Build a resistor-loaded NPN pull-down network. Parallel devices pull low
    # when any input conducts (NOR); a series stack needs all inputs conducting
    # (NAND). A single device is an inverter in either arrangement.
    def inverting_stage(stage, signals, out, *, series=False):
        pullup(stage, out)
        # Parallel devices keep sharing this collector node. In a series stage,
        # advance it to each previous emitter so the devices form one continuous
        # path from output to ground.
        collector = out
        for i, signal in enumerate(signals, 1):
            base = f"base{stage}_{i}"
            # Only non-final stacked transistors need an intermediate emitter junction.
            # The final device returns to gnd; parallel emitters all return there.
            emitter = f"stack{stage}_{i}" if series and i < len(signals) else "gnd"
            # Each signal drives its transistor base through 47k, so input loading is
            # part of the electrical model. Stage/input indexes keep local component
            # and junction names distinct before per-invocation scoping.
            body.append(f"RB{stage}_{i}(47k) between({signal}, {base})")
            body.append(f"transistor Q{stage}_{i}({base}, {collector}, {emitter}, npn)")
            if series:
                collector = emitter

    # Use the standard four-NAND construction for exactly two inputs.
    # The caller has already checked arity before a,b are unpacked.
    if name in {"xor", "xnor"}:
        a, b = inputs
        # Let t=NAND(a,b), l=NAND(a,t), and r=NAND(b,t). Then NAND(l,r)
        # is a XOR b: it is high for exactly one high input. Each NAND here is
        # a real two-transistor series stage with its own pull-up and node capacitor.
        inverting_stage(1, inputs, "nand_ab", series=True)
        inverting_stage(2, (a, "nand_ab"), "left_low", series=True)
        inverting_stage(3, (b, "nand_ab"), "right_low", series=True)
        # For XNOR, keep the XOR result on an internal node and invert once more
        # into y. XOR connects its fourth stage straight to y.
        out = "inverted" if name == "xnor" else "y"
        inverting_stage(4, ("left_low", "right_low"), out, series=True)
        if name == "xnor":
            inverting_stage(5, (out,), "y")
    else:
        # The basic stages are inverting. AND inverts a NAND, OR inverts a NOR,
        # and BUF inverts an inverter. NOT/NAND/NOR already have the desired polarity.
        negate = name in {"and", "or", "buf"}
        out = "inverted" if negate else "y"
        # Choose series only for the AND/NAND family. OR/NOR use parallel paths;
        # the one-input NOT/BUF cases use a single-transistor first stage.
        inverting_stage(1, inputs, out, series=name in {"and", "nand"})
        # A second single-input stage restores positive polarity where requested.
        # Its input is the first stage's physical output node, preserving analog
        # loading and transient state between stages.
        if negate:
            inverting_stage(2, (out,), "y")
    # All returned bodies have the full explicit formal pin list. Omitted-ground
    # shorthand was resolved by the caller before these pins are bound to its
    # actual nets; reports/exporters then see ordinary components and transistors.
    return ("vcc", "gnd", *inputs, "y"), body
