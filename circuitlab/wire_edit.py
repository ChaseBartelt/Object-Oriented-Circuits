"""Compatibility import for :mod:`circuitlab.layout.wire_edit`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.layout.wire_edit")

_sys.modules[__name__] = _implementation
