"""Layout package; preserve the former circuitlab.layout API."""

from .geometry import *  # noqa: F403
from . import geometry as _geometry

def __getattr__(name):
    return getattr(_geometry, name)
