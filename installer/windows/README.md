# Windows installer

Builds a per-user Windows x64 installer containing Python, OOC, the chip/layout library, numerical/Qt dependencies and the VS Code extension. Users do not need the repository or a separate Python installation. VS Code, Quartus/MAX 10 support and USB-Blaster drivers are separate prerequisites.

## Build

Use Windows, Python 3.11+ for this script, setuptools/pip and Inno Setup 7. From the checkout root:

    python installer/windows/build_installer.py --iscc "C:\path\to\ISCC.exe" --output dist/windows

dependencies.json pins the private Python/runtime wheels and hashes. The build stages a clean source package and verifies the bundled runtime. Distribute the final installer; staging directories are build artifacts. Keep license and third-party notices.

Update pyproject.toml and ide/vscode/package.json versions together as appropriate. release-notes.md is consumed by release CI. The workflow builds and verifies the installer, then can create a draft release.

## Installation and updates

Default location: %LOCALAPPDATA%\Programs\OOC. Each version has its own runtime and extension; install.json identifies the verified active version. Installation does not alter the user's Python or PATH. A custom destination can be selected with OOC: Installation Path in VS Code.

Setup installs the extension into an existing VS Code installation. If that step fails or VS Code is absent, users can install the bundled VSIX later.

Installed updates use published non-prerelease GitHub releases tagged ooc-v<version>, an ooc-release.json manifest and a matching installer size/hash. The user approves installation. The updater preserves the active installation on validation failure; it does not install an older version. Publication is separate from building or committing source.

Running viewers retain their old runtime until closed. Compatible extension updates can apply live; registration changes can require reload. Source checkouts use their local extension update feed instead.

Circuit files and embedded layouts belong in user project folders. Legacy layout sidecars still migrate on save. Uninstall preserves user circuits and shared layout settings; VS Code manages extension removal separately.

## Verification

Use verify_installer.py and the Windows release workflow to validate a built artifact. Run Python distribution tests and extension installed/update tests when changing packaging. Historical validation of an older release is not proof that a new artifact works. Current installers are unsigned unless signing is explicitly added.
