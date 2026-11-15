"""Compatibility import for :mod:`circuitlab.math.quantities`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.math.quantities")

_sys.modules[__name__] = _implementation
