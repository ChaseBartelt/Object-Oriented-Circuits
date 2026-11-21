# OOC for VS Code

Language support for .ckt files: highlighting, snippets, Outline, folding, navigation, diagnostics and gate/chip/wire hover previews. Edit syntaxes/OOC.json for highlighting rules and snippets/ooc.code-snippets for snippets.

## Run a circuit

Install OOC or set up the [source checkout](../../README.md#quick-start). Open your circuit folder, save a .ckt file, and click the play button or **OOC: Run Circuit / Open Schematic**. Save again to update the same viewer. **OOC: Check Circuit** validates source; **OOC: Solve Circuit** prints electrical results.

The extension discovers a checkout or installed runtime. Use **OOC: Project Root**, **OOC: Python Path**, or **OOC: Installation Path** when needed. Workspace trust is required before compiler commands run. Save included files before running their entry circuit.

Hover a gate/chip name for its reference symbol or pinout, and a wire argument for its connected path. F12 navigates definitions and includes. Digital syntax supports buses, clock blocks, +=, Hz increments, every(...), and behavioral chip assembly. Electrical and digital source have separate execution rules.

In a `.ckt` editor, select lines and press **Ctrl+#** (**Ctrl+Shift+3** on a US keyboard) or **Ctrl+/** to toggle line comments. The shortcut removes both `#` and `//` from a fully commented selection. New line comments use `//`. You can also run **OOC: Toggle Line Comment** from the Command Palette.

## Build the extension from source

From the checkout root:

    .\.venv\Scripts\python.exe ide/vscode/package_extension.py ide/vscode/dist/ooc-language.vsix

Install ide/vscode/dist/ooc-language-<version>.vsix with **Extensions: Install from VSIX**. The builder also creates the local update manifest and runtime bundle. Increment package.json before packaging a new extension release; rebuilding the same version does not trigger updates.

Source checkouts watch this local feed. Compatible runtime.js, structure.js and hover-client.js updates can apply live; grammar, registration and bootstrap changes can require reload. Python changes require restarting their running processes. Installed users use **OOC: Check for Updates** for the complete application bundle.

Packaging and syntax tests live in tests/tooling. Run them with `python -m unittest discover -s tests/tooling -t .`; use `node --check` on the extension's JavaScript files for syntax checks.

Analog and digital blocks, model properties, math/state functions, component calls, constants, SI units, wire counters, and control flow have distinct syntax scopes. Both editors use blue (`#659CD7`) for `analog`, `digital`, and `count`, light blue (`#AED6FF`) for `measure`, and orange (`#F6A652`) for `@override`. Other VS Code syntax follows your active theme. Quoted paths and comments retain their own colors. FPGA declarations, signals, bit widths/selectors, and clock/timing shortcuts also remain highlighted. After installing a grammar update, use **Developer: Reload Window** to load the new highlighting.
