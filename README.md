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

On macOS/Linux use python3 and .venv/bin/python. Solve is for electrical designs; digital designs use Check, Logic simulation or Verilog export. Test designs are in examples/advanced. Keep your own circuits outside the application source tree.

## Daily workflow

Manual switches use `S1(open) between(a, b);` or `S1(closed) between(a, b);`. Click the switch symbol to toggle it, update its source state, and recalculate; **Undo** restores the previous state. `on`/`off` and `1`/`0` also work, and an omitted state defaults to open. Inside a series or parallel block, use `S1 = open;`. The solver and exports model the selected state as 1 mΩ closed or 1 TΩ open.

Inside a battery block, `Vneg = 5;` declares a −5 V supply on its first wire relative to its return. `Vneg = -5;` gives the same result. Use either `V` or `Vneg` once per battery block.

Op amps accept `op1(in_plus, in_minus, out);` or `op1(in_plus, in_minus, out, vplus, vminus);`. The optional fourth and fifth wires add positive and negative supply pins above and below the triangle. They identify the power connections; the op-amp model remains ideal and does not simulate supply current or rail clipping.

- **Electrical circuits:** compile/solve, inspect results or scope waveforms, and export SPICE/LTspice. Chip models are approximations; seven catalog devices remain pinout only.
- **Digital circuits:** use Logic simulation to toggle inputs, step/run clocked behavior and inspect signals. This simulates ideal logic, not a connected board's measurements.
- **Schematics:** move/rotate parts and wires, open chip internals, and save layout. Layout metadata is embedded in .ckt comments; legacy sidecars still load for migration. Right-click supported components to edit values through the source bridge.
- **Chip library:** include("74HC00"); resolves a bundled definition. Chip sources are in circuitlab/libraries/chips/; shared drawing defaults are in circuitlab/libraries/layouts/.
- **Updates:** installed users use **OOC: Check for Updates**. Developers rebuild the VSIX after extension changes; compatible runtime updates can apply live, while registration/loader changes require reload.

Potentiometers use `P1(10k, 25%) between(high, wiper, low);`. The first value is total resistance; the position runs from 0% at `low` to 100% at `high`. Omitting position gives 50%. Click the potentiometer to open its position slider: dragging previews the electrical result, and releasing updates the code in one undoable edit. Right-click **Edit value** to change resistance and position together; leaving the resistance unspecified creates a schematic placeholder. The native model is an ideal linear potentiometer, exported to SPICE/LTspice as two resistor legs (an exact short at either endpoint). Try [the potentiometer example](examples/potentiometer.ckt).

The **Schematic** tab has a resizable oscilloscope beside the drawing. Choose its node and click **Run** to watch the waveform while adjusting a potentiometer; **View → Oscilloscope** hides or restores the panel. Value and time-step adjustments preserve elapsed time, capacitor voltages, and inductor currents. Changing the monitored node displays that signal's recorded history without restarting the circuit. AC and DC+AC waveforms use the configured frequency and phase, converting RMS amplitudes to peak amplitudes. **Pause/Resume** preserves the simulation; **Reset** starts it over and applies the initial-condition controls. Changing the circuit's connections requires a new run. Theoretical `Z` blocks still require physical R/L/C equivalents for time simulation.

**Optimize wiring** can compact empty space and move components as well as shorten wires. It checks electrical connections and grid alignment before accepting changes; **Ctrl+Z** restores the previous drawing. Saved layouts keep their positions until you explicitly optimize or move them.

## FPGA

The viewer's **FPGA...** window supports the DE10-Lite HYBRID01 profile: 50 MHz clock, SW0-SW9, LEDR0-LEDR9, HEX0-HEX5 seven-segment displays and optional AUDIO_OUT. The profile can add two-stage switch synchronization; it is not debouncing. Install Quartus with MAX 10 support and the USB-Blaster driver separately.

