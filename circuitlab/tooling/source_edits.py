"""Checked source changes requested by the schematic editor."""
import re
from dataclasses import dataclass
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.numbers import scalar
from circuitlab.language.syntax import UNITS


@dataclass(frozen=True)
class SourceEdit:
    path: Path
    before: str
    after: str
    description: str


PLACED = re.compile(r'(?P<name>[RCLVI]\w*)\s*\((?P<value>[^()\r\n]+)\)\s+between\s*\((?P<pins>[^()\r\n]+)\)', re.I)
LEGACY = re.compile(r'(?P<name>[RCLVI]\w*)[ \t]+(?P<a>\S+)[ \t]+(?P<b>\S+)[ \t]+(?P<value>[^;#\r\n]+)', re.I)
ASSIGN = re.compile(r'(?P<name>[RCL]\w*)\s*=\s*(?P<value>[^;#\r\n]+)', re.I)
CALL = re.compile(r'(?P<name>[A-Za-z_]\w*)\s*\((?P<pins>[^()\r\n]*)\)')


def source_target(circuit, name, root, source, read_source):
    """Find one explicit statement; never guess a generated gate/loop body."""
    line, path = circuit.component_lines.get(name), circuit.component_files.get(name, root)
    path = Path(path or root).resolve()
    text = source if path == Path(root).resolve() else read_source(path)
    if not line or not 1 <= line <= len(text.splitlines()):
        raise ValueError('This generated component has no editable source statement. Edit its definition in the IDE.')
    rows = text.splitlines(keepends=True)
    offset = sum(map(len, rows[:line-1]))
    code = rows[line-1].partition('#')[0]
    matches = []
    for pattern in (PLACED, LEGACY, ASSIGN, CALL):
        for match in pattern.finditer(code):
            raw = match['name']
            if name.casefold() == raw.casefold() or name.casefold().startswith(raw.casefold() + '_'):
                matches.append(match)
        if matches:
            break
    if len(matches) != 1:
        raise ValueError('This component is generated or its source is ambiguous. Edit its definition in the IDE.')
    return path, text, offset, matches[0]


def edit_value(circuit, name, value, root, source, read_source):
    part = next(p for p in circuit.components if p.name == name)
    if part.kind not in {'R', 'C', 'L'}:
        raise ValueError('Value editing is available for resistors, capacitors and inductors.')
    scalar(value, UNITS[part.kind])
    if any(c in value for c in ';\r\n#{}'):
        raise ValueError('Enter a component value, such as 1k, 100n, or 10m.')
    path, before, offset, match = source_target(circuit, name, root, source, read_source)
    if 'value' not in match.groupdict():
        raise ValueError('Edit the parameter or shared definition that supplies this value in the IDE.')
    start, end = match.span('value')
    after = before[:offset+start] + value.strip() + before[offset+end:]
    def reader(filename):
        return after if Path(filename).resolve() == path else read_source(filename)
    compile_circuit(after if path == Path(root).resolve() else source,
                    render_only=True, path=Path(root), read_source=reader)
    return SourceEdit(path, before, after, f'Change {match["name"]} value')
