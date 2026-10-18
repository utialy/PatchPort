import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import interactive, management, project_data
from agent_bridge.storage import FileLock, Store


class ProjectDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        self.config = self.project / 'bridge.json'
        self.state = self.project / '.agent-bridge'
        self.serial = 0
        self.api = management.Session()

    def configure(self):
        (self.project / 'input.txt').write_text('SOURCE_SECRET', encoding='utf-8')
        self.config.write_text(json.dumps(dict(project='.', include=['input.txt'], writable=['input.txt'],
                                              endpoints={'mock': dict(adapter='command', command=[sys.executable, 'COMMAND_SECRET'])})), encoding='utf-8')

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob('*')}

    def call(self, **args):
        self.serial += 1
        return self.api.handle(dict(protocol=1, id=str(self.serial), action='project.data.inspect',
                                    args=dict(project=str(self.project), **args)))

    def test_unlinked_and_linked_empty_inspection_does_not_create_state(self):
        for configured in (False, True):
            if configured:
                self.configure()
            before = self.snapshot()
            reply = self.call()
            self.assertTrue(reply['ok'], reply)
            self.assertEqual(reply['result']['config']['status'], 'present' if configured else 'absent')
            self.assertEqual(reply['result']['blockers'], [])
            self.assertEqual(before, self.snapshot())
            self.assertFalse(self.state.exists())

    def test_all_counts_and_project_scope_without_exposing_contents(self):
        self.configure()
        store = Store(self.state)
        with store.connect() as db:
            for index in range(123):
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)',
                           (str(index), 'batch', 'mock', 'PROMPT_SECRET', 'DONE', 1, 2, 3, '{"answer":"ANSWER_SECRET"}'))
        database = store.db
        interactive.initialize(database)
        other = self.root / 'other'
        other.mkdir()
        for name, project in [('mine', self.project), ('other', other)]:
            interactive.begin(database, name, name, str(project), 'claude', 'NAME_SECRET', 'owner')
            interactive.record(database, name, name + '-event', 'session_stopped', data={'prompt': 'BODY_SECRET'})
            interactive.delivery(database, 'owner', {'id': name, 'status': 'completed', 'project': str(project), 'text': 'PEER_SECRET'})
        before = self.snapshot()
        reply = self.call(databases=[str(database), str(database)])
        self.assertTrue(reply['ok'], reply)
        report = reply['result']
        self.assertEqual(len(report['databases']), 1)
        row = report['databases'][0]
        self.assertEqual(row['batch']['tasks'], 123)
        self.assertEqual(row['interactive']['sessions'], 1)
        self.assertEqual(row['interactive']['events'], 1)
        self.assertEqual(row['interactive']['deliveries'], 1)
        self.assertEqual(row['interactive']['other_project_sessions'], 1)
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(before, self.snapshot())

    def test_batch_and_fallback_remain_separate_and_active_records_block(self):
        self.configure()
        store = Store(self.state)
        store.submit('queued', ['mock'], 'request', ['mock'])
        fallback = self.root / 'ledger' / 'interactive.sqlite3'
        interactive.initialize(fallback)
        interactive.begin(fallback, 'mine', 'node', str(self.project), 'codex', 'name', 'owner')
        before = self.snapshot()
        report = self.call(databases=[str(fallback)])['result']
        self.assertEqual(len(report['databases']), 2)
        self.assertIn('INTERACTIVE_UNFINISHED', report['blockers'])
        self.assertIn('BATCH_PENDING_OR_RUNNING', report['blockers'])
        self.assertEqual(before, self.snapshot())

    def test_legacy_tables_are_not_created_and_unknown_usage_is_not_zero(self):
        self.configure()
        self.state.mkdir()
        database = self.state / 'queue.sqlite3'
        with contextlib.closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE tasks (id TEXT,state TEXT)')
            db.execute("INSERT INTO tasks VALUES ('old','DONE')")
            db.commit()
        before = self.snapshot()
        row = self.call()['result']['databases'][0]
        self.assertEqual(row['batch']['tasks'], 1)
        self.assertIsNone(row['batch']['usage'])
        self.assertIsNone(row['interactive'])
        self.assertEqual(before, self.snapshot())

    def test_corrupt_database_and_sidecar_are_unknown_without_repair(self):
        self.configure()
        store = Store(self.state)
        for corrupt in (False, True):
            sidecar = Path(str(store.db) + '-wal')
            if corrupt:
                sidecar.unlink()
                store.db.write_bytes(b'broken')
            else:
                sidecar.write_bytes(b'pending')
            before = self.snapshot()
            row = self.call()['result']['databases'][0]
            self.assertEqual(row['status'], 'unknown')
            self.assertIsNone(row['batch'])
            self.assertEqual(before, self.snapshot())

    def test_detached_never_loads_default_config_or_queue(self):
        self.configure()
        self.config.write_text('{invalid', encoding='utf-8')
        before = self.snapshot()
        report = self.call(detached=True)['result']
        self.assertTrue(report['detached'])
        self.assertEqual(report['databases'], [])
        self.assertEqual(report['blockers'], [])
        self.assertEqual(before, self.snapshot())

    def test_invalid_stored_task_state_is_unknown_without_repair(self):
        self.configure()
        store = Store(self.state)
        with store.connect() as db:
            db.execute("INSERT INTO tasks (id,state) VALUES ('broken',NULL)")
        before = self.snapshot()
        report = self.call()['result']
        self.assertEqual(report['databases'][0]['status'], 'unknown')
        self.assertIn('DATABASE_UNKNOWN', report['blockers'])
        self.assertEqual(before, self.snapshot())

    def test_mismatched_or_broken_configuration_does_not_claim_ownership(self):
        self.configure()
        value = json.loads(self.config.read_text())
        value['project'] = '..'
        self.config.write_text(json.dumps(value), encoding='utf-8')
        report = self.call()['result']
        self.assertIn('CONFIG_UNKNOWN', report['blockers'])
        self.assertEqual(report['databases'], [])

    def test_scan_and_database_limits_are_explicit_unknown(self):
        self.configure()
        Store(self.state)
        artifacts = self.state / 'workspaces'
        artifacts.mkdir()
        (artifacts / 'large.txt').write_text('data')
        before = self.snapshot()
        with patch.object(project_data, 'MAX_ENTRIES', 1), patch.object(project_data, 'MAX_DATABASE_BYTES', 1):
            report = self.call()['result']
        self.assertEqual(report['databases'][0]['reason'], 'DATABASE_SIZE_LIMIT')
        item = next(item for item in report['artifacts'] if item['kind'] == 'task_copies')
        self.assertEqual(item['reason'], 'SCAN_LIMIT')
        self.assertIsNone(item['bytes'])
        self.assertEqual(before, self.snapshot())

    def test_hardlinked_artifact_and_database_are_never_followed(self):
        self.configure()
        Store(self.state)
        outside = self.root / 'outside'
        outside.write_text('SECRET')
        artifacts = self.project / '.role-flows'
        artifacts.mkdir()
        os.link(outside, artifacts / 'linked')
        report = self.call()['result']
        self.assertEqual(report['artifacts'][0]['status'], 'unknown')
        self.assertIsNone(report['artifacts'][0]['bytes'])

    def test_runner_lock_and_unconfirmed_job_are_reported_without_changes(self):
        self.configure()
        self.state.mkdir()
        jobs = self.state / 'p1-jobs'
        jobs.mkdir()
        (jobs / 'job.json').write_text('{"state":"ACCEPTED"}', encoding='utf-8')
        lock = self.state / 'runner.lock'
        lock.write_bytes(b'0')
        before = self.snapshot()
        with FileLock(lock):
            report = self.call()['result']
            self.assertIn('RUNNER_OR_MANAGEMENT_ACTIVE', report['blockers'])
            self.assertIn('ROLE_JOB_UNCONFIRMED', report['blockers'])
        self.assertEqual(before, self.snapshot())

    def test_invalid_api_arguments_are_rejected_without_writes(self):
        before = self.snapshot()
        for values in (dict(databases='bad'), dict(databases=['relative']), dict(databases=[str(self.root / 'secret.json')]),
                       dict(databases=[str(self.root / 'queue.sqlite3')] * 5), dict(detached=1), dict(apply=True)):
            self.assertFalse(self.call(**values)['ok'], values)
        self.assertEqual(before, self.snapshot())

    def test_inventory_revision_changes_when_records_or_configuration_change(self):
        self.configure()
        before = self.call()['result']['revision']
        Store(self.state)
        self.assertNotEqual(before, self.call()['result']['revision'])


if __name__ == '__main__':
    unittest.main()
