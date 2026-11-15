# OOC 1.1.0 Windows installer validation

Validated November 15, 2026 on Windows 11 x64 (10.0.26200). Implementation commit: `61411bc` on `For-IDEs`. The build began from `448040f` with these implementation changes in the working tree; all packaged Python source files were subsequently compared with the committed implementation. The build manifest retains the original build-time commit/status and wheel hashes rather than misrepresenting it as a clean build at that earlier commit.

## Deliverable

| Item | Value |
| --- | --- |
| Installer | `OOC-Setup-1.1.0-windows-x64.exe` |
| Size | 137,958,893 bytes (131.57 MiB) |
| SHA-256 | `fe94a6610fee61029b4eb1a747aa6a96209e264152bb96881b73683c03f0d847` |
| Payload | Approximately 430 MiB; caches, saved files and retained previous versions add to this |
| Application / extension | OOC 1.1.0 / `echoingfarts.ooc-language` 0.7.0 |
| Private Python | 3.14.10, official embeddable Windows x64 runtime |
| Dependencies | PySide6 Essentials/Shiboken 6.12.0; NumPy 2.5.4; SciPy 1.18.1; SymPy 1.14.0; MPMath 1.3.0 |
| Build compiler | Inno Setup 7.1.1, official download with verified SHA-256 |

All 59 chip-library `.ckt` assets matched their source bytes. The application wheel excludes the repository's examples, tests, Git data and virtual environment. The runtime and six dependency wheels were verified against their pinned hashes before packaging. Third-party license files and package metadata are retained.

## Executed checks

| Check | Result |
| --- | --- |
| Full Python suite, developer Python 3.12.14 | 216 tests; zero failures, errors or skips |
| Full Python suite, installed private Python 3.14.10 | 216 tests; zero failures, errors or skips |
| Extension suite, Node 24.22.1, developer interpreter | 30 tests; zero failures or skips |
| Extension suite with installed interpreter | 30 tests; zero failures or skips |
| Fresh silent installation | Passed, including post-install validation and active descriptor |
| VS Code extension registration | Passed in a separate profile and extensions directory, including VS Code's versioned CLI resource path |
| Installed extension runtime discovery | Located the private interpreter and `@chips/logic/74HC00.ckt` from an external project without a checkout |
| External circuit compilation/solution | 12 V / 2 kΩ / 1 kΩ divider produced 4 V; library NAND example solved |
| Viewer | Windows Qt renderer visually inspected; labels readable; no source-editor widgets |
| Hover service | Installed JSON-lines worker produced a readable NOT-gate pinout PNG |
| Same-version repair | Passed; source and layout sentinel files unchanged |
| Update transition | Passed using the fixture described below and the real installer |
| Uninstall | Registered current runtime and active descriptor removed; circuit source and layout sentinel files retained unchanged |
| Public feed check | Actual GitHub request succeeded with “No published OOC release yet” |
| Source whitespace / diff check | Passed |

Python test commands: `python -m unittest discover -s tests -v`. Extension command: `node --test ide/vscode/test/*.test.js`, with `OOC_PYTHON` set explicitly. Qt tests use the offscreen platform; the viewer visual inspection also used the Windows platform with the window hidden. The installed suite preloaded `circuitlab` from the private runtime before loading the checkout's tests and fixtures. Tests explicitly exercising a direct source-file launch still use that source-file path.

The reproducible installed check is `installer/windows/verify_installer.py --setup <installer> --work-dir <test-folder> --full-tests --code <Code.exe>`. It uses a disposable application directory and isolated VS Code directories. Installation runs in portable test mode, which suppresses Start-menu/desktop shortcuts and the Installed-apps registry entry.

The update test cloned the runtime into a synthetic `0.0.0` version folder and made that the active descriptor. While its Python process remained running, the real updater consumed a local fixture for GitHub metadata/download bytes, verified the actual installer SHA-256 and size, ran Setup, and activated 1.1.0. The circuit solved after updating and the same version was no longer offered. This proves the local update/install path; it is not a test against a historical public release or the GitHub asset CDN. Separate tests reject altered/partial downloads, foreign release URLs, downgrade attempts and failed activation.

## Limits and release status

- The installer is unsigned. No Authenticode certificate or publisher reputation was established.
- VS Code itself is a separate prerequisite for IDE editing. The compiler and viewer install without it; Setup can be rerun later to register the extension.
- No public release or repository push was performed. Publish the installer and matching `ooc-release.json` together as the latest `ooc-v1.1.0` release to enable public downloads. Future updates require new versioned releases.
- The GitHub Actions build/validation/draft-release workflow was added but not executed on GitHub.
- Interactive wizard navigation, real Start-menu/Installed-apps registration, a clean Windows VM, Windows 10, and ARM64 emulation were not tested. Automated installation/repair/update/uninstall used the Windows 11 host and portable isolation described above.
- The packaging change does not improve electrical-model accuracy or add chip functionality. Existing model approximations and unsupported modes remain.
- Previously open viewers continue using their old runtime until closed. Compatible extension feature updates can apply live; extension registration/bootstrap changes can still require a VS Code reload.

The user's preexisting edit to `examples/voltage_divider.ckt` was preserved and excluded from the implementation commit and installer.
