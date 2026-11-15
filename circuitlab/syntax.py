"""Compatibility import for :mod:`circuitlab.language.syntax`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.language.syntax")

_sys.modules[__name__] = _implementation
