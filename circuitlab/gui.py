"""Compatibility import for :mod:`circuitlab.viewer.legacy`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.viewer.legacy")

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
