"""Installer activation and update validation without modifying a real installation."""
# Use temporary install/release fixtures and mocked external steps to protect real installations.
# Identity, containment, checksum, and activation checks must still reject stale or corrupt
# candidates.
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from circuitlab.tooling import distribution, updates


class DistributionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.record = dict(schema=1, product=distribution.PRODUCT, version="1.1.0",
                           python="versions/1.1.0/python.exe", library="versions/1.1.0/chips",
                           extension="versions/1.1.0/ooc.vsix", runtime_bundle="versions/1.1.0/runtime.json")
        for field in ("python", "library", "extension", "runtime_bundle"):
            path = self.root / self.record[field]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        self.save()

    def save(self):
        (self.root / "install.json").write_text(json.dumps(self.record), encoding="utf-8")

    def release(self, data=b"installer bytes", version="1.2.0"):
        return dict(schema=1, product=distribution.PRODUCT, version=version,
                    windows_x64=dict(url=updates.BASE+f"ooc-v{version}/OOC-Setup-{version}-windows-x64.exe",
                                     size=len(data), sha256=hashlib.sha256(data).hexdigest()))

    def fetcher(self, record):
        tag = "ooc-v" + record["version"]
        def fetch(url, limit):
            if url == updates.API:
                return json.dumps(dict(tag_name=tag, assets=[dict(name="ooc-release.json",
                    browser_download_url=updates.BASE+tag+"/ooc-release.json")])).encode()
            self.assertEqual(url, updates.BASE+tag+"/ooc-release.json")
            return json.dumps(record).encode()
        return fetch

    def test_installed_paths_reject_escape_or_wrong_product(self):
        self.assertEqual(distribution.read_install(self.root)["version"], "1.1.0")
        for relative in ("../other.exe", str(self.root.parent / "other.exe"), "."):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                distribution.contained(self.root, relative)
        self.record["product"] = "other"
        self.save()
        with self.assertRaises(ValueError): distribution.read_install(self.root)

    def test_activation_preserves_previous_descriptor_if_validation_fails(self):
        before = (self.root / "install.json").read_bytes()
        candidate = {**self.record, "version": "1.2.0"}
        manifest = self.root / "candidate.json"
        manifest.write_text(json.dumps(candidate), encoding="utf-8")
        with patch.object(distribution.sys, "executable", str(self.root / self.record["python"])):
            with patch.object(distribution, "self_test", side_effect=RuntimeError("broken")):
                with self.assertRaises(RuntimeError): distribution.activate(self.root, manifest)
            self.assertEqual((self.root / "install.json").read_bytes(), before)
            with patch.object(distribution, "self_test", return_value={"ok": True,"version":"1.2.0"}):
                distribution.activate(self.root, manifest)
        self.assertEqual(distribution.read_install(self.root)["version"], "1.2.0")

    def test_activation_refuses_downgrade_or_mismatched_package(self):
        manifest = self.root / 'candidate.json'
        manifest.write_text(json.dumps(self.record),encoding='utf-8')
        with patch.object(distribution.sys,'executable',str(self.root/self.record['python'])):
            with patch.object(distribution,'self_test',return_value={'version':'9.0.0'}):
                with self.assertRaises(ValueError): distribution.activate(self.root,manifest)
            self.record['version']='2.0.0';self.save()
            with patch.object(distribution,'self_test',return_value={'version':'1.1.0'}):
                with self.assertRaises(ValueError): distribution.activate(self.root,manifest)
        self.assertEqual(distribution.read_install(self.root)['version'],'2.0.0')

    def test_updates_compare_numerically_and_do_not_downgrade(self):
        for candidate, available in (("1.2.0", True), ("1.10.0", True), ("1.1.0", False), ("1.0.9", False)):
            with self.subTest(candidate=candidate):
                result = updates.check(self.root, self.fetcher(self.release(version=candidate)))
                self.assertEqual(result["available"], available)

    def test_repair_replaces_damaged_previous_installation_after_self_test(self):
        manifest = self.root / "candidate.json"
        manifest.write_text(json.dumps(self.record), encoding="utf-8")
        damaged = {**self.record, "python": "missing/python.exe"}
        for old in (json.dumps(damaged), "interrupted invalid metadata"):
            (self.root / "install.json").write_text(old, encoding="utf-8")
            with patch.object(distribution.sys, "executable", str(self.root / self.record["python"])), \
                 patch.object(distribution, "self_test", return_value={"version": "1.1.0"}):
                distribution.activate(self.root, manifest)
            self.assertEqual(distribution.read_install(self.root), self.record)

    def test_unpublished_release_is_reported_without_installing(self):
        def missing(*args): raise HTTPError(updates.API, 404, "not found", {}, None)
        result = updates.check(self.root, missing)
        self.assertFalse(result["available"])
        self.assertIn("No published", result["message"])

    def test_release_rejects_foreign_urls_tags_and_bad_integrity_metadata(self):
        for key, value in (("url", "https://example.org/installer.exe"), ("url", "http://github.com/a.exe"),
                           ("sha256", "bad"), ("size", -1), ("size", True), ("size", updates.MAX_INSTALLER+1)):
            record = self.release()
            record["windows_x64"][key] = value
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                updates.validate_release(record, "ooc-v1.2.0")
        with self.assertRaises(ValueError): updates.validate_release(self.release(), "ooc-v2.0.0")

    def test_download_never_promotes_partial_or_changed_installer(self):
        record = self.release()
        def opener(data):
            stream = io.BytesIO(data)
            stream.url = record["windows_x64"]["url"]
            return lambda *args, **kwargs: stream
        destination = self.root / "updates"
        for data in (b"partial", b"different bytes", b"installer bytes with excess"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                updates.download(record, destination, opener(data))
            self.assertFalse(list(destination.glob("*.exe")))
            self.assertFalse(list(destination.glob("*.partial")))
        result = updates.download(record, destination, opener(b"installer bytes"))
        self.assertEqual(result.read_bytes(), b"installer bytes")

    def test_apply_rechecks_release_and_checks_setup_exit_and_active_version(self):
        release = self.release()
        with patch.object(updates,"check",return_value={"available":True,"release":release}), \
             patch.object(updates,"download",return_value=self.root/'setup.exe'), \
             patch.object(updates.subprocess,"run") as run:
            with self.assertRaises(ValueError): updates.apply(self.root,"1.3.0")
            run.assert_not_called()
            run.return_value.returncode = 1
            with self.assertRaises(RuntimeError): updates.apply(self.root,"1.2.0")
            run.return_value.returncode = 0
            with self.assertRaises(RuntimeError): updates.apply(self.root,"1.2.0")
            def installed(*args,**kwargs):
                self.record['version']='1.2.0';self.save()
                return type('Done',(),{'returncode':0})()
            run.side_effect = installed
            self.assertTrue(updates.apply(self.root,'1.2.0')['ok'])
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_extension_install_uses_code_cli_and_separate_arguments(self):
        code = self.root / "VS Code" / "Code.exe"
        cli = code.parent / "resources/app/out/cli.js"
        cli.parent.mkdir(parents=True)
        code.touch(); cli.touch()
        with patch.object(distribution.subprocess, "run") as run:
            run.return_value.returncode=0;run.return_value.stdout="Installed";run.return_value.stderr=""
            distribution.install_extension(code,self.root/'has spaces.vsix',user_data=self.root/'profile')
            args=run.call_args.args[0]
            self.assertEqual(args[:2],[str(code.resolve()),str(cli.resolve())])
            self.assertIn(str(self.root/'has spaces.vsix'),args)
            self.assertEqual(run.call_args.kwargs['env']['ELECTRON_RUN_AS_NODE'],'1')
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_versioned_vscode_cli_is_read_without_running_its_batch_file(self):
        code = self.root/'Code.exe'; code.touch()
        cli = self.root/'active123/resources/app/out/cli.js'
        cli.parent.mkdir(parents=True);cli.touch()
        launcher=self.root/'bin/code.cmd';launcher.parent.mkdir()
        launcher.write_text('"%~dp0..\\Code.exe" "%~dp0..\\active123\\resources\\app\\out\\cli.js" %*',encoding='utf-8')
        self.assertEqual(distribution.code_command(code,['--version']),[str(code),str(cli),'--version'])
        launcher.write_text('"%~dp0..\\..\\other\\resources\\app\\out\\cli.js"',encoding='utf-8')
        with self.assertRaises(ValueError): distribution.code_command(code,[])
