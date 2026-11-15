"""Compatibility import for :mod:`circuitlab.tooling.cli`."""

from importlib import import_module as _import_module
import sys as _sys

_implementation = _import_module("circuitlab.tooling.cli")

if __name__ == "__main__":
    _sys.stdout.reconfigure(errors="backslashreplace")
    _sys.stderr.reconfigure(errors="backslashreplace")
    raise SystemExit(_implementation.main())

_sys.modules[__name__] = _implementation
