"""Compatibility import for :mod:`circuitlab.tooling.editor_bridge`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.tooling.editor_bridge")

_sys.modules[__name__] = _implementation
