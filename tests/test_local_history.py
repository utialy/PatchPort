"""Local request bodies participate in existing history and connection fences."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import connection_guard, connection_remove, interactive_guard as guard, management, project_cleanup, project_data
from agent_bridge.storage import atomic_json, load_config


class LocalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.path = self.project / 'state' / 'interactive.sqlite3'
        self.api = management.Session()
        self.addCleanup(self.api.close)
        self.serial = 0
        ready = self.call('history.prepare.preview', **self.scope())
        self.call('history.apply', plan_id=ready['plan_id'], apply=True)
        upgrade = self.call('local.schema.preview', **self.scope())
        self.call('local.schema.apply', plan_id=upgrade['plan_id'], apply=True)

    def scope(self, **extra):
        return dict(database=str(self.path), project=str(self.project), **extra)

    def request(self, action, **args):
        self.serial += 1
        return self.api.handle(dict(protocol=1, id=str(self.serial), action=action, args=args))

    def call(self, action, **args):
        reply = self.request(action, **args)
        self.assertTrue(reply['ok'], reply)
        return reply['result']

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()

    def finished(self, sid='s1', project=None):
        project = project or self.project
        owner = 'owner-' + sid
        self.call('interactive.owner.open', database=str(self.path), project=str(project), owner=owner, apply=True)
        self.call('interactive.begin', database=str(self.path), project=str(project), owner=owner, session_id=sid,
                  node='node-' + sid, provider='local', name='PRIVATE_NAME', apply=True)
        request_id = 'request-' + sid
        binding = json.dumps(dict(model='PRIVATE_MODEL', endpoint='http://127.0.0.1:1234', profile='PRIVATE_PROFILE'))
        request_json = json.dumps(dict(messages=[dict(role='user', content='PRIVATE_QUESTION')]))
        response = 'PRIVATE_ANSWER_\uD55C\uAE00'
        with guard.connection(self.path, '@admin') as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO interactive_local_requests(id,session,state,created,dispatched,finished,binding_sha256,body_sha256,grant_sha256,origin,delivery_id,root,hops,reason,redacted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id, sid, 'COMPLETED', 1, 2, 3, 'b' * 64, 'c' * 64, 'd' * 64, 'human', None, request_id, 0, '', 0))
            db.execute('INSERT INTO interactive_local_bodies(request,binding,request_json,response) VALUES (?,?,?,?)',
                       (request_id, binding, request_json, response))
            db.execute('INSERT INTO interactive_local_usage(request,data) VALUES (?,?)',
                       (request_id, json.dumps(dict(input_tokens=10, output_tokens=5, cost_usd=None))))
        self.call('interactive.record', database=str(self.path), owner=owner, session_id=sid, event_id='stop-' + sid,
                  kind='session_stopped', data=dict(confirmed_exit=True), apply=True)
        self.call('interactive.end', database=str(self.path), owner=owner, apply=True)
        return request_id, sum(len(value.encode('utf-8')) for value in (binding, request_json, response))

    def preview(self, ids=None):
        return self.call('history.purge.preview', **self.scope(session_ids=ids or ['s1']))

    def apply(self, plan):
        return self.call('history.apply', plan_id=plan['plan_id'], apply=True)

    def test_purge_removes_local_binding_messages_and_answer_but_preserves_usage_control(self):
        request_id, body_bytes = self.finished()
        other = self.project / 'other'
        other.mkdir()
        self.finished('other', other)
        usage_before = self.rows('interactive_local_usage')
        requests_before = self.rows('interactive_local_requests')
        other_before = self.rows('interactive_local_bodies')[0]
        plan = self.preview()
        self.assertEqual(plan['report']['local_requests'], 1)
        self.assertEqual(plan['report']['local_body_bytes'], body_bytes)
        result = self.apply(plan)
        self.assertEqual(result['local_request_ids'], [request_id])
        self.assertEqual(usage_before, self.rows('interactive_local_usage'))
        bodies = dict((row[0], row[1:]) for row in self.rows('interactive_local_bodies'))
        self.assertEqual(bodies[request_id], ('', '', ''))
        self.assertEqual(self.rows('interactive_local_bodies')[0], other_before)
        after = self.rows('interactive_local_requests')
        self.assertEqual([row[:-1] for row in requests_before], [row[:-1] for row in after])
        self.assertEqual(dict((row[0], row[-1]) for row in after)[request_id], 1)
        self.assertTrue(self.call('history.operation', **self.scope(operation=plan['plan_id']))['found'])
        serialized = json.dumps(self.call('history.list', **self.scope()))
        self.assertNotIn('PRIVATE_', serialized)

    def test_inventory_counts_utf8_bytes_without_exposing_local_payload(self):
        _, body_bytes = self.finished()
        before = self.path.read_bytes()
        report = project_data.database_info(self.path, self.project)
        self.assertEqual(report['status'], 'present')
        self.assertEqual(report['interactive']['local_requests'], 1)
        self.assertEqual(report['interactive']['local_bodies'], 1)
        self.assertEqual(report['interactive']['local_body_bytes'], body_bytes)
        self.assertEqual(report['interactive']['local_usage'], 1)
        self.assertNotIn('PRIVATE_', json.dumps(report))
        self.assertEqual(self.path.read_bytes(), before)
        self.apply(self.preview())
        self.assertEqual(project_data.database_info(self.path, self.project)['interactive']['local_body_bytes'], 0)

    def test_changed_local_body_or_usage_invalidates_purge_plan(self):
        self.finished()
        for statement in ("UPDATE interactive_local_bodies SET response='CHANGED'", "UPDATE interactive_local_usage SET data='{}'"):
            with self.subTest(statement=statement):
                plan = self.preview()
                with guard.connection(self.path, '@admin') as db:
                    db.execute(statement)
                result = self.request('history.apply', plan_id=plan['plan_id'], apply=True)
                self.assertFalse(result['ok'])
                self.assertEqual(result['problem']['code'], 'PLAN_CHANGED')
                self.assertEqual(self.rows('interactive_local_requests')[0][-1], 0)

    def test_pending_request_blocks_purge_even_with_stopped_session(self):
        self.finished()
        with guard.connection(self.path, '@admin') as db:
            db.execute("UPDATE interactive_local_requests SET state='DISPATCH_AUTHORIZED',finished=NULL")
        preview = self.preview()
        self.assertIsNone(preview['plan_id'])
        self.assertIn('LOCAL_REQUEST_PENDING', preview['report']['blockers'])
        listing = self.call('history.list', **self.scope())
        self.assertFalse(listing['items'][0]['selectable'])
        self.assertEqual(listing['items'][0]['local_pending'], 1)
        report = project_data.inspect(self.project, databases=[self.path])
        self.assertIn('LOCAL_REQUEST_PENDING', report['blockers'])

    def test_active_and_lost_owner_still_block_schema_three_purge(self):
        self.finished()
        self.call('interactive.owner.open', **self.scope(owner='remaining-owner', apply=True))
        self.assertIn('WRITER_ACTIVE', self.preview()['report']['blockers'])
        self.call('interactive.begin', **self.scope(owner='remaining-owner', session_id='unfinished', node='other-node',
                                                  provider='local', name='Waiting', apply=True))
        self.call('interactive.end', database=str(self.path), owner='remaining-owner', apply=True)
        self.assertIn('WRITER_LOST', self.preview()['report']['blockers'])

    def test_missing_local_body_is_unknown_and_never_claimed_as_purged(self):
        self.finished()
        with guard.connection(self.path, '@admin') as db:
            db.execute('DELETE FROM interactive_local_bodies')
        plan = self.preview()
        self.assertIsNone(plan['plan_id'])
        self.assertIn('LOCAL_BODY_SCOPE_UNKNOWN', plan['report']['blockers'])
        report = project_data.database_info(self.path, self.project)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(report['reason'], 'LOCAL_BODY_SCOPE_UNKNOWN')

    def test_deleted_local_body_cannot_be_rewritten_through_unleased_connection(self):
        self.finished()
        self.apply(self.preview())
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            for statement in ("UPDATE interactive_local_bodies SET response='REVIVED'", "UPDATE interactive_local_requests SET redacted=0", "UPDATE interactive_local_usage SET data='{}'"):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                    db.execute(statement)
                db.rollback()
        self.assertEqual(self.rows('interactive_local_bodies')[0][1:], ('', '', ''))

    def test_receipt_failure_rolls_back_local_body_and_tombstone_together(self):
        self.finished()
        plan = self.preview()
        before = {table: self.rows(table) for table in ('interactive_local_requests', 'interactive_local_bodies', 'interactive_local_usage')}
        real = guard.connection

        @contextlib.contextmanager
        def failing(*args, **kwargs):
            with real(*args, **kwargs) as db:
                class Proxy:
                    def execute(self, sql, *params):
                        if sql.startswith('INSERT INTO interactive_redactions'):
                            raise sqlite3.OperationalError('fixture failure')
                        return db.execute(sql, *params)
                yield Proxy()

        with patch.object(guard, 'connection', failing):
            result = self.request('history.apply', plan_id=plan['plan_id'], apply=True)
        self.assertFalse(result['ok'])
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)
        self.assertFalse(self.call('history.operation', **self.scope(operation=plan['plan_id']))['found'])

    def test_connection_fence_covers_all_local_tables_even_with_interactive_admin(self):
        self.finished()
        self.apply(self.preview())
        with connection_guard.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            connection_guard.freeze(db, self.project, 'disconnect')
            connection_guard.validate(db)
        with guard.connection(self.path, '@admin') as db:
            for statement in ("UPDATE interactive_local_bodies SET response='REVIVED'", "UPDATE interactive_local_requests SET redacted=0", "UPDATE interactive_local_usage SET data='{}'"):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.Error, 'CONNECTION_REMOVED'):
                    db.execute(statement)
        self.assertEqual(self.call('history.list', **self.scope())['schema'], 3)

    def test_connection_removal_checks_local_body_even_after_session_tombstone(self):
        self.finished()
        ledgers = [dict(path=self.path, identity=connection_remove.identity(self.path), primary=False)]
        before, _ = connection_remove.blockers(self.project, self.path.parent, ledgers)
        self.assertIn('BODY_CLEANUP_REQUIRED', before)
        self.assertNotIn('HISTORY_PREPARATION_REQUIRED', before)
        self.apply(self.preview())
        after, _ = connection_remove.blockers(self.project, self.path.parent, ledgers)
        self.assertNotIn('BODY_CLEANUP_REQUIRED', after)
        with guard.connection(self.path, '@admin') as db:
            db.execute("UPDATE interactive_local_bodies SET response='INCOMPLETE_REDACTION'")
        partial, _ = connection_remove.blockers(self.project, self.path.parent, ledgers)
        self.assertIn('BODY_CLEANUP_REQUIRED', partial)

    @unittest.skipUnless(os.name == 'nt', 'Queue body preparation is Windows-only')
    def test_guided_cleanup_residuals_include_schema_three_bodies(self):
        self.finished()
        (self.project / 'input.txt').write_text('ORIGINAL', encoding='utf-8')
        config_path = self.project / 'bridge.json'
        atomic_json(config_path, dict(project='.', include=['input.txt'], writable=['input.txt'],
                                     endpoints={'mock': dict(adapter='command', command=[sys.executable, '-c', 'pass'])}))
        config = load_config(config_path)
        plan = self.call('records.prepare.preview', project=str(self.project), config=str(config_path))
        self.call('records.apply', plan_id=plan['plan_id'], apply=True)
        data = dict(project=str(self.project), database=str(config['state'] / 'queue.sqlite3'), databases=[str(self.path)])
        self.assertEqual(project_cleanup.residual(data)['history_bodies'], 1)
        self.apply(self.preview())
        self.assertEqual(project_cleanup.residual(data)['history_bodies'], 0)
        with guard.connection(self.path, '@admin') as db:
            db.execute("UPDATE interactive_local_bodies SET binding='INCOMPLETE_REDACTION'")
        self.assertEqual(project_cleanup.residual(data)['history_bodies'], 1)


if __name__ == '__main__':
    unittest.main()
