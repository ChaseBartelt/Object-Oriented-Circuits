# Object-Oriented Circuits for VS Code

Install the OOC `.vsix` in VS Code using **Extensions → … → Install from VSIX**. Open a `.ckt` file directly in the editor. The language mode in the status bar will read **Object-Oriented Circuits**.

The Windows OOC installer installs this extension and a private compiler/viewer runtime together. It automatically finds the installed chip library and keeps one viewer connection across files in the same VS Code workspace. You do not need a checkout or a separate Python installation. For a custom installer directory, set **OOC: Installation Path**. Development checkouts and explicit Python settings still take precedence.

Installed applications check published GitHub releases at most every six hours; **OOC: Check for Updates** checks immediately and offers **Install update** for the complete compiler/viewer/library/extension set. Close an older viewer after updating. The first release must be published before public updates are available. The following local update behavior applies to development checkouts.

## Automatic local updates

OOC checks your trusted local checkout every 30 seconds for a newer completed extension package. The source is **OOC: Project Root**, the checkout containing the active file, or an OOC workspace folder. After a new release is built, it installs automatically. You no longer need to browse for a VSIX after the initial installation of 0.5.0.

Starting with 0.6.0, compatible feature updates also **apply live without reloading VS Code**. Hover previews, navigation, existing commands and save-refresh handlers are replaced in place. Your schematic process, last-run circuit and unsaved layout remain connected. Hover workers restart on demand. Invalid JavaScript is rejected before replacement; activation errors restore the previous runtime.

Upgrading from 0.5.0 needs one reload to start the new loader. Changes to that loader, registered commands/settings, language configuration, snippets or TextMate grammar still offer **Reload Window**. Those are outside the live runtime. Reload when ready; your editor is never forcibly restarted, and other extensions are not restarted.

Use **OOC: Check for Updates** to check immediately, or disable **OOC: Auto Update** in Settings. Restricted workspaces do not install updates. This updates from the local checkout; it does not fetch GitHub changes or publish to the Marketplace. Python changes in that checkout already take effect in newly launched OOC processes; restart existing schematic/hover processes after changing their Python code.

## Behavioral chips and circuit assembly (0.8.0)

The editor recognizes named `main Design { ... }` blocks, reusable digital chip calls,
bus ports such as `data[8] = input;`, internal `link[8] = wire;` connections,
combinational `if/else`, and clocked state with `clock`, `every`, Hz increments and
compound assignments. Outline and folding include the named main and nested blocks;
F12 on a chip call opens its declaration. Existing electrical syntax remains supported.

Open any file in [`examples/behavioral`](../../examples/behavioral/README.md) and click
the editor's **play button**, or run **OOC: Run Circuit / Open Schematic**. The same
viewer window is reused. Select **Logic simulation** to set inputs and inspect buses;
use its **Run** or **Step** controls for clocked examples. **Ctrl+S** refreshes the
last circuit opened in the viewer. **OOC: Check Circuit** validates digital source;
**OOC: Solve Circuit** is for the electrical solver.

Try `01_byte_selector.ckt` first: enter numbers for `a` and `b`, then toggle `choose`.
`02_counter_bank.ckt` has three independent counters. The assembled HYBRID01 and
ten-switch examples are `03_hybrid01.ckt` and `04_switch_bank.ckt`; use the existing
DE10-Lite profile for their automatic switch synchronization and FPGA build flow.

Install 0.8.0 and use **Developer: Reload Window** once so VS Code loads the new
TextMate grammar and snippets. Close any viewer started with an older Python version
before clicking Run again. This release keeps the existing local update mechanism;
grammar registrations cannot be replaced by the live JavaScript runtime loader.

## Try the divider

Open `examples/circuit_method.ckt` in the OOC checkout. **Outline** shows the battery, `divider` circuit and `main`. Fold their bodies in the editor or use **F12** on the `divider(...)` call to jump to the declaration. F12 also opens an `include(...)` path when the target is available locally.

