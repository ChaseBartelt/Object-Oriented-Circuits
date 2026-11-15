"""Layout package; retain the historical circuitlab.layout API."""

from .geometry import *  # noqa: F403
from .automatic import *  # noqa: F403
from .legacy import route
from . import geometry as _geometry, automatic as _automatic


def __getattr__(name):
    if hasattr(_geometry, name):
        return getattr(_geometry, name)
    return getattr(_automatic, name)
