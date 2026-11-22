# OOC 1.1.5

Application: **1.1.5**. VS Code extension: **0.8.15**.

## Changes

- Add two-terminal diodes: `D1`, `D.rect1`, `D.Tun1`, `D.Schot1`, `D.Zener1`, `D.LED1`, plus PIN, varactor, photo and avalanche presets. Connect them with `between(anode, cathode)`. Defaults need no parentheses; parentheses carry optional voltage/model parameters.
- Add generic diode DC/transient models, junction charge, tunnel negative resistance, oriented schematic symbols, source value editing and self-contained SPICE/LTspice export. Include the runnable `examples/diodes.ckt`.
- Correct diode body clearance so the existing short resistor leads continue to apply in mixed resistor/diode drawings. Diode terminal spans are compact too.

- Add the OOC Cortex-M7 instruction model, visible CPU hierarchy, system registers, interrupts, fault handling and floating-point/DSP operations, with firmware and FreeRTOS demos.
- Add STM32H750/Daisy Seed boot, HAL LED blink, SPI/I2C and libDaisy AudioPassthru examples. Their shared include files, firmware rebuild sources, binary fixtures and license notices are included in the source update.
- Simplify the Cortex-M7 sources using static loops, shared blocks and generated identifiers. Add `*{i}` interpolation alongside `${i}` without changing multiplication.
- Add named or anonymous `input`, `output` and `wire` groups; checked, sliced named argument lists; chip-local integer constants; and clocked `clear { ... };` statements.
- Improve editor folding, multiline quoted pin lists, ARM syntax colors and operator colors. Direction colors apply to declaration keywords and chip header parameters; group names are white and declaration members retain normal colors.
- Include the current schematic layout, dragging, compact symbol, four-sided package, navigation and logic-viewer improvements.

## Scope

The Cortex-M7 and STM32H750 blocks are functional simulation models covering the included firmware demos. They do not establish complete instruction-set, peripheral or cycle-accurate hardware compatibility. Firmware rebuilding requires the external toolchains and upstream sources documented beside each demo. Existing `.ckt` demos run without rebuilding firmware.

## Release assets

The extension package is `ide/vscode/dist/ooc-language-0.8.15.vsix`.
A matching copy and update files are collected in `dist/releases/ooc-v1.1.5`:

- `ooc-language-0.8.15.vsix`
- `latest.json` and `ooc-runtime-0.8.15.json` (the matching local extension update feed)
- `SHA256SUMS.txt`
- `RELEASE-NOTES.md`

Install the VSIX with **Extensions: Install from VSIX**, then reload VS Code.
The VSIX packages the editor extension; the Python compiler, viewer and `.ckt`
examples are supplied by this source update. Restart an open viewer after
updating its Python sources.

Commit the source changes, new language modules, Cortex-M7 examples, tests,
firmware fixtures and license files together. Files under `dist/` are
intentionally ignored by Git; attach the VSIX to the GitHub release.
If using the update feed, keep its VSIX and runtime JSON beside `latest.json`.
No installer is included. No commit, push or publication was performed.
