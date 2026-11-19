# Inspect generated archives and release hashes instead of installing an extension.
# Optional documentation may disappear without creating dangling assets or changing runtime
# identity.
import json
import unittest
from pathlib import Path
from shutil import copyfile, copytree, ignore_patterns
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from ide.vscode.package_extension import (
    EXTENSION, REPO, RUNTIME_FILES, VSIX_SCHEMA, build, publish, runtime_bundle,
)


class ExtensionPackageTest(unittest.TestCase):
    def copy_sources(self, root):
        extension = root / "extension"
        copytree(EXTENSION, extension, ignore=ignore_patterns("README.md", "dist", "__pycache__"))
        copyfile(REPO / "LICENSE", root / "LICENSE")
        return extension

    def test_optional_readme_has_no_dangling_manifest_asset_or_runtime_change(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extension = self.copy_sources(root)
            bundles = []
            with patch.multiple("ide.vscode.package_extension", EXTENSION=extension, REPO=root):
                for include_readme in (False, True):
                    with self.subTest(include_readme=include_readme):
                        if include_readme:
                            (extension / "README.md").write_bytes(b"# OOC extension\r\nOptional documentation.\r\n")
                        package = root / f"readme-{include_readme}.vsix"
                        build(package)
                        with ZipFile(package) as archive:
                            manifest = ET.fromstring(archive.read("extension.vsixmanifest"))
                            assets = {
                                asset.attrib["Type"].rsplit(".", 1)[-1]: asset.attrib["Path"]
                                for asset in manifest.findall("v:Assets/v:Asset", {"v": VSIX_SCHEMA})
                            }
                            self.assertEqual(set(assets), {"Manifest", "License"} | ({"Details"} if include_readme else set()))
                            for path in assets.values():
                                self.assertIn(path, archive.namelist())
                            self.assertEqual("extension/README.md" in archive.namelist(), include_readme)
                            if include_readme:
                                self.assertEqual(assets["Details"], "extension/README.md")
                                self.assertEqual(archive.read(assets["Details"]), b"# OOC extension\nOptional documentation.\n")
                            bundles.append(archive.read("extension/runtime-bundle.json"))
            self.assertEqual(*bundles)

    def test_missing_required_assets_still_fail_the_build(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extension = self.copy_sources(root)
            required = [
                *extension.glob("*.js"),
                extension / "language-configuration.json",
                extension / "syntaxes/OOC.json",
                *extension.glob("snippets/*.code-snippets"),
                root / "LICENSE",
            ]
            with patch.multiple("ide.vscode.package_extension", EXTENSION=extension, REPO=root):
                for path in required:
                    with self.subTest(asset=path.relative_to(root)):
                        data = path.read_bytes()
                        path.unlink()
                        try:
                            with self.assertRaises(FileNotFoundError):
                                build(root / "missing.vsix")
                        finally:
                            path.write_bytes(data)

    def test_line_endings_do_not_force_a_live_update_reload(self):
        package = {"version": "0.7.3", "main": "extension.js"}
        files = {name: b"exports.activate = () => {};\n" for name in RUNTIME_FILES}
        files.update({"extension.js": b"// loader\nexports.activate = () => {};\n",
                      "syntaxes/OOC.json": b'{\n"scopeName": "source.ooc"\n}\n'})
        windows = {name: data.replace(b"\n", b"\r\n") for name, data in files.items()}
        self.assertEqual(runtime_bundle(package, files), runtime_bundle(package, windows))

    def test_compatibility_excludes_runtime_and_version_but_includes_grammar_and_loader(self):
        package = {"version": "0.6.0", "main": "extension.js", "contributes": {"commands": []}}
        files = {name: b"exports.activate = () => {};" for name in RUNTIME_FILES}
        files.update({"extension.js": b"loader", "syntaxes/OOC.json": b"grammar"})
        digest = lambda p, f: json.loads(runtime_bundle(p, f))["compatibility"]
        original = digest(package, files)
        self.assertEqual(digest({**package, "version": "0.7.0"}, {**files, "runtime.js": b"new code"}), original)
        self.assertNotEqual(digest(package, {**files, "syntaxes/OOC.json": b"new grammar"}), original)
        self.assertNotEqual(digest(package, {**files, "extension.js": b"new loader"}), original)
        self.assertNotEqual(digest({**package, "contributes": {"commands": ["new"]}}, files), original)

    def test_package_publishes_identical_runtime_and_complete_release(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "ooc.vsix"
            build(package)
            publish(package, root / "dist")
            release = json.loads((root / "dist/latest.json").read_text())
            with ZipFile(package) as archive:
                for name in archive.namelist():
                    if name.startswith("extension/"):
                        self.assertNotIn(b"\r\n", archive.read(name), name)
                bundled = archive.read("extension/runtime-bundle.json")
                self.assertEqual(bundled, (root / "dist" / release["runtime"]["file"]).read_bytes())
                runtime = json.loads(bundled)
                self.assertEqual(runtime["version"], release["version"])
                self.assertEqual(runtime["api"], 1)
                self.assertEqual(set(runtime["files"]), set(RUNTIME_FILES))
