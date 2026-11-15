"""Save schematic geometry separately from the circuit source."""

import json
import math

from circuitlab.layout.geometry import Layout
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.routing import _mark_crossings
from circuitlab.layout.wire_edit import replace_route


def placement_path(path):
    return path.with_name(path.name + ".layout.json")


def read_placement(path):
    try:
        data = json.loads(placement_path(path).read_bytes())
        if data["version"] == 1 and isinstance(data["layouts"], list):
            records = {
                (level, mode): layout for level, mode, layout in data["layouts"]
                if (level is None or isinstance(level, str))
                and mode in ("auto", "routed", "labeled") and isinstance(layout, dict)
            }
            return data["mode"], records
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return "auto", {}


def read_pin_overrides(path):
    """Read optional viewer probe captions without changing circuit source.

    A null label hides a source-defined probe. Absent entries continue to use
    existing # @pin comments; old version-1 sidecars remain valid.
    """
    try:
        data = json.loads(placement_path(path).read_bytes())
        if data.get("version") == 1 and isinstance(data.get("pin_overrides"), dict):
            return {
                net: label for net, label in data["pin_overrides"].items()
                if isinstance(net, str) and net
                and (label is None or isinstance(label, str) and label.strip())
            }
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def write_placement(mode, records, *, pin_overrides=None):
    data = {
        "version": 1,
        "mode": mode,
        "layouts": [[level, style, layout] for (level, style), layout in records.items()],
    }
    if pin_overrides:
        data["pin_overrides"] = dict(pin_overrides)
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def pack_layout(layout):
    return {
        "symbols": [[part.name, part.kind, list(part.terminal_nets), a, b, top]
                    for part, a, b, top in layout.symbols],
        "wires": layout.wires,
        "junctions": layout.junctions,
        "labels": layout.labels,
        "grounds": layout.grounds,
        "ground_names": list(layout.ground_names.items()),
        "ground_rotations": list(layout.ground_rotations.items()),
        "connectors": layout.connectors,
        "boundary": layout.boundary,
        "rotations": layout.rotations,
        "crossings": [[net, a, b, cuts] for (net, a, b), cuts in layout.crossings.items()],
        "label_offsets": list(layout.label_offsets.items()),
    }


def restore_layout(record, circuit, boundary):
    """Bind saved coordinates to the current parts and verify their wiring."""
    def point(position):
        x, y = position
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Invalid schematic coordinate")
        return x, y

    def points_map(values):
        return {net: point(p) for net, p in values.items()}

    def rotation(turns):
        if type(turns) is not int or turns not in range(4):
            raise ValueError("Invalid schematic rotation")
        return turns

    def finite(value):
        if not math.isfinite(value):
            raise ValueError("Invalid crossing coordinate")
        return value

    try:
        parts = {part.name: part for part in circuit.components}
        symbols = []
        for name, kind, nets, a, b, top in record["symbols"]:
            part = parts[name]
            if part.kind != kind or part.terminal_nets != tuple(nets) or top not in {"+", "-"}:
                return None
            a, b = point(a), point(b)
            if a == b:
                return None
            symbols.append((part, a, b, top))
        drawing = Layout(
            symbols=symbols,
            positions={part.name: a for part, a, _, _ in symbols},
            rotations={name: rotation(turns) for name, turns in record["rotations"].items()},
            grounds=[point(p) for p in record["grounds"]],
            ground_names={point(p): net for p, net in record["ground_names"]},
            ground_rotations={point(p): rotation(turns) for p, turns in record["ground_rotations"]},
            connectors=[(net, point(p), name, bool(left), bool(below))
                        for net, p, name, left, below in record["connectors"]],
            wires={net: [(point(a), point(b)) for a, b in edges]
                   for net, edges in record["wires"].items()},
            junctions={net: [point(p) for p in positions]
                       for net, positions in record["junctions"].items()},
            labels=points_map(record["labels"]),
            boundary=points_map(record["boundary"]),
            crossings={(net, point(a), point(b)): [finite(cut) for cut in cuts]
                       for net, a, b, cuts in record["crossings"]},
            label_offsets={
                tuple(key): point(offset) for key, offset in record.get("label_offsets", ())
                if isinstance(key, (list, tuple)) and len(key) >= 2 and isinstance(key[0], str)
            },
        )
        if drawing.boundary.keys() <= set(boundary):
            _restore_ground_boundaries(drawing, circuit.grounds)
            if preserves_nets(circuit, drawing):
                return drawing
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def _restore_ground_boundaries(drawing, ground_nets):
    """Remove old leads kept alive by a ground port left at a previous position."""
    repaired = False
    for net, position in drawing.boundary.items():
        if net not in ground_nets or position in drawing.grounds:
            continue
        nearby = min(
            (p for p in drawing.grounds if drawing.ground_names.get(p, "0") == net),
            key=lambda p: math.dist(position, p),
            default=None,
        )
        if nearby is not None:
            drawing.boundary[net] = nearby
            replace_route(drawing, net, drawing.wires.get(net, ()))
            repaired = True
    if repaired:
        drawing.crossings.clear()
        _mark_crossings(drawing)
