"""Exercise a built installer in a disposable folder and optional VS Code profile.

No production installation, editor profile or published release is modified.
The update uses a synthetic older descriptor/runtime and a local release fixture;
the real downloader checksum checks and Setup executable still run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]


def verify(args):
    args.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="verify-", dir=args.work_dir)).resolve()
    target = work / "installed app"
    project = work / "user project"
    project.mkdir()
    temp = work / "temp"
    temp.mkdir()
    env = {**os.environ, "TEMP": str(temp), "TMP": str(temp), "QT_QPA_PLATFORM": "offscreen"}
    report = {"installer": str(args.setup.resolve()), "sha256": hashlib.sha256(args.setup.read_bytes()).hexdigest(),
              "work": str(work), "checks": [], "published_feed_tested": False}
    profile = work / "code profile"
    extensions = work / "code extensions"
    # Even the real updater's /TASKS=vscode path must never fall back to the
    # developer's VS Code profile when --code was omitted.
    code = args.code.resolve() if args.code else work / "no-vscode/Code.exe"
    extra = ["/PORTABLE=1", f"/CODE={code}", f"/CODEUSERDATA={profile}", f"/CODEEXTENSIONS={extensions}"]

    def run(command, name, timeout=600):
        command = list(map(str, command))
        result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (work / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); see {work / (name + '.log')}")
        return result.stdout

    def install(name):
        run([args.setup.resolve(), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}",
             "/TASKS=vscode" if args.code else "/TASKS=", f"/LOG={work / (name + '-setup.log')}", *extra], name)
        if args.code:
            extension_result = json.loads((target / "extension-install.log").read_text())
            assert extension_result.get("ok"), extension_result
            assert list(extensions.glob("echoingfarts.ooc-language-*"))

    def execute_python(python, script, name, *arguments, timeout=600):
        filename = work / (name + ".py")
        filename.write_text(script, encoding="utf-8")
        return run([python, "-X", "utf8", filename, *arguments], name, timeout)

    try:
        print(f"Installing in {target}", flush=True)
        install("install")
        record = json.loads((target / "install.json").read_text())
        python = target / record["python"]
        report["version"] = record["version"]
        report["checks"].append("Fresh silent install and activation")
        report["checks"].append("VS Code extension installed in isolated profile" if args.code else "VS Code registration not requested")
        info = json.loads(run([python, "-m", "circuitlab.tooling.distribution", "self-test"], "self-test"))
        assert Path(info["python"]).resolve() == python.resolve()
        assert Path(info["library"]).is_relative_to(target)

        divider = project / "divider.ckt"
        divider.write_text("V1(12) between(supply, 0);\nR1(2k) between(supply, out);\nR2(1k) between(out, 0);\n", encoding="utf-8")
        solved = run([python, "-m", "circuitlab.tooling.cli", "solve", divider], "solve")
        assert "Node out: 4 V" in solved, solved
        chip = project / "nand.ckt"
        shutil.copy2(REPO / "tests/fixtures/refactor/chip_library_nand.ckt", chip)
        run([python, "-m", "circuitlab.tooling.cli", "solve", chip], "chip-library")
        report["checks"].append("Packaged compiler solves external 4 V divider and @chips NAND")

        pot = project / "potentiometer.ckt"
        pot.write_text("V1(5) between(supply, 0);\nP1(10k, 25%) between(supply, level, 0);\n"
                       "op1(level, out, out);\nR1(1k) between(out, 0);\n", encoding="utf-8")
        solved = run([python, "-m", "circuitlab.tooling.cli", "solve", pot], "potentiometer")
        assert "Node level: 1.25 V" in solved and "Node out: 1.25 V" in solved, solved
        report["checks"].append("Packaged three-terminal potentiometer and buffered output solve to 1.25 V")

        if args.full_tests:
            print("Running all tests against the installed package", flush=True)
            execute_python(python, '''import json, pathlib, sys, unittest
import circuitlab
root = pathlib.Path(sys.argv[1])
assert pathlib.Path(circuitlab.__file__).is_relative_to(pathlib.Path(sys.executable).parent)
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / 'tests'), top_level_dir=str(root))
result = unittest.TextTestRunner(verbosity=2).run(suite)
pathlib.Path(sys.argv[2]).write_text(json.dumps(dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped), package=circuitlab.__file__)), encoding='utf-8')
raise SystemExit(not result.wasSuccessful())
''', "installed-tests", REPO, work / "installed-tests.json", timeout=1200)
            report["installed_tests"] = json.loads((work / "installed-tests.json").read_text())

        sentinel = target / "my-circuit.ckt"
        sidecar = target / "my-circuit.ckt.layout.json"
        sentinel.write_bytes(divider.read_bytes())
        sidecar.write_text('{"version":1,"user-sentinel":true}', encoding="utf-8")
        kept = {path: path.read_bytes() for path in (sentinel, sidecar, divider)}
        install("repair")
        report["checks"].append("Same-version repair preserves source and sidecar files")

        # A separately located old runtime remains loaded while real Setup installs
        # and activates the new version. This is a fixture, not a published old build.
        old_prefix = "versions/0.0.0/"
        old_bundle = target / "versions/0.0.0"
        shutil.copytree(target / "versions" / record["version"], old_bundle,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        old = {**record, "version": "0.0.0"}
        for field in ("python", "library", "extension", "runtime_bundle"):
            old[field] = old_prefix + record[field].split("/", 2)[2]
        (target / "install.json").write_text(json.dumps(old), encoding="utf-8")
        release = json.loads(args.setup.with_name("ooc-release.json").read_text())
        fixture = work / "release-fixture.json"
        fixture.write_text(json.dumps(release), encoding="utf-8")
        print("Testing update download, verification, Setup and activation", flush=True)
        execute_python(target / old["python"], '''import json, pathlib, subprocess, sys
from unittest.mock import patch
from circuitlab.tooling import distribution, updates
root, setup, fixture, extra = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), json.loads(sys.argv[4])
release = json.loads(fixture.read_text())
tag = 'ooc-v' + release['version']
def fetch(url, limit):
    if url == updates.API:
        return json.dumps(dict(tag_name=tag, assets=[dict(name='ooc-release.json', browser_download_url=updates.BASE+tag+'/ooc-release.json')])).encode()
    assert url == updates.BASE+tag+'/ooc-release.json'
    return json.dumps(release).encode()
real_check, real_download, real_run = updates.check, updates.download, subprocess.run
def opener(request, **kwargs):
    assert request.full_url == release['windows_x64']['url']
    stream = setup.open('rb')
    stream.url = request.full_url
    return stream
def install(command, **kwargs):
    return real_run([*command, *extra], **kwargs)
with patch.object(updates, 'check', lambda root: real_check(root, fetch)), patch.object(updates, 'download', lambda record, directory: real_download(record, directory, opener)), patch.object(updates.subprocess, 'run', install):
    assert updates.check(root)['available']
    assert updates.apply(root, release['version'])['ok']
    assert not updates.check(root)['available']
assert distribution.read_install(root)['version'] == release['version']
print(json.dumps(dict(ok=True, previous_runtime=sys.executable, active=distribution.read_install(root)['python'])))
''', "update", target, args.setup.resolve(), fixture, json.dumps(extra), timeout=900)
        assert json.loads((target / "install.json").read_text())["version"] == record["version"]
        run([python, "-m", "circuitlab.tooling.cli", "solve", divider], "solve-after-update")
        report["checks"].append("Synthetic older runtime -> real verified installer -> active current runtime; equal version not offered")
        for path, contents in kept.items():
            assert path.read_bytes() == contents, path

        print("Uninstalling test application and checking user files", flush=True)
        run([target / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             f"/LOG={work / 'uninstall-setup.log'}"], "uninstall")
        assert not python.exists(), python
        assert not (target / "install.json").exists()
        for path, contents in kept.items():
            assert path.read_bytes() == contents, path
        report["checks"].append("Uninstall removes registered runtime/descriptor and retains user source/layout files")
        report["ok"] = True
    except Exception as error:
        report["ok"] = False
        report["error"] = str(error)
        raise
    finally:
        destination = work / "validation.json"
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(destination, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--code", type=Path)
    parser.add_argument("--full-tests", action="store_true")
    verify(parser.parse_args())
