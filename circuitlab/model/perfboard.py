"""Physical hole-grid metadata kept separate from electrical connectivity."""

from dataclasses import dataclass, field


def row_number(label):
    """Convert a spreadsheet-style row label to a zero-based hole-grid index."""
    number = 0
    for letter in label.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number - 1


def row_label(number):
    """Convert a zero-based hole-grid index back to its row label."""
    result = ""
    number += 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


@dataclass(frozen=True)
class PerfBoard:
    """Board bounds and off-board component names; no electrical nets change."""

    row_start: str = "A"
    row_end: str = "R"
    column_start: int = 1
    column_end: int = 24
    exclusions: tuple[str, ...] = ()
    pitch_mm: float = 2.54
    line: int | None = field(default=None, compare=False)
    path: object = field(default=None, compare=False)

    @property
    def row_count(self):
        return row_number(self.row_end) - row_number(self.row_start) + 1

    @property
    def column_count(self):
        return self.column_end - self.column_start + 1

    @property
    def rows(self):
        return tuple(row_label(number) for number in range(row_number(self.row_start), row_number(self.row_end) + 1))

    @property
    def columns(self):
        return tuple(range(self.column_start, self.column_end + 1))
