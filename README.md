# Object-Oriented Circuits

OOC is a .ckt circuit language with a compiler, schematic viewer and simulation tools. Write in VS Code, save to refresh the viewer, and keep connections in source. Electrical analysis and digital FPGA compilation are separate workflows.

## Quick start

Installed Windows releases bundle Python, dependencies, the viewer, chip library and VS Code extension. VS Code itself is installed separately.

For source development, use Python 3.10 or newer from the checkout root:

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e .
    .\.venv\Scripts\python.exe ide/vscode/package_extension.py ide/vscode/dist/ooc-language.vsix

Install the generated VSIX from ide/vscode/dist using **Extensions: Install from VSIX**. Select the checkout's Python interpreter if needed. Open your own .ckt file and use **OOC: Run Circuit / Open Schematic** or the editor play button. Save source to update the same viewer. Restart the viewer after changing Python implementation files.

Headless commands and the optional desktop editor:

    .\.venv\Scripts\python.exe -m circuitlab.tooling.cli check path\to\circuit.ckt
    .\.venv\Scripts\python.exe -m circuitlab.tooling.cli solve path\to\circuit.ckt
    .\.venv\Scripts\python.exe -m circuitlab path\to\circuit.ckt
    .\.venv\Scripts\python.exe -m circuitlab --editor

On macOS/Linux use python3 and .venv/bin/python. Solve is for electrical designs; digital designs use Check, Logic simulation or Verilog export. This checkout contains no demonstration projects. Keep your circuits outside the application source tree.

## Daily workflow

- **Electrical circuits:** compile/solve, inspect results or scope waveforms, and export SPICE/LTspice. Chip models are approximations; seven catalog devices remain pinout only.
- **Digital circuits:** use Logic simulation to toggle inputs, step/run clocked behavior and inspect signals. This simulates ideal logic, not a connected board's measurements.
- **Schematics:** move/rotate parts and wires, open chip internals, and save layout. Layout metadata is embedded in .ckt comments; legacy sidecars still load for migration. Right-click supported components to edit values through the source bridge.
- **Chip library:** include("74HC00"); resolves a bundled definition. Chip sources are in circuitlab/libraries/chips/; shared drawing defaults are in circuitlab/libraries/layouts/.
- **Updates:** installed users use **OOC: Check for Updates**. Developers rebuild the VSIX after extension changes; compatible runtime updates can apply live, while registration/loader changes require reload.

## FPGA

The viewer's **FPGA...** window supports the DE10-Lite HYBRID01 profile: 50 MHz clock, SW0-SW9, LEDR0-LEDR9 and optional AUDIO_OUT. The profile can add two-stage switch synchronization; it is not debouncing. Install Quartus with MAX 10 support and the USB-Blaster driver separately.

Open a saved digital design, **Build from OOC**, inspect **Performance**, select the cable, then **Program FPGA**. A verified build is required. Programming uses volatile SRAM (.sof), not power-on flash. Fmax is a fitted estimate, not the running clock. Electrical transistor/op-amp circuits cannot compile directly to FPGA logic.

## Where to change the implementation

| Folder | Responsibility |
| --- | --- |
| circuitlab/language | Syntax, parameters, includes, generation and compilation |
| circuitlab/model | Electrical graph and component data |
| circuitlab/math | Equations, linear/nonlinear solvers and digital simulation |
| circuitlab/layout | Placement, routing, geometry and saved/shared layouts |
| circuitlab/viewer | Windows, schematic interaction, scope and FPGA UI |
| circuitlab/exporters | SPICE, LTspice and Verilog lowering |
| circuitlab/tooling | CLI, IDE bridge, source edits, updates and FPGA verification |
| circuitlab/libraries | Bundled chips and drawing defaults |
| ide/vscode | VS Code extension, syntax grammar and snippets |
| installer/windows | Installer and release build tooling |
| tests | Regression tests and fixed inputs |

Implement directly in these packages. Preserve source locations, instance names and net identities. Geometry edits must preserve connectivity. Keep electrical units separate from digital bit widths. Check behavior and fitted resources when optimizing HDL.

## Checks and packaging

    $env:QT_QPA_PLATFORM = 'offscreen'
    .\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
    $env:OOC_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
    node --test ide/vscode/test/*.test.js

Tests are grouped by the area to work on:

| Test folder | Coverage |
| --- | --- |
| tests/language | Circuit methods, control flow, includes and digital assembly |
| tests/simulation | Analog/digital solvers, BJTs, impedances, sources and MATLAB parity |
| tests/layout | Routing, grid alignment, connections and saved/shared layouts |
| tests/viewer | Schematic interaction, windows, logic simulation UI and FPGA panels |
| tests/exporters | SPICE, LTspice, Verilog and clocked logic generation |
| tests/tooling | CLI, IDE bridge, source editing, FPGA builds, packaging and updates |
| tests/libraries | Bundled chip definitions and imports |
| tests/integration | Architecture boundaries and behavior across subsystems |
| tests/fixtures | Shared circuit inputs, saved layouts, Quartus reports and MATLAB references |

Run one area or one module from the checkout root:

    .\.venv\Scripts\python.exe -m unittest discover -s tests/layout -t . -v
    .\.venv\Scripts\python.exe -m unittest tests.tooling.test_fpga -v

Use -t . with discovery so imports consistently start at the repository root. If a combined Qt run hangs during teardown, run modules in separate processes and report that distinction. Fixtures are regression inputs, not user projects; keep expected values independent of the implementation. JavaScript extension tests remain in ide/vscode/test and use the Node command above.

See [extension packaging](ide/vscode/README.md) and [Windows installer builds](installer/windows/README.md). Retain license and dependency metadata in distributions.