Open a saved digital design, **Build from OOC**, inspect **Performance**, select the cable, then **Program FPGA**. A verified build is required. Programming uses volatile SRAM (.sof), not power-on flash. Fmax is a fitted estimate, not the running clock. Electrical transistor/op-amp circuits cannot compile directly to FPGA logic.

Digital chip behavior supports bounded `for` loops, bit/slice assignments (`LEDR[7:0] = elapsed[7:0];`), ternary expressions (`fsm = SW1 ? EARLY : WAITING;`), and `switch` with `case`, `break`, and `default`, including inside clock blocks. Loops expand at compile time; they do not consume one clock cycle per iteration. Indexed pin banks such as `LEDR[i]` can refer to separately declared `LEDR0`…`LEDR9` pins. Switches support stacked labels and fallthrough; put `break` directly at the end of a case, since conditional breaks are rejected.

Headers and declarations accept numbered pin ranges: `chip HYBRID01(SW[9:0], LEDR[9:0]) { SW[9:0] = input; LEDR[9:0] = output; LEDR[9:0] = SW[9:0]; }`. A range expands in written order (`SW9, SW8, ... SW0`) into individual scalar pins, retaining the board pin assignments. Ranges are inclusive and descending, with indices from 0 to 4095. The existing `data[10] = input;` syntax still declares one ten-bit vector named `data`.

Inside a chip, `requests[8] = SW[7:0];` declares a new eight-bit combinational wire and its expression in one statement. The expression continuously drives the wire; no register is added. Arithmetic and ternary expressions work too. Bare declarations (`phase[32];`) and decimal literal initializers (`phase[32] = 0;`) retain their stored-state meaning. Once a signal is declared, `requests[3]` selects bit 3 instead of declaring a width.

Unsigned vectors support whole-bus `&`, `|`, `^`, `+`, `-`, `<<` and `>>`. Shifts zero-fill and keep the left operand's width: an eight-bit `value << 1` discards its old top bit, even when assigned to a wider output. A shift by the width or more produces zero. Addition binds more tightly than shifts, which bind more tightly than comparisons. Clocked state updates support `+=`, `-=`, `&=`, `|=`, `^=`, `<<=` and `>>=`, including constant bit/slice targets; each reads the previous clock-edge state. `!` remains logical NOT (a one-bit result); use `value ^ 255` to invert every bit of an eight-bit vector.

Declare a display bank with `HEX0[7:0] = output;` and drive it using `HEX0[7:0] = sevenseg(value);`. The helper decodes the low four bits as hexadecimal 0–F. `sevenseg(value, point)` lights the decimal point when `point` is nonzero; it is off by default. Bits 0–6 are segments a–g, bit 7 is the point, and zero lights a segment. Raw masks also work (`255` blanks a digit). Include each used bank in the chip header. The logic simulator shows all six digit positions above its schematic; click a segment to probe its signal. [Seven-segment and shift example](examples/advanced/fpga/07_seven_segment_display.ckt) shows these features together on the DE10-Lite without a clock.

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
    Get-ChildItem ide/vscode -Filter *.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed" } }

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

Use -t . with discovery so imports consistently start at the repository root. If a combined Qt run hangs during teardown, run modules in separate processes and report that distinction. Fixtures are regression inputs, not user projects; keep expected values independent of the implementation. Extension packaging and grammar tests are in tests/tooling; the Node command checks JavaScript syntax.

For a complete Windows installer, run the **Windows installer** workflow in GitHub Actions, or build locally with pip/setuptools and Inno Setup:

    .\.venv\Scripts\python.exe -m pip install setuptools
    .\.venv\Scripts\python.exe installer/windows/build_installer.py --iscc "C:\path\to\ISCC.exe"

The build downloads checksum-verified dependencies and writes the installer to dist/windows. Optional draft releases use GitHub-generated notes. See [extension packaging](ide/vscode/README.md); retain license and dependency metadata in distributions.
