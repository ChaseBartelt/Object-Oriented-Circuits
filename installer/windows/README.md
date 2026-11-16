# Windows installer

The deliverable is `OOC-Setup-<version>-windows-x64.exe`. It bundles Python, OOC, its 59 chip assets, PySide6 Essentials/Shiboken, NumPy, SciPy, SymPy/MPMath and the VS Code extension. Users need neither a Python installation nor a repository checkout. Installation is offline apart from any behavior of VS Code's own extension installer. VS Code itself is a separate prerequisite for IDE editing; the viewer works without it.

## Installation and ownership

Setup installs per user at `%LOCALAPPDATA%\Programs\OOC`, without changing PATH or the user's Python installations. A custom `/DIR=` can be used; set **OOC: Installation Path** to that directory in VS Code. The standard location is discovered automatically. A developer checkout and explicit Python setting retain precedence.

Each release has `versions/<version>/runtime` and `versions/<version>/extension`. `install.json` identifies the active runtime, library, extension and bundle checksums. Setup validates the packaged solver/imports/library before atomically switching this descriptor. An incompatible, broken or older package cannot replace the active descriptor. Existing viewer processes keep using their old version until closed; old version files are retained so updates do not forcibly terminate them.

The shared gate-layout QSettings namespace is unchanged. Circuits and `.ckt.layout.json` sidecars belong in the user's project folders. Uninstall removes installed application files and its descriptor, not user circuits or gate preferences. VS Code owns the extension separately; remove it from Extensions when desired. Untracked Python caches may leave empty/nonempty version directories after uninstall; they are not recursively deleted at the expense of user-created files.

The wizard finds per-user/system VS Code or allows choosing `Code.exe`. The extension is installed with VS Code's executable CLI, passing arguments separately. If VS Code is absent or installation fails, Setup explains that the compiler/viewer succeeded and how to finish extension installation. The bundled VSIX remains available for repair. This installer does not silently download or install VS Code itself.

## Build

Use Windows, Python 3.11+ for the build script, pip/setuptools, and Inno Setup 7. The application itself keeps its existing Python compatibility; the installer pins a private Python 3.14.10 runtime. Run from the checkout:

```powershell
python installer/windows/build_installer.py --iscc "C:\path\to\ISCC.exe" --output dist/windows
```

The build verifies the official Python archive SHA-256 and the six dependency wheel hashes in `dependencies.json`. Dependencies are vendored as wheels, not installed by pip on the end user's computer. The embedded runtime uses an explicit `_pth` file to isolate its package paths. Licenses and distribution metadata are retained, alongside OOC's license and third-party notices. Qt Essentials supplies the QtCore/QtGui/QtWidgets modules used by OOC without installing unused Qt Addons.

The build creates a clean source staging folder so stale setuptools outputs cannot leak into the installer. Output includes the installer, VSIX, `ooc-release.json` and a build manifest recording inputs and hashes. Staging directories are retained for inspection; distribute the final installer, not the stage or source repository.

Version the Python package in `pyproject.toml` and the extension in `ide/vscode/package.json`. Update pinned dependencies deliberately and repeat the installed-runtime tests. The current installer is not Authenticode signed. Add release signing before public distribution if a trusted publisher identity is required, and calculate release checksums after signing.

## Updates and publishing

An installed extension checks the repository's latest public GitHub release at most every six hours in a trusted workspace. **OOC: Check for Updates** checks immediately. **OOC: Auto Update** disables automatic checks. The user chooses **Install update** before the whole application is downloaded and installed. Development checkouts retain their existing local extension update feed and compatible live runtime replacement.

The updater accepts only:

- A non-draft, non-prerelease tag `ooc-v<version>` with a matching manifest.
- `ooc-release.json` attached to that release in `EchoingFarts/Object-Oriented-Circuits`.
- The expected Windows installer name/URL, a bounded size, and a matching SHA-256.

Downloads are streamed into partial files and promoted only after integrity checks. Older/equal versions are not installed. Errors leave the active installation intact. The installer updates the complete compiler/viewer/library/extension set, preventing mismatched Python features. Compatible extension runtime updates can apply live; registration/bootstrap changes offer a reload. Running Python viewers still need to close before using the new runtime.

Publishing the first installer is required before other computers can download it or receive releases through this feed. Until then, the updater accurately reports that no packaged release exists. HTTPS/checksums protect transport/integrity; they do not replace a publisher signature or secure release-account access.

The manual [Windows installer workflow](../../.github/workflows/windows-installer.yml) tests/builds artifacts and can create a **draft** release for review. Publish it as the latest release only after checking installation, update and uninstall behavior. Upload the installer and manifest together; changing the installer requires updating its checksum. The current local build has not been published by the build script.

## Validate without changing your development installation

For an isolated install, use `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /PORTABLE=1 /DIR="<test folder>"`. Portable test mode suppresses Start-menu/desktop shortcuts and the Installed-apps registry entry; the uninstaller is still created. It does not change the runtime's semantics.

Pass `/CODE="...\Code.exe" /CODEUSERDATA="<test profile>" /CODEEXTENSIONS="<test extension folder>"` to test extension installation separately from your active VS Code profile. `/TASKS=vscode` selects extension installation; `/TASKS=""` installs only the runtime/viewer. A custom runtime can be discovered by `OOC: Installation Path` or the test-process environment variable `OOC_INSTALL_ROOT`.

Test the packaged interpreter, not just the developer virtual environment: CLI check/solve, chip includes, hover PNGs, viewer launch/reload, source/layout preservation, rejected downloads, version transitions, then uninstall with sentinel user files retained. `tests/test_distribution.py` and `ide/vscode/test/installed.test.js` cover activation, integrity checks and repository-free discovery. Full suites remain necessary for changes to shared runtime behavior.

The automated installed-application check does this in a new disposable folder:

```powershell
python installer/windows/verify_installer.py --setup "dist/windows/OOC-Setup-1.1.1-windows-x64.exe" --work-dir "C:\OOC-test" --full-tests --code "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"
```

Omit `--code` when VS Code is unavailable. The update check uses a synthetic older descriptor/runtime and local release data while exercising the real checksum verification and installer; it does not publish a release or claim to test GitHub delivery. The full installed suite imports OOC from the bundled runtime, while tests and fixtures come from the checkout. Tests explicitly exercising direct source-file launches still use their source-file paths. Test logs and `validation.json` remain available after the test installation is uninstalled.

Build references: [Python embeddable runtime](https://docs.python.org/3/using/windows.html#the-embeddable-package), [Python 3.14.10 files/checksums](https://www.python.org/downloads/release/python-31410/), [VS Code CLI](https://code.visualstudio.com/docs/configure/command-line), [Inno portable build-tool installation](https://jrsoftware.org/inno-setup/documentation/topic_technotes.htm).
