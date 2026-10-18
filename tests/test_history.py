import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_bridge import history, interactive, interactive_guard as guard, management
from agent_bridge.storage import FileLock, Store, probe_lock


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.path = self.project / 'state' / 'interactive.sqlite3'
        self.api = management.Session()
        self.addCleanup(self.api.close)
        self.serial = 0

    def request(self, action, **args):
        self.serial += 1
        return self.api.handle(dict(protocol=1, id=str(self.serial), action=action, args=args))

    def call(self, action, **args):
        reply = self.request(action, **args)
        self.assertTrue(reply['ok'], reply)
        return reply['result']

    def scoped(self, **extra):
        return dict(database=str(self.path), project=str(self.project), **extra)

    def open(self, owner='owner', project=None):
        return self.call('interactive.owner.open', database=str(self.path), project=str(project or self.project), owner=owner, apply=True)

    def begin(self, sid='s1', owner='owner', project=None):
        return self.call('interactive.begin', database=str(self.path), project=str(project or self.project), session_id=sid, node='node-'+sid, provider='claude', name='PRIVATE_NAME', owner=owner, apply=True)

    def record(self, sid='s1', kind='completed', data=None, owner='owner', **extra):
        return self.call('interactive.record', database=str(self.path), owner=owner, session_id=sid, event_id='e'+str(self.serial+1), kind=kind, data=data or dict(answer='PRIVATE_BODY'), apply=True, **extra)

    def stop(self, sid='s1', owner='owner'):
        self.record(sid, 'session_stopped', dict(confirmed_exit=True), owner)

    def end(self, owner='owner'):
        return self.call('interactive.end', database=str(self.path), owner=owner, apply=True)

    def finished(self):
        self.open(); self.begin(); self.record(); self.stop(); self.end()

    def preview(self, ids=None):
        return self.call('history.purge.preview', **self.scoped(session_ids=ids or ['s1']))

    def apply(self, plan):
        return self.call('history.apply', plan_id=plan['plan_id'], apply=True)

    def rows(self, table):
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()

    def test_missing_reads_and_previews_create_nothing(self):
        before = list(self.project.iterdir())
        self.assertEqual(self.call('history.list', **self.scoped())['schema'], 0)
        plan = self.call('history.prepare.preview', **self.scoped())
        self.assertTrue(plan['report']['eligible'])
        self.assertFalse(self.call('history.operation', **self.scoped(operation='missing'))['found'])
        self.assertEqual(before, list(self.project.iterdir()))
        self.apply(plan)
        self.assertEqual(self.call('history.list', **self.scoped())['schema'], 2)

    def test_schema_one_explicit_preparation_preserves_legacy_and_blocks_old_connection(self):
        interactive.initialize(self.path)
        interactive.begin(self.path, 'legacy', 'node', self.project, 'claude', 'OLD_BODY', 'old')
        interactive.record(self.path, 'legacy', 'old-e', 'session_stopped', data={})
        old = sqlite3.connect(self.path); self.addCleanup(old.close)
        before = [self.rows(table) for table in ('interactive_sessions', 'interactive_events')]
        self.assertEqual(self.request('interactive.owner.open', **self.scoped(owner='new', apply=True))['problem']['code'], 'UPGRADE_REQUIRED')
        plan = self.call('history.prepare.preview', **self.scoped())
        self.apply(plan)
        self.assertEqual(before, [self.rows(table) for table in ('interactive_sessions', 'interactive_events')])
        self.assertEqual(self.call('history.list', **self.scoped())['items'][0]['reason'], 'LEGACY_SESSION')
        with self.assertRaises(sqlite3.Error): old.execute("UPDATE interactive_sessions SET name='rewritten'")
        old.rollback()
        self.assertIn('LEGACY_SESSION', self.preview(['legacy'])['report']['blockers'])
        self.open(); self.begin(); self.stop(); self.end()
        self.assertTrue(self.preview()['report']['eligible'])

    def test_legacy_unconfirmed_or_pending_prevents_preparation(self):
        interactive.initialize(self.path)
        interactive.begin(self.path, 'old', 'node', self.project, 'claude', 'Old', 'old')
        self.assertFalse(self.call('history.prepare.preview', **self.scoped())['report']['eligible'])
        interactive.end_owner(self.path, 'old')
        self.assertFalse(self.call('history.prepare.preview', **self.scoped())['report']['eligible'])
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE interactive_sessions SET state='STOPPED'"); db.commit()
        interactive.delivery(self.path, 'old', dict(id='old-delivery', status='queued', text='OLD_BODY'))
        self.assertFalse(self.call('history.prepare.preview', **self.scoped())['report']['eligible'])

    def test_purge_preserves_usage_ids_batch_controls_and_other_project(self):
        store = Store(self.path.parent); self.path = store.db
        store.submit('job', ['peer'], 'BATCH_BODY', {'peer': {}})
        batch_before, control_before = self.rows('tasks'), store.control()
        self.open(); self.begin(); self.record()
        self.record(kind='usage', data=dict(sequence=1, metrics=dict(input_tokens=10, cost_usd=0.1), source='fixture'))
        self.record(kind='session_started', data=dict(provider_session='11111111-1111-4111-8111-111111111111'))
        self.stop(); self.end()
        other = self.project / 'other'; other.mkdir()
        self.open('other-owner', other); self.begin('other', 'other-owner', other); self.record('other', owner='other-owner')
        before = interactive.report(self.path, self.project)
        usage_before = self.rows('interactive_usage')
        other_before = interactive.report(self.path, other)
        result = self.apply(self.preview())
        self.assertEqual(result['node_ids'], ['node-s1'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        after = interactive.report(self.path, self.project)
        self.assertEqual(before['summary'], after['summary'])
        self.assertEqual(before['group_attempts'], after['group_attempts'])
        self.assertTrue(after['sessions'][0]['body_deleted'])
        self.assertNotIn('PRIVATE', json.dumps(after))
        self.assertEqual(before['sessions'][0]['id'], after['sessions'][0]['id'])
        self.assertEqual(other_before, interactive.report(self.path, other))
        self.assertEqual(usage_before, self.rows('interactive_usage'))
        self.assertEqual(batch_before, self.rows('tasks')); self.assertEqual(control_before, store.control())
        with self.assertRaisesRegex(guard.LedgerError, 'BODY_DELETED'): interactive.resume_info(self.path, self.project, 's1')
        for event in before['events']:
            read = interactive.read_event(self.path, self.project, event['id'])
            self.assertTrue(read['body_deleted']); self.assertEqual(read['data'], dict(body_deleted=True))

    def test_shared_delivery_requires_both_sessions_and_preserves_attempts(self):
        self.open()
        for sid in ('a', 'b'): self.begin(sid)
        data = dict(id='delivery', source='node-a', target='node-b', source_session='a', target_session='b', project=str(self.project), text='PRIVATE_BODY', reason='PRIVATE_REASON', groupId='group', round='round', root='root', hops=1, status='sent')
        self.call('interactive.delivery', database=str(self.path), owner='owner', data=data, apply=True)
        for sid in ('a', 'b'): self.stop(sid)
        self.end()
        half = self.preview(['a'])['report']
        self.assertEqual(half['required_sessions'], ['b']); self.assertFalse(half['eligible'])
        before = interactive.report(self.path, self.project)['group_attempts']
        result = self.apply(self.preview(['a', 'b']))
        self.assertEqual(result['delivery_ids'], ['delivery'])
        after = interactive.report(self.path, self.project)
        self.assertEqual(before, after['group_attempts']); self.assertEqual(before['group:round'], 1)
        self.assertTrue(after['deliveries'][0]['body_deleted']); self.assertNotIn('PRIVATE', json.dumps(after))

    def test_legacy_ambiguous_delivery_is_not_guessed(self):
        interactive.initialize(self.path)
        interactive.delivery(self.path, 'old', dict(id='legacy', status='completed', text='PRIVATE'))
        self.apply(self.call('history.prepare.preview', **self.scoped()))
        self.finished()
        self.assertIn('DELIVERY_SCOPE_UNKNOWN', self.preview()['report']['blockers'])

    def test_active_owner_and_kill_without_exit_proof_block_purge(self):
        self.open(); self.begin()
        self.assertEqual(probe_lock(guard.owner_path(self.path, 'owner')), 'LOCKED')
        self.assertIn('WRITER_ACTIVE', self.preview()['report']['blockers'])
        self.record(kind='session_stopped', data=dict(kill_requested=True))
        self.assertEqual(self.end()['state'], 'LOST')
        self.assertIn('WRITER_LOST', self.preview()['report']['blockers'])
        self.assertEqual(self.call('history.list', **self.scoped())['items'][0]['reason'], 'SESSION_EXIT_UNCONFIRMED')

    def test_connection_loss_releases_lock_but_never_confirms_exit(self):
        self.open(); self.begin(); self.api.close()
        self.assertEqual(probe_lock(guard.owner_path(self.path, 'owner')), 'FREE')
        self.assertIn('WRITER_LOST', self.preview()['report']['blockers'])
        self.assertFalse(self.request('interactive.owner.open', **self.scoped(owner='owner', apply=True))['ok'])

    def test_foreign_pipe_and_plain_sql_cannot_write(self):
        self.open(); self.begin()
        foreign = management.Session(); self.addCleanup(foreign.close)
        result = foreign.handle(dict(protocol=1, id='foreign', action='interactive.record', args=dict(database=str(self.path), owner='owner', session_id='s1', event_id='forged', kind='completed', data={}, apply=True)))
        self.assertEqual(result['problem']['code'], 'WRITER_LEASE_REQUIRED')
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            for statement in ("UPDATE interactive_sessions SET name='BODY'", "INSERT INTO interactive_events VALUES ('forged','s1','completed',NULL,1,'{}')"):
                with self.assertRaises(sqlite3.Error): db.execute(statement)

    def test_deleted_ids_cannot_be_reused_or_rewritten(self):
        self.finished(); self.apply(self.preview())
        self.open('new')
        self.assertFalse(self.request('interactive.begin', **self.scoped(session_id='s1', node='node', provider='claude', name='BODY', owner='new', apply=True))['ok'])
        self.assertFalse(self.request('interactive.record', database=str(self.path), owner='new', session_id='s1', event_id='new', kind='completed', data=dict(answer='BODY'), apply=True)['ok'])
        self.assertIn('ALREADY_DELETED', self.preview()['report']['blockers'])

    def test_damaged_or_extra_guard_refuses_reads_and_mutations(self):
        self.finished()
        with contextlib.closing(sqlite3.connect(self.path)) as db: db.execute("CREATE TRIGGER extra AFTER UPDATE ON interactive_events BEGIN SELECT 1; END")
        self.assertEqual(self.request('history.list', **self.scoped())['problem']['code'], 'LEDGER_GUARD_DAMAGED')
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TRIGGER extra'); db.execute('DROP TRIGGER bridge_guard_interactive_events_insert')
        self.assertEqual(self.request('history.purge.preview', **self.scoped(session_ids=['s1']))['problem']['code'], 'LEDGER_GUARD_DAMAGED')

    def test_plan_cancel_expiry_namespace_and_one_use_receipt(self):
        self.finished(); plan = self.preview()
        self.assertFalse(self.request('operation.apply', plan_id=plan['plan_id'], apply=True)['ok'])
        self.call('history.cancel', plan_id=plan['plan_id'])
        self.assertFalse(self.request('history.apply', plan_id=plan['plan_id'], apply=True)['ok'])
        plan = self.preview(); _, payload = self.api.plans[plan['plan_id']]
        self.api.plans[plan['plan_id']] = (time.monotonic()-1, payload)
        self.assertEqual(self.request('history.apply', plan_id=plan['plan_id'], apply=True)['problem']['code'], 'PLAN_EXPIRED')
        plan = self.preview(); applied = self.apply(plan)
        self.assertFalse(self.request('history.apply', plan_id=plan['plan_id'], apply=True)['ok'])
        receipt = self.call('history.operation', **self.scoped(operation=plan['plan_id']))
        self.assertEqual(receipt, dict(found=True, **applied))

    def test_new_owner_after_preview_invalidates_plan_without_purging(self):
        self.finished(); plan = self.preview(); self.open('new')
        result = self.request('history.apply', plan_id=plan['plan_id'], apply=True)
        self.assertEqual(result['problem']['code'], 'PLAN_CHANGED')
        self.assertFalse(interactive.report(self.path, self.project)['sessions'][0]['body_deleted'])

    def test_changed_database_identity_invalidates_plan(self):
        self.finished(); plan = self.preview()
        replacement = self.path.with_suffix('.replacement'); replacement.write_bytes(self.path.read_bytes()); replacement.replace(self.path)
        self.assertEqual(self.request('history.apply', plan_id=plan['plan_id'], apply=True)['problem']['code'], 'PLAN_CHANGED')

    def test_changed_body_invalidates_plan(self):
        self.finished(); plan = self.preview()
        with guard.connection(self.path, '@admin') as db: db.execute("UPDATE interactive_events SET data='{}'")
        self.assertEqual(self.request('history.apply', plan_id=plan['plan_id'], apply=True)['problem']['code'], 'PLAN_CHANGED')

    def test_transaction_failure_rolls_back_bodies_tombstones_and_receipt(self):
        self.finished(); plan = self.preview(); before = self.rows('interactive_events')
        real = guard.connection
        @contextlib.contextmanager
        def failing(*args, **kwargs):
            with real(*args, **kwargs) as db:
                class Proxy:
                    def execute(self, sql, *params):
                        if sql.startswith('INSERT INTO interactive_redactions'): raise sqlite3.OperationalError('fixture failure')
                        return db.execute(sql, *params)
                yield Proxy()
        with patch.object(guard, 'connection', failing):
            self.assertFalse(self.request('history.apply', plan_id=plan['plan_id'], apply=True)['ok'])
        self.assertEqual(before, self.rows('interactive_events'))
        self.assertFalse(self.rows('interactive_session_control')[0][3])
        self.assertFalse(self.call('history.operation', **self.scoped(operation=plan['plan_id']))['found'])

    def test_page_scope_and_unselected_body_preserved(self):
        self.open()
        for sid in ('a', 'b', 'c'): self.begin(sid); self.record(sid); self.stop(sid)
        self.end()
        page = self.call('history.list', **self.scoped(limit=2))
        self.assertEqual([s['id'] for s in page['items']], ['a', 'b'])
        self.assertTrue(page['more'])
        next_page = self.call('history.list', **self.scoped(limit=2, cursor=page['next_cursor']))
        self.assertEqual([s['id'] for s in next_page['items']], ['c'])
        self.apply(self.preview(['a']))
        report = interactive.report(self.path, self.project)
        self.assertEqual(sum(s['body_deleted'] for s in report['sessions']), 1)
        self.assertEqual(sum(e['data'].get('answer') == 'PRIVATE_BODY' for e in report['events']), 2)

    def test_closed_owner_with_held_lock_blocks_purge(self):
        self.finished()
        with FileLock(guard.owner_path(self.path, 'owner')):
            self.assertIn('OWNER_CLOSE_UNCONFIRMED', self.preview()['report']['blockers'])

    def test_request_capacity_reserves_bounded_exit_recording(self):
        self.open(); self.begin()
        with patch.object(management, 'MAX_REQUESTS', len(self.api.seen)):
            self.assertEqual(self.request('capabilities')['problem']['code'], 'SESSION_FULL')
            self.stop(); self.assertEqual(self.end()['state'], 'CLOSED')

    def test_owner_start_and_apply_race_never_redacts_an_active_writer(self):
        self.finished(); plan = self.preview(); payload = self.api.plans[plan['plan_id']][1]
        barrier = threading.Barrier(2)
        lease = guard.WriterLease(self.path, self.project, 'racing-owner')
        self.addCleanup(lease.release)
        def start():
            barrier.wait()
            try:
                lease.open()
                return True
            except RuntimeError:
                return False
        def purge():
            barrier.wait()
            try:
                return history.apply(payload, plan['plan_id'])
            except (RuntimeError, guard.LedgerError):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            opened, applied = pool.submit(start), pool.submit(purge)
            opened, applied = opened.result(), applied.result()
        if applied:
            self.assertTrue(interactive.report(self.path, self.project)['sessions'][0]['body_deleted'])
        else:
            self.assertFalse(interactive.report(self.path, self.project)['sessions'][0]['body_deleted'])
            self.assertFalse(history.operation(self.path, self.project, plan['plan_id'])['found'])
        # If both operations succeeded, the gate placed the complete purge before the new owner.
        if opened:
            self.assertIn('WRITER_ACTIVE', self.preview()['report']['blockers'])
            lease.close()


if __name__ == '__main__':
    unittest.main()