The language mode provides highlighting, bracket matching, indentation, and **Ctrl+/** line comments. Type `ooc-divider` and accept the snippet to insert that example into an empty `.ckt` file. Snippets also cover circuit methods, built-in control flow and digital chips.

## Open the schematic

Open a `.ckt` file and click the **play button** at the top right, or choose **OOC: Run Circuit / Open Schematic** from **Ctrl+Shift+P**. This saves the active file and opens the lightweight OOC viewer, which compiles the circuit and draws its schematic. The window includes Results, analysis, interactive layout, diagnostics and export controls. It does not construct a source editor or file tabs: code stays in VS Code. A circuit that can be rendered may still have solver errors.

Save included files before running. The play button reuses the window for that OOC checkout and reloads the saved circuit, updating the schematic and results. Running another circuit uses the same window. Closing the window lets the next Run start a fresh one. Unsaved viewer layout changes prompt before an explicit reload or file switch.

**Save layout** (or **Ctrl+S** in the viewer) stores geometry and probe captions inside a managed comment block at the end of the `.ckt` file. Attached viewers use a checked VS Code edit and wait for its save acknowledgment; save any unsaved IDE edits first. Existing `.ckt.layout.json` files still load and transfer into comments on save, with the JSON retained as a backup. The embedded layout takes priority. Shared gate-layout settings and existing `# @pin` comments still work. Double-click a viewer diagnostic to open its file and line in VS Code.

The connection lasts for the VS Code session; after reloading VS Code, close the old OOC window before running again. This extension requires an installed OOC runtime or matching development checkout with `--editor` support. The original editor remains available by launching `python -m circuitlab` without `--editor`. After updating Python implementation files, close the old viewer and Run again to load the new code.

### Refresh on save

Click Run once to open the schematic. Saving a `.ckt` or `.txt` file in the same OOC project then refreshes the last circuit you ran, including changes to saved include files. Save All events are combined into one refresh. The window stays in the background while you type. Closing it stops automatic updates until you click Run again. Turn off **OOC: Refresh On Save** to disable this behavior.

Unsaved viewer layout changes pause automatic refresh. Click **Save layout**, then save in VS Code again. This protects your layout without interrupting editing with a dialog or letting the viewer overwrite source.

### Hover previews

Hover over a gate or chip name to see its reference symbol or declared pinout. Hover over a wire argument such as `middle` in `R3(1k) between(middle, split);` to see its name and a small connected-path schematic with that wire highlighted. These are OOC model references, not manufacturer photographs. Wire paths are representative traces, not simulated current flow.

Previews use the current editor text and saved include files, and do not alter the main schematic. They run in a separate, reusable offscreen process. A syntax error or uninstantiated method may provide wire information without an image. Large traces provide text instead of an unreadably small diagram. VS Code controls tooltip placement. Both features require a trusted local workspace and the updated OOC checkout. Turn off **OOC: Hover Previews** to disable image previews.

## Check or solve

Save included files. Open the Command Palette with **Ctrl+Shift+P** for **OOC: Check Circuit** or **OOC: Solve Circuit**. These commands save the active circuit and show text results in the **OOC** output channel, with diagnostics from the compiler shown in **Problems**.

The commands use the OOC checkout's Python environment. If you edit a circuit in another folder, set **OOC: Project Root** to the OOC checkout location. Set **OOC: Python Path** if you use a Python executable outside the checkout's `.venv`. Reading, highlighting, folding and Outline work without the Python environment. Interactive schematics and waveforms run in the separate OOC viewer.

Bundled chip imports can use just the chip name, such as `include("74HC00");` or `include("NE555");`. F12 on the name opens its file under `circuitlab/libraries/chips`. The compiler and hover previews use the same library. Existing `@chips/` paths and relative `.ckt` / `.txt` imports remain supported.

## Build the extension from source

For each extension release, increase `version` in `ide/vscode/package.json`, then run `python ide/vscode/package_extension.py ooc-language-0.6.0.vsix` (use the new version in the filename). The builder places the versioned package, runtime bundle and checksum manifest in the ignored `ide/vscode/dist/` directory. It publishes the manifest only after all artifacts are complete. Running editors pick up the newer version automatically. Raw source edits are not installed until packaged, and rebuilding the same version does not trigger an update.

`runtime.js`, `structure.js` and `hover-client.js` form the replaceable runtime. The builder fingerprints the remaining packaged implementation and editor registrations. A matching fingerprint permits live replacement; a difference requires a reload. Keep loader changes separate when a release should apply live. Source development must use a built/staged package because `runtime-bundle.json` is generated into the VSIX.

The package uses the shared OOC TextMate grammar and contains no editor runtime dependencies. The compiler commands use the existing project dependencies.

The Python implementation is organized under `circuitlab/language`, `model`, `math`, `layout`, `exporters`, `tooling` and `viewer`. The extension launches `circuitlab` for the viewer, `circuitlab.tooling.cli` for checks/solutions, and `circuitlab.tooling.hover` for previews. It discovers development checkouts by their `circuitlab/tooling/cli.py` file. Extension 0.7.1 uses these direct paths; update the extension when moving to the cleaned source tree. See the [architecture overview](../../docs/architecture.md) and [development guide](../../docs/development.md).
