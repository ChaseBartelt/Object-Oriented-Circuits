"""Named schematic probes stored as backward-compatible circuit comments."""

import json

PREFIX = "# @pin "


def pin_entries(source):
    """Yield valid pin comments with their zero-based editor line numbers."""
    for line, text in enumerate(source.split("\n")):
        if not text.startswith(PREFIX):
            continue
        try:
            data = json.loads(text[len(PREFIX) :])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        net, name = data.get("node"), data.get("name")
        if isinstance(net, str) and isinstance(name, str) and net and name.strip():
            yield line, net, name


def read_pins(source):
    return {net: name for _, net, name in pin_entries(source)}


def circuit_text(source):
    """Return the editor text with pin metadata removed for change detection."""
    pin_lines = {line for line, _, _ in pin_entries(source)}
    return "\n".join(
        text for i, text in enumerate(source.split("\n")) if i not in pin_lines
    ).rstrip("\n")


def pin_comment(net, name):
    return PREFIX + json.dumps({"node": net, "name": name}, ensure_ascii=False)
