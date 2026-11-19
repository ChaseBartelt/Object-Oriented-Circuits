"""Embedded geometry preserves executable text and imports old layouts safely."""
# Compare executable text before and after metadata writes, including encoding/newlines.
# Malformed blocks, failed replacements, and legacy migration must preserve the original file.
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from circuitlab.language.compiler import compile_circuit
from circuitlab.layout.persistence import (
    embed_placement, layout_payload, migrate_placement, pack_layout,
    placement_path, read_pin_overrides, read_placement, restore_layout,
    strip_placement, write_placement,
)
from circuitlab.layout.routing import fast_layout
from circuitlab.math.report import component_labels


SOURCE = 'V1(5) between(v,0);\nR1(1k) between(v,out);\nR2(2k) between(out,0);\n'


class EmbeddedLayoutsTest(unittest.TestCase):
    # Give this case private files so disk edits cannot leak into another test case.
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'circuit.ckt'
        self.circuit = compile_circuit(SOURCE, render_only=True)
        self.layout = fast_layout(self.circuit, component_labels(self.circuit))
        # Use the serialized forms in equality checks: coordinates become lists.
        self.records = {(None, 'auto'): pack_layout(self.layout),
                        ('@method:0', 'routed'): {'saved-child': [1, 2, 3]}}
        self.pins = {'out': 'Output Ω', 'v': None}
        self.data = json.loads(write_placement('auto', self.records, pin_overrides=self.pins))

    def test_roundtrip_preserves_source_behavior_and_every_record(self):
        result = embed_placement(SOURCE, 'auto', self.records, pin_overrides=self.pins)
        self.assertEqual(strip_placement(result), SOURCE)
        self.assertEqual(layout_payload(result), self.data)
        self.assertEqual(compile_circuit(result, render_only=True).components, self.circuit.components)
        self.path.write_bytes(result.encode('utf-8'))
        mode, saved = read_placement(self.path)
        self.assertEqual(mode, 'auto')
        self.assertEqual(set(saved), set(self.records))
        self.assertEqual(read_pin_overrides(self.path), self.pins)
        self.assertIsNotNone(restore_layout(saved[None, 'auto'], self.circuit, ()))

    def test_repeated_save_replaces_block_and_prefers_embedded_over_sidecar(self):
        first = embed_placement(SOURCE, 'auto', self.records, pin_overrides=self.pins)
        second = embed_placement(first, 'routed', {}, pin_overrides={'out': 'New'})
        self.assertEqual(second.count('# @ooc-layout-begin'), 1)
        self.assertEqual(strip_placement(second), SOURCE)
        self.assertEqual(embed_placement(second, 'routed', {}, pin_overrides={'out': 'New'}), second)
        self.path.write_bytes(second.encode('utf-8'))
        placement_path(self.path).write_bytes(write_placement('auto', self.records, pin_overrides=self.pins))
        self.assertEqual(read_placement(self.path), ('routed', {}))
        self.assertEqual(read_pin_overrides(self.path), {'out': 'New'})

    def test_migration_preserves_bom_newlines_all_data_and_sidecar_backup(self):
        for ending in ('\n', '\r\n'):
            for final_newline in (False, True):
                for bom in (False, True):
                    with self.subTest(ending=ending, final_newline=final_newline, bom=bom):
                        source = SOURCE.replace('\n', ending)
                        if not final_newline:
                            source = source.rstrip('\r\n')
                        encoding = 'utf-8-sig' if bom else 'utf-8'
                        original = source.encode(encoding)
                        self.path.write_bytes(original)
                        sidecar = placement_path(self.path)
                        backup = write_placement('auto', self.records, pin_overrides=self.pins)
                        sidecar.write_bytes(backup)
                        self.assertTrue(migrate_placement(self.path))
                        migrated = self.path.read_bytes()
                        self.assertEqual(migrated.startswith(b'\xef\xbb\xbf'), bom)
                        text = migrated.decode(encoding)
                        self.assertEqual(strip_placement(text).encode(encoding), original)
                        self.assertEqual(layout_payload(text), self.data)
                        self.assertEqual(sidecar.read_bytes(), backup)
                        self.assertFalse(migrate_placement(self.path))
                        self.assertEqual(self.path.read_bytes(), migrated)
                        if ending == '\r\n':
                            self.assertNotIn('\n', text.replace('\r\n', ''))

    def test_empty_source_and_nonlayout_comments_are_preserved(self):
        for source in ('', '# user note', '# user note\n', SOURCE + '# keep this\n\n'):
            with self.subTest(source=source):
                embedded = embed_placement(source, 'auto', {})
                self.assertEqual(strip_placement(embedded), source)

    def test_two_blank_lines_before_comments_without_accumulating_spacing(self):
        for ending in ('\n', '\r\n'):
            for suffix in ('', ending, ending * 2, ending * 3, ending * 5, ending + '  '):
                source = 'R1(1k) between(a,b);' + suffix
                with self.subTest(ending=ending, suffix=suffix):
                    embedded = embed_placement(source, 'auto', self.records)
                    prefix = embedded.split('# @ooc-layout-begin')[0]
                    self.assertGreaterEqual(len(prefix.splitlines()) - 1, 2)
                    self.assertEqual(strip_placement(embedded), source)
                    self.assertEqual(embed_placement(embedded, 'auto', self.records), embedded)

    def test_old_markers_remain_readable_when_spacing_is_added(self):
        payload = json.dumps(self.data)
        for source, option in ((SOURCE, ''), (SOURCE.rstrip('\n'), ' no-final-newline')):
            old = source + ('\n' if option else '') + '# @ooc-layout-begin' + option + '\n# ' + payload + '\n# @ooc-layout-end\n'
            updated = embed_placement(old, 'auto', self.records, pin_overrides=self.pins)
            self.assertEqual(strip_placement(updated), source)
            self.assertEqual(layout_payload(updated), self.data)

    def test_malformed_or_duplicate_block_is_never_overwritten(self):
        valid = embed_placement('', 'auto', {})
        broken = [
            SOURCE + '# @ooc-layout-begin\n# {}\n',
            SOURCE + '# @ooc-layout-end\n',
            SOURCE + valid + valid,
            SOURCE + '# @ooc-layout-begin\nR3(1k) between(v,0);\n# @ooc-layout-end\n',
            SOURCE + '# @ooc-layout-begin\n# {broken}\n# @ooc-layout-end\n',
            SOURCE + '# @ooc-layout-begin\n# {"version":2,"mode":"auto","layouts":[]}\n# @ooc-layout-end\n',
        ]
        for source in broken:
            with self.subTest(source=source):
                self.path.write_bytes(source.encode('utf-8'))
                placement_path(self.path).write_bytes(write_placement('auto', self.records))
                self.assertIsNone(layout_payload(source))
                self.assertTrue(read_placement(self.path)[1])  # Legacy data remains recoverable.
                with self.assertRaises(ValueError):
                    embed_placement(source, 'auto', {})
                with self.assertRaises(ValueError):
                    migrate_placement(self.path)
                self.assertEqual(self.path.read_bytes(), source.encode('utf-8'))

    def test_legacy_fallback_without_writing_and_missing_sidecar(self):
        self.path.write_bytes(SOURCE.encode('utf-8'))
        self.assertFalse(migrate_placement(self.path))
        self.assertEqual(read_placement(self.path), ('auto', {}))
        placement_path(self.path).write_bytes(write_placement('auto', self.records, pin_overrides=self.pins))
        self.assertTrue(read_placement(self.path)[1])
        self.assertEqual(read_pin_overrides(self.path), self.pins)
        self.assertEqual(self.path.read_bytes(), SOURCE.encode('utf-8'))

    def test_failed_atomic_replace_keeps_original_and_removes_temporary_file(self):
        self.path.write_bytes(SOURCE.encode('utf-8'))
        sidecar = placement_path(self.path)
        backup = write_placement('auto', self.records)
        sidecar.write_bytes(backup)
        with patch('circuitlab.layout.persistence.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                migrate_placement(self.path)
        self.assertEqual(self.path.read_bytes(), SOURCE.encode('utf-8'))
        self.assertEqual(sidecar.read_bytes(), backup)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), sorted([self.path.name, sidecar.name]))

    def test_brief_windows_lock_retries_but_does_not_overwrite_new_source(self):
        self.path.write_bytes(SOURCE.encode('utf-8'))
        placement_path(self.path).write_bytes(write_placement('auto', self.records))
        real_replace = os.replace
        calls = []
        def locked_once(source, target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError('scanner lock')
            return real_replace(source, target)
        with patch('circuitlab.layout.persistence.os.replace', side_effect=locked_once):
            self.assertTrue(migrate_placement(self.path))
        self.assertEqual(len(calls), 2)
        self.path.write_bytes(SOURCE.encode('utf-8'))
        changed = SOURCE + '# saved externally\n'
        def changed_while_locked(source, target):
            self.path.write_bytes(changed.encode('utf-8'))
            raise PermissionError('scanner lock')
        with patch('circuitlab.layout.persistence.os.replace', side_effect=changed_while_locked):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                migrate_placement(self.path)
        self.assertEqual(self.path.read_bytes(), changed.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
