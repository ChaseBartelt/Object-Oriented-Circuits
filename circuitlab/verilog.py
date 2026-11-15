"""Compatibility import for :mod:`circuitlab.exporters.verilog`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.exporters.verilog")

_sys.modules[__name__] = _implementation
