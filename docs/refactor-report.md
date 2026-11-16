# IDE-first refactor validation

Historical report for the initial refactor. Its temporary Python import compatibility layer was subsequently removed in OOC 1.1.1: callers now use the organized modules directly, and the package root contains only `__init__.py` and `__main__.py`. Use the [current architecture guide](architecture.md) for today's paths; the validation below records the earlier phase.

Completed November 15, 2026 on **For-IDEs**. The implementation was checked against the actual local working tree, starting at `78cdeccb0ffeade251450f55349f5f05da0bcd68`, with its existing uncommitted IDE work. The earlier source audit and its immutable snapshots remain the reference for pre-refactor behavior.

## Delivered behavior

VS Code's existing Run command now launches an independent schematic/results viewer. It does not create or import the original source editor. Repeated Run and save-refresh requests reuse that viewer. Dirty layout protects against background refresh; explicit reload/file switching offers Save, Discard or Cancel. Viewer Save writes presentation metadata, never the circuit source.

The viewer retains analysis, scope controls, diagnostics, probing, hierarchy inspection, gate-layout editing, export and the existing schematic interactions. Probe names use optional layout-sidecar overrides while older source-comment pins remain supported. Diagnostics can open their source location in VS Code. The optional original editor retains its tabs, file explorer and editing controls.

Implementation ownership is organized under `language`, `model`, `math`, `layout`, `exporters`, `tooling` and `viewer`. Historical module imports and CLI/GUI/hover launch paths remain available through compatibility modules. The new [architecture guide](architecture.md) explains the boundaries; [development guide](development.md) covers extending syntax, components, chips, solvers and exporters.

## Implementation phases and commits

| Phase | Commit | Change |
| --- | --- | --- |
| Preserve current work | `ccf2f3a` | Checkpoint existing IDE integration separately from the refactor. |
| Regression baseline | `8e8a27c` | Give tests an owned divider fixture and add 62 nominal gate truth-table cases. |
| Package organization | `bd24362` | Move implementations into responsibility-based packages, retain compatibility imports, and package submodules/chip assets. |
| Independent viewer | `ab8747f` | Add a non-Qt document session and source inspector, independent viewer, shared widgets/exports, persistence checks and immutable behavior fixtures. |
| Dependency separation | `db4e776` | Separate compiler entry point/document expansion/graph builder; numerical result contracts; geometry, automatic layout and interactive editing. Fix scope refresh during linked-file export. |

The final documentation commit follows these implementation commits. Work stayed on For-IDEs; no main-branch changes or remote push were performed. The user's edited `examples/voltage_divider.ckt` remains an uncommitted user change.

## Tests actually executed

Full Python discovery and the complete VS Code Node suite were run at each implementation phase boundary:

| Phase | Python methods passed | Python failures / errors / skips | Extension tests passed | Extension failures / skips |
| --- | ---: | --- | ---: | --- |
| Owned fixtures | 167 | 0 / 0 / 0 | 26 | 0 / 0 |
| Organized packages | 167 | 0 / 0 / 0 | 26 | 0 / 0 |
| Independent viewer | 198 | 0 / 0 / 0 | 26 | 0 / 0 |
| Final implementation | **205** | **0 / 0 / 0** | **26** | **0 / 0** |

Final Python duration was 183.266 seconds; final Node duration was 4.481 seconds. Method counts include tests with subcases: the 62 gate truth-table combinations belong to one Python test method. These counts are not coverage percentages or proof of manufacturer-level model accuracy.

The audit baseline had 166 Python methods, including two failures caused by tests expecting the original 4 V divider while the user had changed its lower resistor to 10 kΩ, producing 10 V. Tests now own their 4 V fixture. The user's example was preserved rather than changed to satisfy those assertions.

Additional focused checks covered graphs/source scopes, numerical operating points, SPICE terminal order/text, root and child layout restoration, source-byte preservation, optional probe overrides, viewer reload/export behavior and import boundaries. The layout extraction rerun passed 81 affected tests. Six architecture tests verify compatibility identities, independent compiler/linker and solver imports, source polarity geometry and the absence of Qt from those layers.

Python AST comparisons confirmed 64 layout functions/classes were moved without body changes; the old layout `route` wrapper became a direct compatibility export. The 11 compiler definitions moved into document/builder modules and the numerical result/solver definitions were structurally checked against the pre-split implementation. No electrical formula, language grammar or chip implementation was redesigned.

## Package and visual checks

The final wheel was built with `pip wheel --no-deps --no-build-isolation`, installed into a fresh target and exercised from outside the checkout using isolated Python imports. Dependencies came from the existing virtual environment; this was not a fresh dependency installation or an operating-system compatibility matrix.

