"""Installed application paths and post-install validation, independent of Qt."""
# An installed runtime is trusted only after its manifest and contained paths are validated.
# Keep activation separate from staging so an incomplete install cannot replace the active
# version. VS Code integration uses explicit argument lists rather than shell interpolation.

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from circuitlab.language.linked import CHIP_LIBRARY

PRODUCT = "object-oriented-circuits"


def install_root():
    override = os.environ.get("OOC_INSTALL_ROOT")
    return Path(override) if override else Path(os.environ["LOCALAPPDATA"]) / "Programs" / "OOC"


# Resolve symlinks before checking containment so a valid-looking relative name cannot
# redirect an installation entry outside the selected runtime directory.
def contained(root, relative):
    """Only accept paths below the installation, including resolved symlinks."""
    root = Path(root).resolve()
    relative = Path(relative)
    target = (root / relative).resolve()
    if relative.is_absolute() or relative.drive or target == root or not target.is_relative_to(root):
        raise ValueError("Invalid installed application path.")
    return target


def validate_install(root, record):
    if record.get("schema") != 1 or record.get("product") != PRODUCT:
        raise ValueError("This directory is not an OOC installation.")
    if not isinstance(record.get("version"), str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", record["version"]):
        raise ValueError("Invalid installed application version.")
    for field in ("python", "library", "extension", "runtime_bundle"):
        if not contained(root, record[field]).exists():
            raise ValueError(f"Installed {field} is missing. Run OOC Setup to repair it.")
    return record


def read_install(root):
    root = Path(root).resolve()
    return validate_install(root, json.loads((root / "install.json").read_text(encoding="utf-8-sig")))


# Promote only a validated staged installation, preserving the previous active descriptor
# if verification fails before the switch.
def activate(root, manifest):
    root = Path(root).resolve()
    record = validate_install(root, json.loads(Path(manifest).read_text(encoding="utf-8-sig")))
    if Path(sys.executable).resolve() != contained(root, record["python"]):
        raise ValueError("Activate the installation using its own Python runtime.")
    result = self_test()
    if result["version"] != record["version"]:
        raise ValueError("The application package and installer versions differ.")
    if (root / "install.json").exists():
        # A repair must work when old files or the descriptor are damaged. A
        # readable previous version still protects against accidental downgrade.
        try:
            previous = json.loads((root / "install.json").read_text(encoding="utf-8-sig"))
            old_version = previous.get("version", "") if isinstance(previous, dict) else ""
        except (OSError, ValueError):
            old_version = ""
        if isinstance(old_version, str) and re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", old_version) and tuple(map(int, old_version.split("."))) > tuple(map(int, record["version"].split("."))):
            raise ValueError("A newer OOC version is already installed.")
    temporary = root / "install.pending.json"
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temporary, root / "install.json")
    return {"ok": True, "version": record["version"]}


def code_command(code, arguments):
    """Use VS Code's executable CLI entry point, never interpolate a shell command."""
    code = Path(code).resolve()
    cli = code.parent / "resources" / "app" / "out" / "cli.js"
    # New VS Code builds keep resources in a versioned folder. Read the active
    # path from their launcher without executing/interpolating a batch command.
    launcher = code.parent / 'bin/code.cmd'
    if launcher.is_file():
        text = launcher.read_text(encoding='utf-8-sig')
        match = re.search(r'"%~dp0([^"\r\n]+[\\/]resources[\\/]app[\\/]out[\\/]cli\.js)"', text, re.I)
        if match:
            candidate = (launcher.parent / match[1].replace('\\', os.sep)).resolve()
            if candidate.is_relative_to(code.parent) and candidate.is_file():
                cli = candidate
    if not code.is_file() or not cli.is_file():
        raise ValueError("Select a VS Code installation containing Code.exe and its CLI.")
    return [str(code), str(cli), *map(str, arguments)]


# Pass each path as its own argument and record the CLI result for installation diagnostics.
def install_extension(code, vsix, *, user_data=None, extensions_dir=None):
    args = ["--install-extension", str(vsix), "--force"]
    if user_data:
        args += ["--user-data-dir", str(user_data)]
    if extensions_dir:
        args += ["--extensions-dir", str(extensions_dir)]
    env = {**os.environ, "ELECTRON_RUN_AS_NODE": "1", "VSCODE_DEV": ""}
    result = subprocess.run(code_command(code, args), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=180,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "VS Code extension installation failed.")
    return result.stdout.strip()


# Validate the installed payload through its own interpreter rather than assuming the
# development environment can import the same dependencies.
def self_test():
    from circuitlab.tooling.session import CircuitSession
    session = CircuitSession()
    session.set_source("V1(12) between(supply, 0);\nR1(2k) between(supply, out);\nR2(1k) between(out, 0);")
    session.compile()
    if session.issues or session.solution is None:
        raise RuntimeError("The installed compiler could not solve its test circuit.")
    net = session.solution.network
    if abs(net.voltage[net.nodes.index("out")] - 4) > 1e-8:
        raise RuntimeError("The installed solver returned an incorrect test result.")
    if len(list(CHIP_LIBRARY.rglob("*.ckt"))) != 59:
        raise RuntimeError("The installed chip library is incomplete.")
    # Import Qt and the viewer without showing a window during Setup.
    from circuitlab.viewer.window import ViewerWindow
    return {"ok": True, "version": importlib.metadata.version("electric-circuits-calculator"),
            "python": sys.executable, "library": str(CHIP_LIBRARY)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    sub.add_parser("self-test")
    activation = sub.add_parser("activate")
    activation.add_argument("--root", type=Path, required=True)
    activation.add_argument("--manifest", type=Path, required=True)
    extension = sub.add_parser("install-extension")
    extension.add_argument("--code", type=Path, required=True)
    extension.add_argument("--vsix", type=Path, required=True)
    extension.add_argument("--user-data", type=Path)
    extension.add_argument("--extensions-dir", type=Path)
    extension.add_argument("--log", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "info":
            answer = {"python": sys.executable, "library": str(CHIP_LIBRARY),
                      "version": importlib.metadata.version("electric-circuits-calculator")}
        elif args.command == "self-test":
            answer = self_test()
        elif args.command == "activate":
            answer = activate(args.root, args.manifest)
        else:
            answer = {"ok": True, "message": install_extension(args.code, args.vsix,
                       user_data=args.user_data, extensions_dir=args.extensions_dir)}
        print(json.dumps(answer))
        if args.command == 'install-extension' and args.log:
            args.log.write_text(json.dumps(answer,indent=2),encoding='utf-8')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}))
        if args.command == 'install-extension' and args.log:
            args.log.write_text(json.dumps({'error':str(error)},indent=2),encoding='utf-8')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
