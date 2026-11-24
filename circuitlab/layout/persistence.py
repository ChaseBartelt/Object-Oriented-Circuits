"""Save schematic geometry in source comments, with legacy sidecar support."""
# Only complete, validated managed comment blocks may be replaced during a save.
# Executable circuit text and unrelated comments remain authoritative; stored geometry is
# rebound to current component identities and checked before being applied.

import json
import math
import os
from dataclasses import replace
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import time

from circuitlab.layout.geometry import Layout
from circuitlab.layout.board_layers import remap_wire_layers, wire_subset
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.checks import mark_crossings as _mark_crossings
from circuitlab.layout.wire_edit import replace_route


def placement_path(path):
    return path.with_name(path.name + ".layout.json")


_BEGIN = "# @ooc-layout-begin"
_END = "# @ooc-layout-end"
_MARKER = re.compile(r"^[ \t]*#[ \t]*@ooc-layout-(begin|end)([^\r\n]*)", re.M)
_MODES = {"auto", "routed", "labeled"}


# Malformed or duplicate managed markers are ambiguous; refuse to guess which region
# should be removed or overwritten.
def _block(source):
    """Locate one complete managed comment block; never guess at broken data."""
    markers = list(_MARKER.finditer(source))
    if not markers:
        return None
    if (len(markers) != 2 or markers[0][1] != "begin" or markers[1][1] != "end"
            or markers[0][2].strip() not in ("", "no-final-newline", "padding=1", "padding=2", "padding=3")
            or markers[1][2].strip()):
        raise ValueError("The embedded layout comments are incomplete or duplicated. Repair the layout block before saving.")
    begin, end = markers
    start, finish = begin.start(), end.end()
    if source[finish:finish+2] == "\r\n":
        finish += 2
    elif source[finish:finish+1] in ("\r", "\n"):
        finish += 1
    option = begin[2].strip()
    padding = 1 if option == "no-final-newline" else int(option[-1]) if option.startswith("padding=") else 0
    for _ in range(padding):
        if source[max(0, start-2):start] == "\r\n":
            start -= 2
        elif start and source[start-1] in "\r\n":
            start -= 1
        else:
            raise ValueError("The embedded layout separator is missing. Repair the layout block before saving.")
    payload = []
    for row in source[begin.end():end.start()].splitlines():
        row = row.lstrip()
        if not row:
            continue
        if not row.startswith("#"):
            raise ValueError("The embedded layout block contains circuit code; it cannot be replaced safely.")
        payload.append(row[1:].lstrip())
    return start, finish, "\n".join(payload)


def _valid_payload(data):
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("mode"), str) or data["mode"] not in _MODES):
        return False
    rows = data.get("layouts")
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != 3
           or not (row[0] is None or isinstance(row[0], str))
           or not isinstance(row[1], str) or row[1] not in _MODES or not isinstance(row[2], dict) for row in rows):
        return False
    pins = data.get("pin_overrides", {})
    return isinstance(pins, dict) and all(
        isinstance(net, str) and net and (label is None or isinstance(label, str) and label.strip())
        for net, label in pins.items()
    )


def _decode_payload(text):
    def invalid_constant(value):
        raise ValueError(f"Non-finite layout value: {value}")
    data = json.loads(text, parse_constant=invalid_constant)
    if not _valid_payload(data):
        raise ValueError("Unsupported or damaged schematic layout data.")
    return data


def layout_payload(source):
    """Read a valid embedded payload, or None when absent/unreadable."""
    try:
        block = _block(source)
        return _decode_payload(block[2]) if block else None
    except (ValueError, TypeError):
        return None


def strip_placement(source):
    """Remove only a complete, valid layout block; preserve all other text."""
    block = _block(source)
    if block is None:
        return source
    start, end, payload = block
    _decode_payload(payload)
    return source[:start] + source[end:]


def _embed_payload(source, data):
    code = strip_placement(source)
    newline = "\r\n" if "\r\n" in code else "\n"
    trailing = re.search(r"(?:(?:\r\n|\r|\n)[ \t]*)*$", code)[0]
    # Three line endings after code leave two blank lines before the block.
    # Track only inserted separators so stripping restores the exact source.
    padding = max(0, 3 - len(re.findall(r"\r\n|\r|\n", trailing))) if code else 0
    separator = newline * padding
    marker = _BEGIN + (f" padding={padding}" if padding else "")
    # Keep the payload compact inside a clearly delimited, foldable block.
    lines = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return code + separator + marker + newline + "# " + lines + newline + _END + newline


