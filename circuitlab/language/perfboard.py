"""Read perfboard dimensions without adding components or changing their wires."""

import re

from circuitlab.model.graph import CircuitError
from circuitlab.model.perfboard import PerfBoard, row_number


PERF_START = re.compile(r"perf\b", re.I)
PERF = re.compile(
    r"perf\s*\[\s*([A-Za-z]{1,3})\s*-\s*([A-Za-z]{1,3})\s*,\s*"
    r"([0-9]{1,6})\s*-\s*([0-9]{1,6})\s*\]"
    r"(?:\s*exclude\s*\[([^\[\]]*)\])?", re.I,
)
EXCLUDED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_. -]*")
MAX_BOARD_AXIS = 256


def parse_perfboard(text, *, line=None, path=None):
    """Parse one named rectangular grid, using standard 2.54 mm hole pitch."""
    match = PERF.fullmatch(text.removesuffix(";").rstrip())
    if match is None:
        raise CircuitError(
            "Use perf[A-R,1-24] optionally followed by exclude[component, ...].", line, path=path,
        )
    first_row, last_row, first_column, last_column, excluded = match.groups()
    first_row, last_row = first_row.upper(), last_row.upper()
    first_column, last_column = int(first_column), int(last_column)
    if row_number(first_row) > row_number(last_row) or first_column > last_column:
        raise CircuitError("Write perfboard ranges in ascending order, such as perf[A-R,1-24].", line, path=path)
    if first_column < 1:
        raise CircuitError("Perfboard column numbers start at 1 or higher.", line, path=path)
    exclusions = tuple(item.strip() for item in excluded.split(",")) if excluded and excluded.strip() else ()
    if any(not EXCLUDED_NAME.fullmatch(name) for name in exclusions):
        raise CircuitError("List component or chip names in exclude[name, ...].", line, path=path)
    if len({name.casefold() for name in exclusions}) != len(exclusions):
        raise CircuitError("List each excluded perfboard component once.", line, path=path)
    board = PerfBoard(first_row, last_row, first_column, last_column, exclusions, line=line, path=path)
    if max(board.row_count, board.column_count) > MAX_BOARD_AXIS:
        raise CircuitError(f"A perfboard can have at most {MAX_BOARD_AXIS} rows and {MAX_BOARD_AXIS} columns.", line, path=path)
    return board
