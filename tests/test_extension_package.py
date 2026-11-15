import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from ide.vscode.package_extension import build, publish, runtime_bundle, RUNTIME_FILES


class ExtensionPackageTest(unittest.TestCase):
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
                bundled = archive.read("extension/runtime-bundle.json")
                self.assertEqual(bundled, (root / "dist" / release["runtime"]["file"]).read_bytes())
                runtime = json.loads(bundled)
                self.assertEqual(runtime["version"], release["version"])
                self.assertEqual(runtime["api"], 1)
                self.assertEqual(set(runtime["files"]), set(RUNTIME_FILES))