# Replace only recognized metadata, preserving the surrounding executable source text.
def embed_placement(source, mode, records, *, pin_overrides=None, logic_layouts=None):
    """Replace the managed comment block without touching executable source."""
    if logic_layouts is None:
        logic_layouts = (layout_payload(source) or {}).get('logic')
    data = _decode_payload(write_placement(mode, records, pin_overrides=pin_overrides,
                                          logic_layouts=logic_layouts))
    return _embed_payload(source, data)


def _read_payload(path):
    path = Path(path)
    try:
        embedded = layout_payload(path.read_bytes().decode("utf-8-sig"))
        if embedded is not None:
            return embedded
    except (OSError, UnicodeError):
        pass
    try:
        return _decode_payload(placement_path(path).read_bytes())
    except (OSError, ValueError, TypeError):
        return None


def read_placement(path):
    data = _read_payload(path)
    if data is None:
        return "auto", {}
    return data["mode"], {(level, mode): layout for level, mode, layout in data["layouts"]}


def read_pin_overrides(path):
    """Read optional viewer probe captions from comments or an old sidecar.

    A null label hides a source-defined probe. Absent entries continue to use
    existing # @pin comments; old version-1 sidecars remain valid.
    """
    data = _read_payload(path)
    return dict(data.get("pin_overrides", {})) if data else {}


def read_logic_placement(path):
    """Read optional digital geometry without invalidating older analog layouts."""
    data = (_read_payload(path) or {}).get('logic', {})
    return data if isinstance(data, dict) else {}


# Migrate a valid old sidecar into the source once, retaining the sidecar as a backup
# rather than discarding the only copy of a layout.
def migrate_placement(path):
    """Embed a valid sidecar once, keeping the original JSON as a backup."""
    path = Path(path)
    original = path.read_bytes()
    source = original.decode("utf-8-sig")
    block = _block(source)
    if block is not None:
        _decode_payload(block[2])
        return False
    sidecar = placement_path(path)
    if not sidecar.exists():
        return False
    data = _decode_payload(sidecar.read_bytes())
    embedded = _embed_payload(source, data)
    if strip_placement(embedded) != source or layout_payload(embedded) != data:
        raise ValueError("Layout migration could not preserve the source and saved layout.")
    encoding = "utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8"
    encoded = embedded.encode(encoding)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows file scanners can briefly lock a just-written file. Retry
        # only that error, rechecking source each time rather than losing edits.
        for attempt in range(3):
            if path.read_bytes() != original:
                raise ValueError("The circuit changed during layout migration; no source was overwritten.")
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(.025 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if path.read_bytes() != encoded:
        raise OSError("The migrated circuit could not be verified; the original layout backup was kept.")
    return True


def write_placement(mode, records, *, pin_overrides=None, logic_layouts=None):
    data = {
        "version": 1,
        "mode": mode,
        "layouts": [[level, style, layout] for (level, style), layout in records.items()],
    }
    if pin_overrides:
        data["pin_overrides"] = dict(pin_overrides)
    if isinstance(logic_layouts, dict) and logic_layouts:
        data['logic'] = logic_layouts
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


# Serialize component identities and contacts with positions so restore can detect a
# layout that belongs to a different version of the electrical circuit.
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
        **({"pin_grid": layout.pin_grid} if layout.pin_grid else {}),
        **({"compact_labels": sorted(layout.compact_labels)} if layout.compact_labels else {}),
        **({"underside_wires": layout.underside_wires} if layout.underside_wires else {}),
        **({"wires_visible": False,
            "wiring_positions": [[name, a, b, turns]
                                 for name, (a, b, turns) in sorted(layout.wiring_positions.items())]}
           if not layout.wires_visible else {}),
    }


