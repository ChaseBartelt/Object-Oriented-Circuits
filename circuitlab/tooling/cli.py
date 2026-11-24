"""Check or solve a saved OOC file without starting the graphical editor."""
# The command line shares compilation and analysis with the viewer but does not import Qt.
# Diagnostics include editor-readable file/line locations, and the exit status distinguishes
# a successful result from invalid source or failed numerical analysis.

import argparse
import sys
from pathlib import Path

from circuitlab.math.analysis import analyze, diagnostics
from circuitlab.language.compiler import compile_circuit
from circuitlab.model.graph import CircuitError
from circuitlab.math.report import result_rows
from circuitlab.exporters.verilog import compile_chips
from circuitlab.tooling.session import CircuitSession, issue_for

# Editors may capture terminal output using the Windows code page.
_CONSOLE_SYMBOLS = str.maketrans({
    "→": "->", "∠": "angle", "Ω": "ohm", "µ": "u", "°": " deg",
    "−": "-", "·": "-", "×": "*", "π": "pi", "√": "sqrt",
})


def _diagnostic(path, severity, message, line=None):
    """Print a file diagnostic with a location that editors can recognize."""
    location = f"{path}({line})" if line else str(path)
    code = "OOC001" if severity == "error" else "OOC002"
    text = " ".join(message.splitlines()).translate(_CONSOLE_SYMBOLS)
    print(f"{location}: {severity} {code}: {text}", file=sys.stderr)


# Choose electrical checking/solving or digital validation based on the source structure.
# Keep numerical failure diagnostics distinct from a circuit that checked successfully.
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "solve"))
    parser.add_argument("source", type=Path, help="Path to a saved circuit file.")
    parser.add_argument(
        "--mode", choices=("auto", "dc", "ac", "mixed", "transient"), default="auto",
        help="Electrical analysis mode (default: auto).",
    )
    args = parser.parse_args(argv)
    path = args.source.absolute()
    try:
        source = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        _diagnostic(path, "error", f"Cannot read circuit file: {error}")
        return 1

    try:
        if CircuitSession(path=path, source=source).is_digital():
            if args.command == "solve":
                raise CircuitError("Digital chips use the Verilog compiler. Run check to validate this file.")
            compile_chips(source, path=path)
            print("Check passed (digital chip).")
            return 0

        circuit = compile_circuit(source, args.mode, path=path)
        if args.command == "check":
            print(f"Check passed ({circuit.mode.upper()}).")
            return 0

        result = analyze(circuit)
    except CircuitError as error:
        issue = issue_for(error)
        _diagnostic(issue.path or path, issue.severity, issue.message, issue.line)
        return 1

    failed = False
    for issue in diagnostics(source, result):
        _diagnostic(issue.path or path, issue.severity, issue.message, issue.line)
        failed |= issue.severity == "error"
    print(f"Results ({result.circuit.mode.upper()})")
    for label, _, value in result_rows(result):
        print(f"{label}: {value}".translate(_CONSOLE_SYMBOLS))
    return int(failed)


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
    raise SystemExit(main())
