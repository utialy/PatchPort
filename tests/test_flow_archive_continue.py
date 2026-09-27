import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_flow_archive_writer as fixtures
from agent_bridge.storage import FileLock
import flow_archive
import flow_archive_writer as writer
import role_manage


class ArchiveContinueTests(unittest.TestCase):
    setUp = fixtures.ArchiveWriterTests.setUp
    run_flow = fixtures.ArchiveWriterTests.run_flow
    preview = fixtures.ArchiveWriterTests.preview
    snapshot = fixtures.ArchiveWriterTests.snapshot
    write = fixtures.ArchiveWriterTests.write
    assert_originals = fixtures.ArchiveWriterTests.assert_originals

    def interrupt(self, phase='blob'):
        if phase == 'blob':
            target = patch.object(writer, 'durable_write', side_effect=OSError('interrupted'))
        elif phase == 'publish':
            target = patch.object(Path, 'rename', side_effect=OSError('interrupted'))
        else:
            original = writer.atomic_json
            def fail(path, data):
                if data.get('state') == 'ARCHIVE_READY':
                    raise OSError('interrupted')
                return original(path, data)
            target = patch.object(writer, 'atomic_json', side_effect=fail)
        with target, self.assertRaisesRegex(OSError, 'interrupted'):
            self.write()
        return json.loads((self.flow/'cleanup.json').read_text())

    def resume(self, journal, apply=False):
        return writer.continue_archive(self.root, 'sample', journal['operation'],
                                       journal['plan_hash'] if apply else None, apply=apply, now=self.now)

    def test_resume_partial_then_allow_management_with_historical_snapshot(self):
        original = self.snapshot()
        journal = self.interrupt()
        before = self.snapshot()
        report = self.resume(journal)
        self.assertTrue(report['eligible'])
        self.assertTrue(report['missing_files'])
        self.assertEqual(before, self.snapshot())
        done = self.resume(journal, True)
        self.assertEqual(done['artifact_state'], 'ARCHIVE_READY')
        self.assert_originals(original)
        task = self.result['developer']['task']
        role_manage.promote(self.root, 'sample', 'developer', task, ['src/x.py'], True)
        self.assertEqual((self.root/'src/x.py').read_text(), 'VALUE=2\n')
        self.assertEqual(flow_archive.load_snapshot(self.root, 'sample')['archive_digest'], done['archive_digest'])

    def test_resume_completed_staging(self):
        journal = self.interrupt('publish')
        self.assertEqual(self.resume(journal)['missing_files'], [])
        self.assertEqual(self.resume(journal, True)['artifact_state'], 'ARCHIVE_READY')

    def test_resume_published_archive_and_repeat_are_read_only(self):
        journal = self.interrupt('ready')
        self.assertTrue(self.resume(journal)['published'])
        self.resume(journal, True)
        before = self.snapshot()
        self.assertTrue(self.resume(journal, True)['already_complete'])
        self.assertEqual(before, self.snapshot())

    def test_wrong_operation_hash_or_source_refuses_without_writes(self):
        journal = self.interrupt()
        before = self.snapshot()
        for operation, digest in (('wrong', journal['plan_hash']), (journal['operation'], '0'*64), (journal['operation'], None)):
            with self.assertRaises(ValueError):
                writer.continue_archive(self.root, 'sample', operation, digest, apply=True, now=self.now)
        self.assertEqual(before, self.snapshot())
        (self.flow/'test-0.log').write_text('changed')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'PLAN_CHANGED'):
            self.resume(journal, True)
        self.assertEqual(before, self.snapshot())

    def test_unknown_staged_file_and_corrupt_blob_refuse(self):
        journal = self.interrupt('publish')
        staging = self.root/journal['staging']
        unknown = staging/'extra.txt'
        unknown.write_text('keep')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Unknown or corrupt'):
            self.resume(journal, True)
        self.assertEqual(before, self.snapshot())
        unknown.unlink()
        next((staging/'blobs').iterdir()).write_bytes(b'partial bytes')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Unknown or corrupt'):
            self.resume(journal, True)
        self.assertEqual(before, self.snapshot())

    def test_busy_lock_and_legacy_journal_refuse(self):
        journal = self.interrupt()
        with FileLock(self.base['state']/'runner.lock'):
            with self.assertRaisesRegex(ValueError, 'FLOW_PROTECTED'):
                self.resume(journal, True)
        journal.pop('resume_schema')
        fixtures.atomic_json(self.flow/'cleanup.json', journal)
        with self.assertRaisesRegex(ValueError, 'legacy'):
            self.resume(journal, True)

    def test_resume_failure_can_be_inspected_and_explicitly_retried(self):
        journal = self.interrupt()
        with patch.object(writer, 'durable_write', side_effect=OSError('full')), self.assertRaises(OSError):
            self.resume(journal, True)
        self.assertTrue(self.resume(journal)['eligible'])
        self.assertEqual(self.resume(journal, True)['artifact_state'], 'ARCHIVE_READY')

    def test_cli_continue_rejects_period_and_completes(self):
        journal = self.interrupt()
        args = ['role_manage', '--project', str(self.root), 'archive-flow', '--id', 'sample', '--continue', journal['operation']]
        with patch.object(sys, 'argv', args+['--days', '1']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(role_manage.main(), 2)
        with patch.object(sys, 'argv', args+['--plan-hash', journal['plan_hash'], '--apply']), patch.object(writer.flow_cleanup.time, 'time', return_value=self.now), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(role_manage.main(), 0)


if __name__ == '__main__':
    unittest.main()