# Bind records to current parts and verify contacts before returning a usable drawing.
# A structurally valid JSON object is insufficient if its electrical wiring no longer agrees.
def restore_layout(record, circuit, boundary, *, editable_perfboard=False):
    """Bind saved coordinates to the current parts and verify their wiring."""
    def point(position):
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise ValueError("Invalid schematic coordinate")
        x, y = position
        if not all(type(value) in (int, float) and math.isfinite(value) for value in (x, y)):
            raise ValueError("Invalid schematic coordinate")
        return x, y

    def rotation(turns):
        if type(turns) is not int or turns not in range(4):
            raise ValueError("Invalid schematic rotation")
        return turns

    def finite(value):
        if not math.isfinite(value):
            raise ValueError("Invalid crossing coordinate")
        return value

    try:
        wires_visible = record.get("wires_visible", True)
        if type(wires_visible) is not bool:
            return None
        grid = record.get("pin_grid", 0)
        if not isinstance(grid, (int, float)) or not math.isfinite(grid) or not 0 <= grid <= 16:
            return None
        parts = {part.name: part for part in circuit.components}
        compact_labels = record.get("compact_labels", [])
        if (not isinstance(compact_labels, list)
                or any(not isinstance(name, str) or name not in parts for name in compact_labels)):
            return None
        symbols = []
        for name, kind, nets, a, b, top in record["symbols"]:
            part = parts[name]
            if part.kind != kind or part.terminal_nets != tuple(nets) or top not in {"+", "-"}:
                return None
            a, b = point(a), point(b)
            if a == b:
                return None
            symbols.append((part, a, b, top))
        wiring_rows = record.get("wiring_positions", [])
        if not isinstance(wiring_rows, list):
            return None
        wiring_positions = {}
        for row in wiring_rows:
            if not isinstance(row, list) or len(row) != 4:
                return None
            name, a, b, turns = row
            if not isinstance(name, str) or name not in parts or name in wiring_positions:
                return None
            a, b = point(a), point(b)
            if a == b:
                return None
            wiring_positions[name] = (a, b, rotation(turns))
        if not wires_visible and (
                len(symbols) != len(parts)
                or {part.name for part, *_ in symbols} != parts.keys()
                or wiring_positions.keys() != parts.keys()):
            return None
        wires = {net: [(point(a), point(b)) for a, b in edges]
                 for net, edges in record["wires"].items()}
        underside_rows = record.get("underside_wires", {})
        if not isinstance(underside_rows, dict):
            return None
        known_nets = {net for part in parts.values() for net in part.terminal_nets} | set(boundary)
        underside_wires = {}
        for net, rows in underside_rows.items():
            if not isinstance(net, str) or net not in known_nets or net not in wires or not isinstance(rows, list):
                return None
            edges, seen = [], set()
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    return None
                a, b = point(row[0]), point(row[1])
                edge = tuple(sorted((a, b)))
                if a == b or edge in seen:
                    return None
                seen.add(edge)
                edges.append(edge)
            if not wire_subset(edges, wires[net]):
                return None
            if edges:
                underside_wires[net] = edges
        drawing = Layout(
            pin_grid=grid,
            compact_labels=frozenset(compact_labels),
            wires_visible=wires_visible,
            wiring_positions=wiring_positions,
            underside_wires=underside_wires,
            symbols=symbols,
            positions={part.name: a for part, a, _, _ in symbols},
            rotations={name: rotation(turns) for name, turns in record["rotations"].items()},
            grounds=[point(p) for p in record["grounds"]],
            ground_names={point(p): net for p, net in record["ground_names"]},
            ground_rotations={point(p): rotation(turns) for p, turns in record["ground_rotations"]},
            connectors=[(net, point(p), name, bool(left), bool(below))
                        for net, p, name, left, below in record["connectors"]],
            wires=wires,
            junctions={net: [point(p) for p in positions]
                       for net, positions in record["junctions"].items()},
            labels={net: point(p) for net, p in record["labels"].items()},
            boundary={net: point(p) for net, p in record["boundary"].items()},
            crossings={(net, point(a), point(b)): [finite(cut) for cut in cuts]
                       for net, a, b, cuts in record["crossings"]},
            label_offsets={
                tuple(key): point(offset) for key, offset in record.get("label_offsets", ())
                if isinstance(key, (list, tuple)) and len(key) >= 2 and isinstance(key[0], str)
            },
        )
        if drawing.boundary.keys() <= set(boundary):
            if not wires_visible:
                # The retained routes belong to the saved wiring positions;
                # placement mode deliberately lets the current pins move away.
                return drawing
            before_repair = (
                replace(drawing, wires={net: list(edges) for net, edges in drawing.wires.items()})
                if drawing.underside_wires else None
            )
            _restore_ground_boundaries(drawing, circuit.grounds)
            if before_repair is not None:
                drawing = remap_wire_layers(before_repair, drawing)
            if (editable_perfboard and circuit.perfboard is not None and grid == 1
                    and len(symbols) == len(parts)
                    and {part.name for part, *_ in symbols} == parts.keys()):
                # Retain unfinished board placements so users can reopen and
                # repair them. The viewer revalidates their fit and routing.
                return drawing
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
