"""Transistor-level implementations of the built-in gate calls."""

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


def gate_signature(name):
    if name in {"not", "buf"}:
        return "vcc, gnd, a, y"
    if name == "nor":
        return "vcc, gnd, a, [b, ...], y"
    if name in {"and", "nand", "or"}:
        return "vcc, gnd, a, b, ..., y"
    return "vcc, gnd, a, b, y"


def gate_call_help(name):
    explicit = f"{name}({gate_signature(name)})"
    inputs = "a" if name in {"not", "buf", "nor"} else "a, b"
    shorthand = f"{name}(vcc, {inputs}, y)"
    if name in {"and", "nand", "or", "nor"}:
        shorthand += f" or {name}(vcc, (a, b, ...), y)"
    return f"{explicit}\nWith ground omitted: {shorthand}"


def gate_accepts(name, input_count):
    if name in {"not", "buf"}:
        return input_count == 1
    if name in {"xor", "xnor"}:
        return input_count == 2
    return input_count >= (1 if name == "nor" else 2)


def gate_body(name, input_count):
    """Return method pins and the component statements for one gate."""
    inputs = tuple("ab"[i] if i < 2 else f"in{i + 1}" for i in range(input_count))
    body = []

    def pullup(stage, out):
        body.extend((
            f"RC{stage}(2.2k) between(vcc, {out})",
            f"CNode{stage}(10p) between({out}, gnd)",
        ))

    def inverting_stage(stage, signals, out, *, series=False):
        pullup(stage, out)
        collector = out
        for i, signal in enumerate(signals, 1):
            base = f"base{stage}_{i}"
            emitter = f"stack{stage}_{i}" if series and i < len(signals) else "gnd"
            body.append(f"RB{stage}_{i}(47k) between({signal}, {base})")
            body.append(f"transistor Q{stage}_{i}({base}, {collector}, {emitter}, npn)")
            if series:
                collector = emitter

    if name in {"xor", "xnor"}:
        a, b = inputs
        inverting_stage(1, inputs, "nand_ab", series=True)
        inverting_stage(2, (a, "nand_ab"), "left_low", series=True)
        inverting_stage(3, (b, "nand_ab"), "right_low", series=True)
        out = "inverted" if name == "xnor" else "y"
        inverting_stage(4, ("left_low", "right_low"), out, series=True)
        if name == "xnor":
            inverting_stage(5, (out,), "y")
    else:
        negate = name in {"and", "or", "buf"}
        out = "inverted" if negate else "y"
        inverting_stage(1, inputs, out, series=name in {"and", "nand"})
        if negate:
            inverting_stage(2, (out,), "y")
    return ("vcc", "gnd", *inputs, "y"), body
