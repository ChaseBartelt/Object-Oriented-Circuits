"""Check and install complete application releases from the project's GitHub feed."""
# Validate release identity, version ordering, and integrity metadata before accepting an update.
# Downloads stay temporary until complete and verified; only then may the installer activate
# the candidate while preserving the previous usable installation on failure.

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from circuitlab.tooling.distribution import PRODUCT, read_install

REPOSITORY = "EchoingFarts/Object-Oriented-Circuits"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
BASE = f"https://github.com/{REPOSITORY}/releases/download/"
MAX_INSTALLER = 700 * 1024 * 1024


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("Invalid OOC release version.")
    return tuple(map(int, value.split(".")))


def read_url(url, limit):
    if urlsplit(url).scheme != "https":
        raise ValueError("OOC downloads require HTTPS.")
    request = Request(url, headers={"User-Agent": "OOC-Updater/1", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        if urlsplit(response.url).scheme != "https":
            raise ValueError("Insecure download redirect.")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("OOC release metadata is too large.")
    return data


# Treat remote metadata as untrusted input: validate product identity, version, URL, and
# integrity fields before any candidate is considered installable.
def validate_release(record, tag):
    if record.get("schema") != 1 or record.get("product") != PRODUCT:
        raise ValueError("Invalid OOC release manifest.")
    release = record.get("version")
    version(release)
    if tag != f"ooc-v{release}":
        raise ValueError("Release tag and application version differ.")
    asset = record.get("windows_x64", {})
    name = f"OOC-Setup-{release}-windows-x64.exe"
    if asset.get("url") != BASE + tag + "/" + name:
        raise ValueError("Unexpected OOC installer URL.")
    if not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]):
        raise ValueError("Invalid installer checksum.")
    if type(asset.get("size")) is not int or not 0 < asset["size"] <= MAX_INSTALLER:
        raise ValueError("Invalid installer size.")
    return record


def check(root, fetch=read_url):
    current = read_install(root)
    version(current["version"])
    try:
        release = json.loads(fetch(API, 1024 * 1024))
    except HTTPError as error:
        if error.code == 404:
            return {"available": False, "current": current["version"], "message": "No published OOC release yet."}
        raise
    if release.get("draft") or release.get("prerelease"):
        return {"available": False, "current": current["version"]}
    tag = release.get("tag_name", "")
    if not re.fullmatch(r"ooc-v\d+\.\d+\.\d+", tag):
        return {"available": False, "current": current["version"], "message": "No packaged OOC release is published as latest."}
    location = BASE + tag + "/ooc-release.json"
    if not any(asset.get("name") == "ooc-release.json" and asset.get("browser_download_url") == location
               for asset in release.get("assets", ())):
        raise ValueError("The published release is missing its OOC manifest.")
    record = validate_release(json.loads(fetch(location, 64 * 1024)), tag)
    return {"available": version(record["version"]) > version(current["version"]),
            "current": current["version"], "release": record}


# Keep incomplete bytes out of the final installer path and verify integrity before promotion.
def download(record, directory, opener=urlopen):
    """Stream into a temporary file; a partial/corrupt update is never executable."""
    validate_release(record, "ooc-v" + record["version"])
    asset = record["windows_x64"]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f'OOC-Setup-{record["version"]}-windows-x64.exe'
    descriptor, temporary = tempfile.mkstemp(dir=directory, suffix=".partial")
    total = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with opener(Request(asset["url"], headers={"User-Agent": "OOC-Updater/1"}), timeout=60) as response:
                if urlsplit(response.url).scheme != "https":
                    raise ValueError("Insecure installer redirect.")
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > asset["size"]:
                        raise ValueError("Installer exceeds its declared size.")
                    digest.update(chunk)
                    stream.write(chunk)
        if total != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise ValueError("Installer download is incomplete or its checksum does not match.")
        os.replace(temporary, final)
        return final
    finally:
        Path(temporary).unlink(missing_ok=True)


# Recheck the candidate and installed version at application time, since they may have
# changed since the earlier update check.
def apply(root, expected):
    result = check(root)
    if not result["available"] or result["release"]["version"] != expected:
        raise ValueError("The available release changed. Check for updates again.")
    root = Path(root).resolve()
    setup = download(result["release"], root / "updates")
    # Versioned directories allow existing viewers to finish using the previous runtime.
    process = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                              f"/DIR={root}", "/TASKS=vscode", f"/LOG={root / 'update.log'}"],
                             timeout=600)
    if process.returncode or read_install(root)["version"] != expected:
        raise RuntimeError("OOC Setup did not complete the update. See update.log in the installation folder.")
    return {"ok": True, "version": expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    try:
        answer = check(args.root) if args.command == "check" else apply(args.root, args.expected_version)
        print(json.dumps(answer))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
