# Developing OOC

Start with the [architecture overview](architecture.md) and the [language reference](../README.md#circuit-language). The code is organized by responsibility; root-level compatibility modules preserve older imports. Add implementation to the organized packages rather than duplicating it in those forwarding modules.

## Set up and check a change

Use the editable install from the main README. From the checkout root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:OOC_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
node --test ide/vscode/test/*.test.js
```

Python tests exercise Qt offscreen. Node is needed for extension tests. If an environment restricts temporary-file access, set `TEMP` and `TMP` to an existing writable workspace directory. Do not describe a skipped Python hover smoke test as passing; `OOC_PYTHON` selects the intended interpreter explicitly.

Use focused tests while editing, then the full suites at a phase boundary. Existing test files often contain many subtests; method counts are not assertion counts or coverage percentages. Check the recorded failures, not just a wrapper command's exit status.

Keep examples editable. The CLI/GUI 4 V test uses `tests/fixtures/voltage_divider.ckt`; the current user-facing divider has a 10 kΩ lower resistor and produces 10 V. The broader [refactor fixtures](../tests/fixtures/refactor/README.md) were captured before reorganization. Do not regenerate them from the new implementation merely to hide a difference.

## Use the compiler without a window

For direct source text, use `circuitlab.language.compiler.compile_circuit`. For a document with file includes and viewer-style diagnostics, use a session:

```python
from circuitlab.tooling.session import CircuitSession

session = CircuitSession()
session.load("examples/circuit_method.ckt")
session.compile()
for issue in session.issues:
    print(issue.path, issue.line, issue.message)
if session.solution is not None and session.solution.network is not None:
    network = session.solution.network
    for node, voltage in zip(network.nodes, network.voltage):
        print(node, voltage)
```

The session does not import Qt. `session.drawing` may exist even when a model cannot be solved. Replacing source clears old numerical/display results. Keep any additional cache tied to a specific accepted source revision and its include inputs.

## Add or adjust syntax

First write a valid example, an invalid example and the intended resulting circuit. Decide whether the feature changes graph construction, calculation or presentation.

1. Review `language/syntax.py`, `methods.py`, `control_flow.py` and the compilation entry point before adding a new recognizer. `document.py` elaborates source into scoped statements; `builder.py` constructs the resulting electrical graph.
2. Keep source path/line information through include loading, method expansion and generated statements. Repeated method calls must have distinct internal names.
3. Lower new source forms into existing electrical components when possible. The solver should not need to know that a loop created a resistor.
4. Preserve established dispatch/scoping behavior, including older spellings. Do not reinterpret existing identifiers just because they resemble a new keyword.
5. Update `tooling/source_info.py`, editor highlighting/help, TextMate grammar or the VS Code structure scanner only where their behavior changes. Incomplete code must remain safe to edit.
6. Test valid/invalid forms, nested calls, included definitions, diagnostics, graph structure and export.

Loops and conditions are evaluated at compile time. Preserve integer, nesting and expansion limits; do not use Python `eval` on circuit text. Runtime voltage-dependent switching is an electrical modeling feature, not a new meaning for compile-time `if`. Recursive definitions and general classes are not currently supported.

## Add an electrical primitive

Try composing existing devices in a `.ckt` method first. A new primitive needs an explicit contract for pin order, polarity, parameters, supported analyses, and initial state.

- Define its graph fields and terminal behavior in `model/graph.py`, then compiler construction/validation under `language/`.
- Add only the numerical operations actually implemented under `math/`; report unsupported modes clearly. Document current and voltage sign conventions.
- Distinguish sensing inputs from conducting branches. Reusing display connectivity as a solver equation can create false shorts.
- Add pin geometry, symbol/reference drawing and movement/rotation checks under `layout/` and `viewer/`.
- Add target mappings under `exporters/`. Test serialized pin order independently of the source argument order.
- Test analytical cases, edge/failure cases, hierarchy, source origins, saved layout restoration and export connectivity.

Ordinary functions and small data records are preferred. Do not build a class hierarchy for every resistor or source merely to make the tree look object-oriented.

## Add a chip

Use `circuitlab/libraries/chips/<group>/<canonical-name>.ckt`. Keep package variants, physical pin order, source links, modeled behavior and omissions in its comments. Aliases should import the canonical definition instead of copying it.

Implement the chip with existing primitives and reusable methods when possible. Internal nodes must stay private per instance. A definition without an electrical model should retain `model = unavailable;` so rendering is allowed while simulation/export give an honest diagnostic.

Validate the device's purpose: truth tables for combinational logic; reset/clock/load/hold behavior for sequential parts; transfer/loading/limits for analog models. Compile success alone is not model validation. Check source-declared physical pin order, repeated instances, unused/tied pins, hierarchy rendering and SPICE export. Keep the catalog's modeled/pinout-only status accurate, and do not count alias/shared helper files as extra packages.

## Change appearance or wiring geometry

Use **View → Gate Layouts** for shared gate appearance and the renderer for one circuit's layout. `language/gates.py` defines electrical transistor arrangements; `layout/gates.py` defines appearance/templates. Changing one should not accidentally change the other.

`Layout` in `layout/geometry.py` owns symbols, net-tagged segments, junctions, grounds, connectors and caption offsets. Use `layout/automatic.py` for recognized topologies, `layout/routing.py` for general automatic placement/wires, and `layout/editing.py` for interactive movement. Geometry operations should return a candidate whose connections still satisfy `preserves_nets` in `layout/checks.py`. Test both movement directions, rotation, wire straightening, group selection, connector split/join and undo. A crossing without a dot must stay disconnected.

Preserve the saved-layout version and settings namespace. Saved user gate layouts take precedence over new defaults. Use `restore_layout` to bind stored records to the current graph; do not apply raw coordinates to unrelated component identities. Verify export uses stored child geometry even when a package is collapsed.

The IDE viewer never writes source during Save. Probe caption overrides belong in its sidecar, with existing source-comment probes still honored. Any future metadata migration needs an old-format reader and roundtrip tests.

## Change a solver or exporter

Solver changes require more than a screenshot. Preserve electrical orientation, reference domains, scalar/DC/AC/mixed distinctions, nonlinear startup state, convergence diagnostics and transient timing. Compare numerical outputs with justified tolerances and verify expected failures. Dense-to-sparse solving, different integration rules and new semiconductor physics are separate numerical changes, not part of a folder move.

Keep shared numerical contracts in `math/results.py`. Its `Network` record and `SimulationBackend` protocol must not depend on a particular solver; importing a nonlinear solver should not require a circular import through linear dispatch.

An exporter consumes a resolved graph and optionally its geometry. It must allocate target names without merging distinct nets, map every terminal correctly, retain hierarchy companions and refuse unsupported models instead of omitting them. Compare connectivity independently of visual appearance. A future external-simulator backend should be explicit; ordinary compile/check must not require that simulator to be installed.

## Work on the viewer and IDE

`tooling/session.py` owns source/compilation state; `viewer/window.py` owns the lightweight Qt window. Shared results/layout dialogs belong in `viewer/widgets.py`. Keep the original editor's source tabs and completion widgets inside the optional editor path.

Keep Qt scene/widget operations on the GUI thread. If moving expensive compile/solve/layout work to a worker, use source revisions to discard stale results and coalesce save events. The current refactor does not promise all expensive operations are already asynchronous.

The VS Code replaceable JavaScript runtime is `runtime.js`, `structure.js` and `hover-client.js`. The stable bootstrap/update loader has a separate compatibility boundary. Follow the [extension build guide](../ide/vscode/README.md#build-the-extension-from-source): version and package a release before expecting the local updater to install it. Grammar, registration or loader changes can require reload; Python implementation changes require restarting affected Python processes.

## Check packaging before a release

The package configuration discovers the Python subpackages and includes the chip assets. Verify a built wheel and VSIX, not only an editable checkout:

1. Check that canonical Python modules, compatibility entry points and all 59 chip `.ckt` assets are present.
2. Install to a fresh target and run from outside the source checkout. Assert imported `circuitlab` modules come from that target so an editable environment cannot silently hide missing packaged files.
3. Exercise CLI Check/Solve, `@chips` resolution, a packaged NAND calculation, viewer launch and offscreen hover rendering.
4. Verify the VSIX contains its generated runtime bundle, grammar, snippets and registrations. Test both compatible live replacement and reload-required updates.
5. Report skips, failures and unverified external-tool/hardware comparisons honestly.

## Keep explanations close to the code

For each non-obvious algorithm, document what data it owns, which electrical or layout invariant it preserves, and why the chosen calculation is needed. Prefer clear names and a small example over comments that merely repeat the implementation. A feature may legitimately span language, math and display code; the goal is to make those responsibilities easy to locate, not force them into one giant file.