- All **92 Python source files** and **59 chip `.ckt` assets** in the wheel match the current source bytes. No stale root `layout.py` is present.
- Imported `circuitlab` modules were verified to come from the installed target, not the editable checkout.
- Headless compilation/analysis, 4 V divider, packaged `@chips` NAND and SPICE generation passed.
- Compatibility CLI Check and Solve passed.
- The installed IDE viewer rendered a schematic and closed cleanly without importing or constructing the source editor.
- The installed hover worker returned a valid gate-preview PNG.
- A VSIX was built and inspected for its runtime bundle, grammar, snippets, bootstrap and registrations. The live update feed and installed extension were not replaced for this Python-side refactor.

Offscreen screenshots of the 555 schematic and divider Results view were visually inspected at 1280 × 860. Required controls, diagrams and results were readable and contained. The existing saved 555 layout's VReset caption overlap was preserved; it is not a newly introduced routing change. These checks do not substitute for a fresh interactive VS Code installation or external LTspice run.

Environment: Windows, Python 3.12.14, Node 24.22.1, PySide6 6.12, NumPy 2.5.4, SciPy 1.18.1, SymPy 1.14. Qt tests used `QT_QPA_PLATFORM=offscreen`; tests used a writable workspace temporary directory. `OOC_PYTHON` explicitly selected the project interpreter for Node/Python integration checks.

## Preservation and corrections

Checksums confirmed **91 existing library/example/layout files** match the saved starting snapshot byte for byte. The user divider SHA-256 remains `fd06829cf005ebc487271a473990a8e44b778b8115056a063fb81a00b454953e`. The README's complete Circuit language section was also retained unchanged.

Layout sidecars remain version 1. Shared gate settings retain the `Electric Circuits` / `Object-Oriented Circuits` namespace and `gate-layouts/v1/<gate>/<inputs>` keys. This environment exposed zero existing QSettings entries at backup time; preservation of populated layouts was tested through fixtures and the existing gate-layout tests, not by claiming to have recovered another environment's registry values.

Failures and findings during implementation were resolved before completion:

- A moved source-orientation helper was initially missing from geometry. The affected tests caught the `NameError`; ownership was corrected, an isolated polarity regression added, and the full final suite passed.
- One new architecture test initially supplied unsupported multiple top-level statements on one line. The fixture was corrected; parsing was not loosened to make that test pass.
- Review found export-triggered linked-file refresh could leave the scope on its old circuit. The viewer now resets it to the newly rendered graph, with a regression that edits an include while the scope is running.
- Hierarchical export now restores saved child geometry even when that package is collapsed or a redraw has serialized its manual placement.
- The pure source inspector fixes grouped-gate hover argument indexing when whitespace precedes the input group; a regression verifies the correct net.
- An intermediate wheel picked up an old generated build file. Final packaging used a clean build and exact source/package inventory comparison.

There are no known unresolved regressions from this refactor in the tested behavior.

## Remaining limits and future work

The original editor is retained as an optional compatibility shell. The graph builder and Qt schematic still contain substantial existing logic; they now have clearer owners, but this is not a claim that every large class has been decomposed. Compatibility files can be removed only after downstream imports have migrated.

Compile/solve and some layout operations remain synchronous. The scope uses worker batches with cooperative cancellation between steps; an already-running step finishes before cancellation. No measured large-circuit speed or memory improvement is claimed merely from the package reorganization. A sparse solver, incremental scene updates and broader background compilation remain separate work.

The 40 canonical chip definitions retain their existing status: 33 approximate native models and seven pinout-only definitions. Twelve aliases and seven helpers are not additional packaged chips. Generic BJT behavior, convergence limits, unsupported nonlinear AC/port analysis and arbitrary external SPICE-model limitations remain. Passing regression tests demonstrates preserved implementation behavior, not guaranteed physical accuracy.

Analog electrical compilation remains separate from single-bit combinational Verilog generation. The refactor adds no analog-to-FPGA conversion, sequential synthesis, classes/recursion, external simulator backend, hardware deployment or new chip physics. No new LTspice or MATLAB execution was performed; existing serialized exports and captured MATLAB reference tests were checked.

## Use the updated application

Close any OOC viewer process that was already running before these Python changes, then click Run in VS Code once. The existing extension commands remain compatible; no VSIX reinstall or VS Code reload is required for these Python changes. New viewer saves and subsequent source saves use the same window. A running Python hover worker also needs a fresh process to use changed Python implementation code.

Validation logs, package hashes, backups, wheel/VSIX checks and screenshots are retained in the task's `outputs/OOC-Refactor-2026-11-15` artifact directory. `package-validation.json` pins the packaged implementation to `db4e7765aeb2fd785280fc136eb796c066adac60`.
