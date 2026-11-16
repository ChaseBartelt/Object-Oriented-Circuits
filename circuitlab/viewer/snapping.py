"""Screen-sized grid and alignment rules for schematic dragging."""

import math

GRID_STEP = 0.25
SNAP_PIXELS = 8
SNAP_RELEASE_PIXELS = 11
MIN_GRID_PIXELS = 4


def grid_step(pixels_per_unit):
    """Keep grid rows usable when zoomed out, without drawing a dense mesh."""
    step = GRID_STEP
    while step * max(pixels_per_unit, 0.01) < MIN_GRID_PIXELS:
        step *= 2
    return step


def grid_delta(anchor, delta, step):
    """Snap the moving anchor, preserving the pointer's grab offset."""
    return tuple(round(math.floor((a + d) / step + 0.5) * step - a, 8)
                 for a, d in zip(anchor, delta))


def on_grid(value, step):
    return abs(value / step - round(value / step)) < 1e-6


def snap_radius(pixels_per_unit, step):
    """A magnet cannot hold a part across an adjacent grid row or column."""
    return (min(SNAP_PIXELS / pixels_per_unit, step * 0.4),
            min(SNAP_RELEASE_PIXELS / pixels_per_unit, step * 0.6))
