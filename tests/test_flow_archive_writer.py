import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_flow_cleanup as fixtures
from agent_bridge.storage import FileLock, atomic_json
import connect_project
import flow_archive
import flow_archive_writer as writer
import role_manage


class ArchiveWriterTests(unittest.TestCase):
    setUp = fixtures.FlowCleanupTests.setUp
    run_flow = fixtures.FlowCleanupTests.run_flow
    preview = fixtures.FlowCleanupTests.preview
    snapshot = fixtures.FlowCleanupTests.snapshot

    def write(self):
        return writer.write_archive(self.root, 'sample', self.preview()['plan_hash'], 1, now=self.now)

    def assert_originals(self, before):
        after = self.snapshot()
        for path, value in before.items():
            self.assertEqual(value, after[path], path)

    def test_complete_snapshot_preserves_originals_and_deduplicates(self):
        before = self.snapshot()
        result = self.write()
        self.assertEqual(result['artifact_state'], 'ARCHIVE_READY')
        self.assertEqual(result['deleted_files'], 0)
        self.assert_originals(before)
        entries = result['entries']
        self.assertEqual(len(list((self.flow/'archive/blobs').iterdir())), len({e['sha256'] for e in entries}))
        self.assertLess(len(list((self.flow/'archive/blobs').iterdir())), len(entries))
        for entry in entries:
            source = writer._source(self.flow, entry, result['records'])
            self.assertEqual(source.read_bytes(), (self.flow/'archive/blobs'/entry['sha256']).read_bytes())
        self.assertEqual(json.loads((self.flow/'result.json').read_text())['state'], 'REVIEW_DONE')
        after = self.snapshot()
        self.assertEqual(flow_archive.load_snapshot(self.root, 'sample')['records'], result['records'])
        self.assertEqual(after, self.snapshot())

    def test_stale_plan_and_busy_locks_create_no_archive(self):
        plan = self.preview()['plan_hash']
        (self.flow/'test-0.log').write_bytes(b'changed')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'PLAN_CHANGED'):
            writer.write_archive(self.root, 'sample', plan, 1, now=self.now)
        self.assertEqual(before, self.snapshot())
        plan = self.preview()['plan_hash']
        with FileLock(self.base['state']/'runner.lock'):
            with self.assertRaisesRegex(ValueError, 'FLOW_PROTECTED'):
                writer.write_archive(self.root, 'sample', plan, 1, now=self.now)
        self.assertFalse((self.flow/'cleanup.json').exists())

    def test_blob_write_failure_preserves_payload_and_blocks_retry(self):
        before = self.snapshot()
        with patch.object(writer, 'durable_write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.write()
        self.assert_originals(before)
        journal = json.loads((self.flow/'cleanup.json').read_text())
        self.assertEqual(journal['state'], 'ARCHIVE_FAILED')
        self.assertTrue((self.root/journal['staging']).exists())
        self.assertIn('CLEANUP_INCOMPLETE', self.preview()['reasons'])
        with self.assertRaisesRegex(ValueError, 'FLOW_PROTECTED'):
            writer.write_archive(self.root, 'sample', journal['plan_hash'], 1, now=self.now)

    def test_source_change_during_copy_is_rejected(self):
        original = writer.durable_write
        def mutate(path, data):
            original(path, data)
            (self.flow/'test-0.log').write_bytes(b'concurrent change')
        with patch.object(writer, 'durable_write', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'SOURCE_CHANGED|PLAN_CHANGED'):
                self.write()
        self.assertFalse((self.flow/'archive').exists())
        self.assertTrue(Path(self.result['developer']['result']['workspace']).exists())

    def test_publish_failure_retains_staging_and_originals(self):
        before = self.snapshot()
        with patch.object(Path, 'rename', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                self.write()
        self.assert_originals(before)
        journal = json.loads((self.flow/'cleanup.json').read_text())
        self.assertTrue((self.root/journal['staging']/'manifest.json').exists())
        self.assertEqual(journal['state'], 'ARCHIVE_FAILED')

    def test_final_journal_failure_does_not_claim_ready(self):
        before = self.snapshot()
        original = writer.atomic_json
        def fail_ready(path, data):
            if data.get('state') == 'ARCHIVE_READY':
                raise OSError('journal failed')
            return original(path, data)
        with patch.object(writer, 'atomic_json', side_effect=fail_ready):
            with self.assertRaisesRegex(OSError, 'journal failed'):
                self.write()
        self.assert_originals(before)
        self.assertTrue((self.flow/'archive').is_dir())
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            flow_archive.load_snapshot(self.root, 'sample')

    def test_corrupt_blob_is_detected_and_pending_snapshot_blocks_management(self):
        result = self.write()
        digest = result['entries'][0]['sha256']
        (self.flow/'archive/blobs'/digest).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'FLOW_ARCHIVE_PENDING'):
            role_manage.promote(self.root, 'sample', 'developer', self.result['developer']['task'], ['src/x.py'], True)
        with self.assertRaisesRegex(ValueError, 'digest|size'):
            flow_archive.load_snapshot(self.root, 'sample')

    def test_binary_and_empty_test_log_bytes_are_preserved(self):
        for data in (b'\xff\x00\r\n', b''):
            with self.subTest(data=data):
                # Each archive uses a separate flow; no archive is overwritten.
                name = 'binary' if data else 'empty'
                self.run_flow(name)
                flow = self.root/'.role-flows'/name
                (flow/'test-0.log').write_bytes(data)
                report = fixtures.flow_cleanup.preview(self.root, name, 1, now=self.now)
                writer.write_archive(self.root, name, report['plan_hash'], 1, now=self.now)
                evidence = flow_archive.read_evidence(self.root, name, 'flow_metadata', 'test-0.log')
                self.assertEqual(evidence['encoding'], 'base64' if data else 'utf-8')
                self.assertEqual((flow/'test-0.log').read_bytes(), data)

    def test_failed_checks_archive_has_no_reviewer(self):
        self.policy['checks'] = [[sys.executable, '-c', 'raise SystemExit(3)']]
        atomic_json(self.root/'role_workflow.json', self.policy)
        self.run_flow('failed')
        report = fixtures.flow_cleanup.preview(self.root, 'failed', 1, now=self.now)
        result = writer.write_archive(self.root, 'failed', report['plan_hash'], 1, now=self.now)
        self.assertIsNone(result['records']['roles']['reviewer'])
        self.assertEqual(result['records']['original_phase'], 'TESTS_FAILED')

    def test_cli_requires_explicit_apply_and_reads_installed_snapshot(self):
        (self.root/'role_workflow.json').unlink()
        connect_project.install(connect_project.plan_install(self.root, sys.executable), True)
        self.assertTrue((self.root/'flow_archive_writer.py').is_file())
        plan = self.preview()['plan_hash']
        with patch.object(sys, 'argv', ['role_manage', '--project', str(self.root), 'archive-flow', '--id', 'sample', '--days', '1', '--plan-hash', plan, '--apply']), patch.object(fixtures.flow_cleanup.time, 'time', return_value=self.now), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(role_manage.main(), 0)
        command = [sys.executable, '-B', str(self.root/'bridge.py'), 'role-manage', 'archive-flow', '--id', 'sample']
        before = self.snapshot()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        response = subprocess.run(command, env=env, capture_output=True, timeout=30)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertTrue(json.loads(response.stdout)['originals_retained'])
        evidence = subprocess.run(command + ['--evidence-source', 'flow_metadata', '--evidence-path', 'test-0.log'],
                                  env=env, capture_output=True, timeout=30)
        self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
        self.assertIn('CHECK_OK', json.loads(evidence.stdout)['content'])
        self.assertEqual(before, self.snapshot())
        with patch.object(sys, 'argv', ['role_manage', '--project', str(self.root), 'archive-flow', '--id', 'sample', '--apply']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(role_manage.main(), 2)

    def test_private_queue_bytes_and_usage_are_retained(self):
        fixtures.FlowCleanupTests.test_private_queues_preserved_and_active_peer_blocks_management(self)
        with fixtures.Store(self.flow/'review-input/.agent-bridge').connect() as db:
            db.execute("UPDATE tasks SET state='DONE'")
        before = self.snapshot()
        result = self.write()
        self.assertEqual(result['records']['queue_mode'], 'PRIVATE')
        self.assert_originals(before)

    def test_rolled_back_backup_evidence_is_retained(self):
        fixtures.FlowCleanupTests.test_backups_are_protected_until_rolled_back(self)
        before = self.snapshot()
        result = self.write()
        self.assertTrue(any('backup-' in entry['path'] for entry in result['entries']))
        self.assert_originals(before)

    def test_changed_database_during_archive_is_rejected(self):
        original = writer.durable_write
        changed = False
        def mutate(path, data):
            nonlocal changed
            original(path, data)
            if not changed:
                changed = True
                with fixtures.Store(self.base['state']).connect() as db:
                    db.execute('UPDATE control SET calls_started=calls_started+1')
        with patch.object(writer, 'durable_write', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'PLAN_CHANGED'):
                self.write()
        self.assertFalse((self.flow/'archive').exists())

    def test_corruption_before_publication_is_rejected(self):
        original = writer.durable_write
        def corrupt(path, data):
            original(path, data)
            if path.parent.name == 'blobs':
                path.write_bytes(b'corrupt')
        with patch.object(writer, 'durable_write', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'digest'):
                self.write()
        self.assertFalse((self.flow/'archive').exists())

    def test_metadata_corruption_before_publication_is_rejected(self):
        original = writer.durable_write
        def corrupt(path, data):
            original(path, data)
            if path.name == 'records.json':
                path.write_bytes(b'{}')
        with patch.object(writer, 'durable_write', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'metadata changed'):
                self.write()
        self.assertFalse((self.flow/'archive').exists())


if __name__ == '__main__':
    unittest.main()
