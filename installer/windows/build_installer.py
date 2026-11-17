"""Build a complete Windows x64 installer from pinned, verified runtime packages.

Requires a build Python with pip/setuptools and an Inno Setup compiler. Neither
Python, pip nor the repository is required on the user's computer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.request import urlopen
from zipfile import ZipFile

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PYTHON_VERSION = "3.14.10"
PYTHON_FILE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_FILE}"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url, checksum, path):
    if path.is_file() and digest(path) == checksum:
        return path
    temporary = path.with_suffix(path.suffix + ".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(url, timeout=60) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if digest(temporary) != checksum:
            raise ValueError(f"Checksum mismatch for {path.name}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def execute(command, **kwargs):
    print("Running:", command[0], *command[1:3], flush=True)
    subprocess.run(list(map(str, command)), check=True, **kwargs)


def build(args):
    version = tomllib.loads((REPO / "pyproject.toml").read_text())['project']['version']
    extension_version = json.loads((REPO / "ide/vscode/package.json").read_text())['version']
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="stage-", dir=out))
    payload = stage / "payload"
    runtime = payload / "runtime"
    runtime.mkdir(parents=True)
    python = fetch(PYTHON_URL, PYTHON_SHA256, cache / PYTHON_FILE)
    with ZipFile(python) as archive:
        archive.extractall(runtime)
    (runtime / "python314._pth").write_text("python314.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii")
    lock = json.loads((HERE / "dependencies.json").read_text())
    wheels = [fetch(item['url'], item['sha256'], cache / 'wheels' / item['file']) for item in lock]
    # Build from a clean source staging folder to exclude stale setuptools output.
    source = stage / "source"
    source.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / filename, source / filename)
    shutil.copytree(REPO / "circuitlab", source / "circuitlab", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    own = stage / "wheel"
    execute([sys.executable, "-m", "pip", "wheel", source, "--no-deps", "--no-build-isolation", "--wheel-dir", own])
    wheels.extend(own.glob("*.whl"))
    execute([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile",
             "--target", runtime / "Lib/site-packages", "--platform", "win_amd64", "--python-version", "3.14",
             "--implementation", "cp", "--abi", "cp314", "--only-binary=:all:", *wheels])
    extension = payload / "extension" / f"ooc-language-{extension_version}.vsix"
    packager = runpy.run_path(str(REPO / "ide/vscode/package_extension.py"))
    packager['build'](extension)
    with ZipFile(extension) as archive:
        bundle = payload / 'extension/runtime-bundle.json'
        bundle.write_bytes(archive.read('extension/runtime-bundle.json'))
    prefix = f"versions/{version}/"
    record = dict(schema=1, product="object-oriented-circuits", version=version,
                  python=prefix+'runtime/python.exe', library=prefix+'runtime/Lib/site-packages/circuitlab/libraries/chips',
                  extension=prefix+'extension/'+extension.name, runtime_bundle=prefix+'extension/runtime-bundle.json',
                  extension_version=extension_version, extension_sha256=digest(extension), runtime_sha256=digest(bundle))
    (payload / 'install.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    shutil.copy2(REPO / 'LICENSE', payload / 'LICENSE.txt')
    notices = ["OOC bundles unmodified third-party runtime packages. Their license files are retained in the installed runtime.",
               "Python " + PYTHON_VERSION + ": runtime/LICENSE.txt", "", "Python packages:"]
    notices += [f"{item['name']} {item['version']} — runtime/Lib/site-packages/<package>.dist-info/" for item in lock]
    (payload / 'THIRD-PARTY-NOTICES.txt').write_text('\n'.join(notices)+'\n',encoding='utf-8')
    environment = {**os.environ, 'QT_QPA_PLATFORM':'offscreen'}
    execute([runtime / 'python.exe', '-m', 'circuitlab.tooling.distribution', 'self-test'], cwd=stage, env=environment)
    execute([args.iscc, '/Qp', '/DAppVersion='+version, '/DExtensionVersion='+extension_version,
             '/DPayload='+str(payload), '/DSourceRoot='+str(REPO), '/DOutputDir='+str(out), HERE/'setup.iss'])
    setup = out / f'OOC-Setup-{version}-windows-x64.exe'
    release = dict(schema=1, product='object-oriented-circuits', version=version,
                   windows_x64=dict(url=f'https://github.com/EchoingFarts/Object-Oriented-Circuits/releases/download/ooc-v{version}/{setup.name}',
                                    sha256=digest(setup), size=setup.stat().st_size))
    (out/'ooc-release.json').write_text(json.dumps(release,indent=2)+'\n',encoding='utf-8')
    shutil.copy2(extension,out/extension.name)
    manifest = dict(version=version,extension_version=extension_version,stage=str(stage),payload=str(payload),
                    python=dict(version=PYTHON_VERSION,url=PYTHON_URL,sha256=PYTHON_SHA256),dependencies=lock,
                    installer=release['windows_x64'],source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
                    source_status=subprocess.check_output(['git','status','--short'],cwd=REPO,text=True).splitlines(),
                    application_wheel_sha256=digest(next(own.glob('*.whl'))), extension_sha256=digest(extension))
    (out/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(setup,flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iscc',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=REPO/'dist/windows')
    parser.add_argument('--cache',type=Path,default=REPO/'build/installer-cache')
    build(parser.parse_args())
