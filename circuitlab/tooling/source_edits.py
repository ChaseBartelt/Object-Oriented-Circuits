"""Checked source changes requested by the schematic editor."""
# Schematic edits are plans containing before/after source, not immediate unchecked writes.
# Locate one unambiguous original statement, validate the edited circuit, then let the caller
# commit it through its existing file/IDE acknowledgement and undo machinery.
import re
from dataclasses import dataclass
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.math.numbers import scalar
from circuitlab.language.syntax import UNITS, parse_potentiometer


@dataclass(frozen=True)
class SourceEdit:
    path: Path
    before: str
    after: str
    description: str


PLACED = re.compile(r'(?P<name>[RCLZVIPS]\w*)(?P<value_clause>\s*\((?P<value>[^()\r\n]*)\))?\s+between\s*\((?P<pins>[^()\r\n]+)\)', re.I)
LEGACY = re.compile(r'(?P<name>[RCLZVIS]\w*)[ \t]+(?P<a>\S+)[ \t]+(?P<b>\S+)[ \t]+(?P<value>[^;#\r\n]+)', re.I)
ASSIGN = re.compile(r'(?P<name>[RCLZS]\w*)\s*=\s*(?P<value>[^;#\r\n]*)', re.I)
CALL = re.compile(r'(?P<name>[A-Za-z_]\w*)\s*\((?P<pins>[^()\r\n]*)\)')


# Source lines and instance names identify the original declaration. Reject ambiguous
# generated statements rather than editing a nearby component with a similar name.
def source_target(circuit, name, root, source, read_source):
    """Find one explicit statement; never guess a generated gate/loop body."""
    line, path = circuit.component_lines.get(name), circuit.component_files.get(name, root)
    path = Path(path or root).resolve()
    text = source if path == Path(root).resolve() else read_source(path)
    rows = text.splitlines(keepends=True)
    if not line or not 1 <= line <= len(rows):
        raise ValueError('This generated component has no editable source statement. Edit its definition in the IDE.')
    offset = sum(map(len, rows[:line-1]))
    code = rows[line-1].partition('#')[0]
    key = name.casefold()
    matches = []
    for pattern in (PLACED, LEGACY, ASSIGN, CALL):
        for match in pattern.finditer(code):
            raw = match['name'].casefold()
            if key == raw or key.startswith(raw + '_'):
                matches.append(match)
        if matches:
            break
    if len(matches) != 1:
        raise ValueError('This component is generated or its source is ambiguous. Edit its definition in the IDE.')
    return path, text, offset, matches[0]


# Replace only the value span, then recompile the proposed source with its include reader.
# The returned plan remains reversible and has not written a file yet.
def edit_value(circuit, name, value, root, source, read_source):
    part = next(p for p in circuit.components if p.name == name)
    if part.kind not in {'R', 'C', 'L', 'Z', 'P'}:
        raise ValueError('Value editing is available for resistors, capacitors, inductors, impedances and potentiometers.')
    if value.strip():
        if part.kind == 'P':
            parse_potentiometer(value)
        else:
            scalar(value, UNITS[part.kind])
    if any(c in value for c in ';\r\n#{}'):
        raise ValueError('Enter a component value, such as 1k, 100n, 10m, or 2-j2 for an impedance.')
    path, before, offset, match = source_target(circuit, name, root, source, read_source)
    groups = match.groupdict()
    if 'value' not in groups:
        raise ValueError('Edit the parameter or shared definition that supplies this value in the IDE.')
    replacement = value.strip()
    if 'value_clause' in groups and groups['value_clause'] is None:
        start = end = match.end('name')
        replacement = f'({replacement})' if replacement else ''
    elif 'value_clause' in groups and not replacement:
        start, end = match.span('value_clause')
    elif 'a' in groups and not replacement:
        # Legacy node rows need an explicit placement when their value is removed.
        start, end = match.span()
        replacement = f'{match["name"]} between({match["a"]}, {match["b"]})'
        replacement += match[0][len(match[0].rstrip()):]
    else:
        start, end = match.span('value')
    after = before[:offset+start] + replacement + before[offset+end:]
    def reader(filename):
        return after if Path(filename).resolve() == path else read_source(filename)
    compile_circuit(after if path == Path(root).resolve() else source, mode=circuit.mode,
                    render_only=True, path=Path(root), read_source=reader)
    return SourceEdit(path, before, after, f'Change {match["name"]} value')


# Canonicalize the selected state while preserving terminal names, surrounding whitespace,
# and comments; shared-definition edits continue to affect all of that definition's instances.
def edit_switch(circuit, name, closed, root, source, read_source):
    """Change one manual switch state without changing its connections."""
    part = next(p for p in circuit.components if p.name == name)
    if part.kind != 'K' or not isinstance(closed, bool):
        raise ValueError('Choose an open or closed state for a manual switch.')
    path, before, offset, match = source_target(circuit, name, root, source, read_source)
    groups = match.groupdict()
    if 'value' not in groups:
        raise ValueError('Edit this generated switch in its source definition.')
    replacement = 'closed' if closed else 'open'
    if 'value_clause' in groups and groups['value_clause'] is None:
        start = end = match.end('name')
        replacement = f'({replacement})'
    else:
        start, end = match.span('value')
        raw = match['value']
        start += len(raw) - len(raw.lstrip())
        end -= len(raw) - len(raw.rstrip())
    after = before[:offset+start] + replacement + before[offset+end:]

    def reader(filename):
        return after if Path(filename).resolve() == path else read_source(filename)

    compile_circuit(after if path == Path(root).resolve() else source, mode=circuit.mode,
                    render_only=True, path=Path(root), read_source=reader)
    return SourceEdit(path, before, after, f'{"Close" if closed else "Open"} {match["name"]}')
