"""Compatibility import for :mod:`circuitlab.viewer.editor`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.viewer.editor")

_sys.modules[__name__] = _implementation
