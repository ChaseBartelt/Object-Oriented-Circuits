"""Compatibility import for :mod:`circuitlab.tooling.hover`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.tooling.hover")

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
