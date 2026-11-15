"""Compatibility import for :mod:`circuitlab.viewer.schematic`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.viewer.schematic")

_sys.modules[__name__] = _implementation
