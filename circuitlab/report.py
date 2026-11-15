"""Compatibility import for :mod:`circuitlab.math.report`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.math.report")

_sys.modules[__name__] = _implementation
