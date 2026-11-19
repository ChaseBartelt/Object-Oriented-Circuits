"""Installer staging and CI inputs without downloading or running Setup."""
# Inspect staged payloads and workflow inputs in temporary directories.
# These checks validate packaging without downloading dependencies or launching Setup.

from argparse import Namespace
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from installer.windows import build_installer as builder


ROOT = Path(__file__).resolve().parents[2]


class InstallerTest(unittest.TestCase):
    def test_build_keeps_required_payload_with_or_without_optional_readme(self):
        for readme in (False, True):
            with self.subTest(readme=readme), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "source checkout"

                def write(name, text):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")

                write("pyproject.toml", '[project]\nname="ooc-test"\nversion="1.2.3"\n')
                write("LICENSE", "application license")
                write("ide/vscode/package.json", '{"version":"0.9.0"}')
                write("circuitlab/__init__.py", "")
                write("circuitlab/libraries/chips/logic/74HC00.ckt", "# chip fixture\n")
                write("circuitlab/__pycache__/old.pyc", "stale bytecode")
                dependency = dict(name="dependency", version="1.0", file="dependency.whl",
                                  url="https://example.invalid/dependency.whl", sha256="a" * 64)
                write("installer/windows/dependencies.json", json.dumps([dependency]))
                write("installer/windows/setup.iss", "; compiler fixture\n")
                if readme:
                    write("README.md", "optional documentation")
                args = Namespace(output=Path(directory) / "output", cache=Path(directory) / "cache",
                                 iscc=Path(directory) / "ISCC.exe")

                def fetch(url, checksum, path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.name == builder.PYTHON_FILE:
                        with ZipFile(path, "w") as archive:
                            archive.writestr("python.exe", b"runtime fixture")
                            archive.writestr("LICENSE.txt", "Python license")
                    else:
                        self.assertEqual((url, checksum), (dependency["url"], dependency["sha256"]))
                        path.write_bytes(b"dependency fixture")
                    return path

                def extension(path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with ZipFile(path, "w") as archive:
                        archive.writestr("extension/runtime-bundle.json", '{"api":1,"version":"0.9.0"}')

                def execute(command, **kwargs):
                    command = list(map(str, command))
                    if command[1:4] == ["-m", "pip", "wheel"]:
                        source, wheels = Path(command[4]), Path(command[-1])
                        self.assertEqual((source / "README.md").exists(), readme)
                        self.assertTrue((source / "pyproject.toml").is_file())
                        self.assertTrue((source / "LICENSE").is_file())
                        self.assertFalse(list(source.rglob("*.pyc")))
                        wheels.mkdir()
                        with ZipFile(wheels / "ooc.whl", "w") as archive:
                            for path in (source / "circuitlab").rglob("*"):
                                if path.is_file():
                                    archive.write(path, path.relative_to(source).as_posix())
                    elif command[1:4] == ["-m", "pip", "install"]:
                        with ZipFile(command[-1]) as archive:
                            archive.extractall(command[command.index("--target") + 1])
                    elif command[1:] == ["-m", "circuitlab.tooling.distribution", "self-test"]:
                        self.assertEqual(kwargs["env"]["QT_QPA_PLATFORM"], "offscreen")
                        self.assertNotEqual(Path(kwargs["cwd"]), root)
                    elif command[0] == str(args.iscc):
                        (args.output / "OOC-Setup-1.2.3-windows-x64.exe").write_bytes(b"Setup fixture")
                    else:
                        self.fail(f"Unexpected external command: {command}")

                with patch.object(builder, "REPO", root), \
                     patch.object(builder, "HERE", root / "installer/windows"), \
                     patch.object(builder, "fetch", side_effect=fetch), \
                     patch.object(builder, "execute", side_effect=execute) as commands, \
                     patch.object(builder.runpy, "run_path", return_value={"build": extension}), \
                     patch.object(builder.subprocess, "check_output", side_effect=["abc123\n", ""]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    builder.build(args)

                manifest = json.loads((args.output / "build-manifest.json").read_text())
                payload = Path(manifest["payload"])
                record = json.loads((payload / "install.json").read_text())
                for field in ("python", "library", "extension", "runtime_bundle"):
                    relative = record[field].removeprefix("versions/1.2.3/")
                    self.assertTrue((payload / relative).exists(), field)
                self.assertTrue((payload / "runtime/Lib/site-packages/circuitlab/libraries/chips/logic/74HC00.ckt").is_file())
                self.assertEqual((payload / "LICENSE.txt").read_text(), "application license")
                self.assertEqual((payload / "runtime/LICENSE.txt").read_text(), "Python license")
                self.assertIn("dependency 1.0", (payload / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8"))
                self.assertEqual(record["runtime_sha256"], builder.digest(payload / "extension/runtime-bundle.json"))
                vsix = args.output / "ooc-language-0.9.0.vsix"
                self.assertEqual(record["extension_sha256"], builder.digest(vsix))
                release = json.loads((args.output / "ooc-release.json").read_text())
                setup = args.output / "OOC-Setup-1.2.3-windows-x64.exe"
                self.assertEqual(release["windows_x64"]["sha256"], builder.digest(setup))
                self.assertEqual(release["windows_x64"]["size"], setup.stat().st_size)
                self.assertEqual(manifest["source_commit"], "abc123")
                self.assertEqual(sum(call.args[0][-1] == "self-test" for call in commands.call_args_list), 1)

    def test_installer_workflow_inputs_exist_after_cleanup(self):
        workflow = (ROOT / ".github/workflows/windows-installer.yml").read_text()
        paths = re.findall(r"\b(?:installer/windows|ide/vscode)[/\w.*-]*", workflow)
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(list(ROOT.glob(path)), f"Missing installer workflow input: {path}")
        javascript = re.search(r"Get-ChildItem\s+(\S+)\s+-Filter\s+(\S+)", workflow)
        self.assertIsNotNone(javascript)
        self.assertTrue(list((ROOT / javascript[1]).glob(javascript[2])))
        self.assertIn("node --check", workflow)
        self.assertIn("--generate-notes", workflow)
