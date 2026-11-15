"""Compatibility import for :mod:`circuitlab.layout.gates`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.layout.gates")

_sys.modules[__name__] = _implementation
