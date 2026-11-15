# Object-Oriented Circuits for VS Code

Install the OOC `.vsix` in VS Code using **Extensions → … → Install from VSIX**. Open a `.ckt` file directly in the editor. The language mode in the status bar will read **Object-Oriented Circuits**.

## Try the divider

Open `examples/circuit_method.ckt` in the OOC checkout. **Outline** shows the battery, `divider` circuit and `main`. Fold their bodies in the editor or use **F12** on the `divider(...)` call to jump to the declaration. F12 also opens an `include(...)` path when the target is available locally.

The language mode provides highlighting, bracket matching, indentation, and **Ctrl+/** line comments. Type `ooc-divider` and accept the snippet to insert that example into an empty `.ckt` file. Snippets also cover circuit methods, built-in control flow and digital chips.

## Check or solve

Save the circuit and its included files. Open the Command Palette with **Ctrl+Shift+P** for **OOC: Check Circuit** or **OOC: Solve Circuit**. The play button on a saved `.ckt` editor also runs Solve. Results appear in the **OOC** output channel, with diagnostics from the compiler shown in **Problems**.

The commands use the OOC checkout's Python environment. If you edit a circuit in another folder, set **OOC: Project Root** to the OOC checkout location. Set **OOC: Python Path** if you use a Python executable outside the checkout's `.venv`. Reading, highlighting, folding and Outline work without the Python environment. Interactive schematics and waveforms are available in the OOC desktop app.

## Build the extension from source

From the OOC repository root, run `python ide/vscode/package_extension.py ooc-language-0.1.0.vsix`. The package uses the shared OOC TextMate grammar and contains no editor runtime dependencies. The compiler commands use the existing project dependencies.
