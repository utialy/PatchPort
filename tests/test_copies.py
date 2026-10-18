"""D2 read-only previews, guarded deletion, and durable partial outcomes."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_cleanup as batch_fixtures
import test_role_review as role_fixtures
import test_role_manage as role_helpers
import test_flow_cleanup as archive_fixtures
from agent_bridge import cleanup, management, workspace
from agent_bridge.storage import FileLock, Store, atomic_json
import role_manage


class Calls:
    def call(self, action, **args):
        if not hasattr(self, 'session'):
            self.session = management.Session()
            self.sequence = 0
        self.sequence += 1
        if action not in ('operation.apply', 'operation.cancel', 'capabilities'):
            args.setdefault('config', str(self.root / 'bridge.json'))
        return self.session.handle(dict(protocol=1, id=str(self.sequence), action=action, args=args))

    def success(self, action, **args):
        reply = self.call(action, **args)
        self.assertTrue(reply['ok'], reply)
        return reply['result']

    def apply(self, plan):
        return self.call('operation.apply', plan_id=plan['plan_id'], apply=True)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
                for p in self.root.rglob('*')}


class CopyBatchTests(Calls, unittest.TestCase):
    setUp = batch_fixtures.CleanupTests.setUp
    tearDown = batch_fixtures.CleanupTests.tearDown
    task = batch_fixtures.CleanupTests.task
    age = batch_fixtures.CleanupTests.age

    def preview(self, ids):
        return self.success('copies.prune.preview', ids=ids, days=30)

    def test_list_and_preview_do_not_create_locks_or_initialize_store(self):
        task, _ = self.task()
        before = self.snapshot()
        with patch.object(cleanup.Store, '__init__', side_effect=AssertionError('Unexpected Store initialization')):
            self.assertEqual(self.success('copies.list')['items'][0]['task_id'], task)
            self.assertTrue(self.preview([task])['plan_id'])
        self.assertEqual(before, self.snapshot())

    def test_pagination_and_role_exclusion_use_exact_task_ids(self):
        ids = [self.task('item-' + str(i))[0] for i in range(6)]
        with self.store.connect() as db:
            db.execute('INSERT INTO role_tasks VALUES (?,?,?,?)', (ids[-1], 'flow', 'developer', '{}'))
        found, cursor = [], ''
        while True:
            page = self.success('copies.list', limit=2, cursor=cursor)
            found.extend(item['task_id'] for item in page['items'])
            if not page['more']:
                break
            cursor = page['next_cursor']
        self.assertEqual(found, sorted(ids[:-1]))
        self.assertFalse(self.call('copies.prune.preview', ids=[ids[-1]], days=30)['ok'])

    def test_protected_selection_never_issues_a_partial_plan(self):
        first, home = self.task('old')
        recent, recent_home = self.task('recent')
        (recent_home / 'answer.txt').write_text('Recent')
        preview = self.preview([first, recent])
        self.assertIsNone(preview['plan_id'])
        self.assertEqual(preview['report']['items'][1]['artifact']['reason'], 'RETENTION_PERIOD')
        self.assertTrue(home.exists())

    def test_selected_cleanup_preserves_unselected_original_answer_usage_and_controls(self):
        selected, home = self.task('a')
        other, other_home = self.task('b')
        self.store.record_usage(selected, {'metrics': {'input_tokens': 17}})
        with self.store.connect() as db:
            usage = db.execute('SELECT * FROM usage_snapshots').fetchall()
        control = self.store.control()
        original = (self.root / 'src/base[1].txt').read_bytes()
        result = self.apply(self.preview([selected]))
        self.assertTrue(result['ok'], result)
        self.assertFalse(home.exists())
        self.assertTrue(other_home.exists())
        self.assertEqual(self.store.control(), control)
        self.assertEqual((self.root / 'src/base[1].txt').read_bytes(), original)
        answer = self.success('result', task_id=selected)
        self.assertEqual(answer['answer']['text'], 'answer')
        self.assertEqual(answer['artifact_cleanup']['state'], 'DELETED')
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM usage_snapshots')], [tuple(row) for row in usage])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)
        page = self.success('copies.list')
        self.assertEqual(page['items'][0]['artifact']['reason'], 'ALREADY_DELETED')

    def test_same_size_same_mtime_hidden_content_change_invalidates_plan(self):
        task, home = self.task()
        hidden = home / '.hidden'; hidden.mkdir(); secret = hidden / 'events.bin'; secret.write_bytes(b'ab')
        self.age(home)
        plan = self.preview([task])
        stamp = secret.stat().st_mtime_ns
        secret.write_bytes(b'cd'); os.utime(secret, ns=(stamp, stamp))
        self.assertFalse(self.apply(plan)['ok'])
        self.assertTrue(home.exists())
        self.assertEqual(self.apply(plan)['problem']['code'], 'PLAN_EXPIRED')

    def test_added_empty_directory_invalidates_plan(self):
        task, home = self.task()
        plan = self.preview([task]); stamp = home.stat().st_mtime_ns
        (home / 'extra').mkdir(); os.utime(home, ns=(stamp, stamp))
        self.assertFalse(self.apply(plan)['ok']); self.assertTrue(home.exists())

    def test_changed_artifacts_are_rechecked_after_acquiring_locks(self):
        task, home = self.task(); plan = self.preview([task])
        real = cleanup.FileLock
        @contextlib.contextmanager
        def changed(path):
            with real(path) as held:
                if Path(path).name == 'promotion.lock':
                    (home / 'events.jsonl').write_text('Changed under lock')
                yield held
        with patch.object(cleanup, 'FileLock', changed):
            self.assertFalse(self.apply(plan)['ok'])
        self.assertTrue(home.exists()); self.assertIsNone(self.store.artifact_status(task))

    def test_busy_runner_prevents_preview_and_later_apply(self):
        task, home = self.task(); plan = self.preview([task])
        with FileLock(self.c['state'] / 'runner.lock'):
            self.assertIsNone(self.preview([task])['plan_id'])
            self.assertFalse(self.apply(plan)['ok'])
        self.assertTrue(home.exists())

    def test_applied_backup_is_protected_regardless_of_age(self):
        task, home = self.task()
        (home / 'project/src/base[1].txt').write_text('Updated')
        workspace.changes(self.c, home)
        workspace.review(self.c, task, ['src/base[1].txt'])
        self.age(home)
        result = self.preview([task])
        self.assertIsNone(result['plan_id'])
        self.assertEqual(result['report']['items'][0]['artifact']['reason'], 'RECOVERY_DATA_PROTECTED')

    def test_hardlink_and_recorded_workspace_mismatch_are_not_deletion_candidates(self):
        task, home = self.task()
        link = home / 'linked'; os.link(self.root / 'src/base[1].txt', link); self.age(home)
        self.assertIsNone(self.preview([task])['plan_id'])
        link.unlink(); self.age(home)
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps({'workspace': str(self.root)}), task))
        self.assertIsNone(self.preview([task])['plan_id'])
        self.assertTrue(home.exists())

    def test_partial_file_failure_retains_completed_and_failed_outcomes(self):
        first, first_home = self.task('a'); second, second_home = self.task('b')
        plan = self.preview([first, second]); real = cleanup.shutil.rmtree
        def failed(path, *args, **kwargs):
            if Path(path) == second_home:
                raise PermissionError('Injected file error')
            return real(path, *args, **kwargs)
        with patch.object(cleanup.shutil, 'rmtree', failed):
            reply = self.apply(plan)
        self.assertEqual(reply['problem']['code'], 'OPERATION_INCOMPLETE')
        self.assertEqual([item['reason'] for item in reply['result']['tasks']], ['DELETED', 'DELETE_FAILED'])
        self.assertFalse(first_home.exists()); self.assertTrue(second_home.exists())
        self.assertEqual(self.store.artifact_status(second)['state'], 'DELETE_FAILED')
        self.assertEqual(self.success('result', task_id=second)['answer']['text'], 'answer')
        self.assertEqual(self.apply(plan)['problem']['code'], 'PLAN_EXPIRED')

    def test_answer_preservation_failure_never_deletes_the_copy(self):
        task, home = self.task(); (home / 'answer.txt').write_bytes(b'\xff'); self.age(home)
        reply = self.apply(self.preview([task]))
        self.assertEqual(reply['problem']['code'], 'OPERATION_INCOMPLETE')
        self.assertEqual(reply['result']['tasks'][0]['reason'], 'DELETE_FAILED')
        self.assertTrue(home.exists())

    def test_failed_completion_record_is_unknown_not_a_successful_zero(self):
        task, home = self.task(); plan = self.preview([task]); real = Store.mark_artifacts
        def failed(store, task_id, state):
            if state == 'DELETED':
                raise sqlite3.OperationalError('Injected commit failure')
            return real(store, task_id, state)
        with patch.object(Store, 'mark_artifacts', failed):
            reply = self.apply(plan)
        self.assertEqual(reply['problem']['code'], 'OPERATION_INCOMPLETE')
        self.assertIsNone(reply['result']['tasks'][0]['deleted'])
        self.assertIsNone(reply['result']['deleted_bytes'])
        self.assertFalse(home.exists())
        self.assertEqual(self.store.artifact_status(task)['state'], 'DELETING')

    def test_invalid_arguments_unknown_ids_and_cancel_are_non_destructive(self):
        task, home = self.task(); before = self.snapshot()
        for ids in ([], [task, task], ['../bad'], ['missing'], [task] * 26):
            self.assertFalse(self.call('copies.prune.preview', ids=ids, days=30)['ok'])
        for args in (dict(limit=0), dict(limit=True), dict(kind='any'), dict(cursor='../bad')):
            self.assertFalse(self.call('copies.list', **args)['ok'])
        plan = self.preview([task]); self.success('operation.cancel', plan_id=plan['plan_id'])
        self.assertEqual(self.apply(plan)['problem']['code'], 'PLAN_EXPIRED')
        self.assertEqual(before, self.snapshot()); self.assertTrue(home.exists())

    def test_legacy_database_preview_has_no_schema_migration(self):
        folder = self.root / 'legacy'; folder.mkdir()
        raw = json.loads(self.config_path.read_text()); raw['state'] = 'legacy'
        old_config = self.root / 'legacy.json'; atomic_json(old_config, raw)
        with contextlib.closing(sqlite3.connect(folder / 'queue.sqlite3')) as db:
            db.execute('CREATE TABLE tasks(id TEXT,state TEXT,finished REAL,result TEXT)')
            db.execute("INSERT INTO tasks VALUES ('old','DONE',1,'{}')"); db.commit()
        before = self.snapshot()
        self.assertEqual(self.success('copies.list', config=str(old_config))['items'][0]['artifact']['reason'], 'ARTIFACTS_MISSING')
        self.assertEqual(before, self.snapshot())
        home = folder / 'workspaces/old'; home.mkdir(parents=True)
        (home / 'answer.txt').write_text('Retain this unsupported legacy record')
        self.age(home); before = self.snapshot()
        unsupported = self.success('copies.prune.preview', config=str(old_config), ids=['old'], days=30)
        self.assertIsNone(unsupported['plan_id']); self.assertEqual(before, self.snapshot())


class CopyRoleTests(Calls, unittest.TestCase):
    setUp = role_fixtures.RoleReviewTests.setUp
    age = role_helpers.RoleManageTests.age
    args = role_helpers.RoleManageTests.args

    def preview(self):
        return self.success('roles.prune.preview', flow_id='sample', role='developer', task_id=self.result['developer']['task'], days=1)

    def test_shared_preview_is_readonly_and_keeps_reviewer_and_parent_controls(self):
        self.age(); before = self.snapshot()
        page = self.success('copies.list', kind='role', days=1, limit=1)
        self.assertTrue(page['more']); self.assertEqual(page['items'][0]['queue_mode'], 'SHARED_PARENT')
        second = self.success('copies.list', kind='role', days=1, cursor=page['next_cursor'], limit=1)
        self.assertEqual(second['items'][0]['role'], 'reviewer')
        plan = self.preview(); self.assertEqual(before, self.snapshot())
        store = Store(self.root / '.agent-bridge'); controls = store.control()
        self.assertTrue(self.apply(plan)['ok'])
        self.assertFalse(self.home.exists())
        self.assertTrue(Path(self.result['reviewer']['result']['workspace']).exists())
        self.assertEqual(store.control(), controls)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')

    def test_private_role_cleanup_updates_only_its_own_queue(self):
        state = self.flow / 'developer-state'
        with contextlib.closing(sqlite3.connect(self.root / '.agent-bridge/queue.sqlite3')) as source:
            with contextlib.closing(sqlite3.connect(state / 'queue.sqlite3')) as target:
                source.backup(target)
        self.result.pop('queue_mode'); self.result['developer'].pop('queue_state')
        atomic_json(self.flow / 'result.json', self.result); self.age()
        parent = (self.root / '.agent-bridge/queue.sqlite3').read_bytes()
        before = self.snapshot(); plan = self.preview()
        self.assertEqual(plan['report']['queue_mode'], 'PRIVATE'); self.assertEqual(before, self.snapshot())
        self.assertTrue(self.apply(plan)['ok'])
        self.assertEqual(parent, (self.root / '.agent-bridge/queue.sqlite3').read_bytes())
        self.assertEqual(Store(state).artifact_status(self.args()[-1])['state'], 'DELETED')

    def test_role_file_change_and_wrong_task_are_rejected(self):
        self.age(); plan = self.preview()
        (self.home / 'events.jsonl').write_text('new data')
        self.assertFalse(self.apply(plan)['ok']); self.assertTrue(self.home.exists())
        self.assertFalse(self.call('roles.prune.preview', flow_id='sample', role='developer', task_id='other', days=1)['ok'])

    def test_active_flow_and_incomplete_archive_are_protected(self):
        self.age(); self.result['state'] = 'TESTING'; atomic_json(self.flow / 'result.json', self.result)
        self.assertIsNone(self.preview()['plan_id'])
        self.result['state'] = 'REVIEW_DONE'; atomic_json(self.flow / 'result.json', self.result)
        atomic_json(self.flow / 'cleanup.json', {'state': 'PREPARING'})
        preview = self.preview(); self.assertIsNone(preview['plan_id'])
        self.assertEqual(preview['report']['artifact']['reason'], 'FLOW_ARCHIVE_PENDING')


class CopyArchiveTests(Calls, unittest.TestCase):
    setUp = archive_fixtures.FlowCleanupTests.setUp
    run_flow = archive_fixtures.FlowCleanupTests.run_flow

    def test_verified_archive_is_preserved_after_role_copy_deletion(self):
        import flow_archive_writer
        import flow_cleanup
        import flow_archive
        preview = flow_cleanup.preview(self.root, 'sample', days=1, now=self.now)
        flow_archive_writer.write_archive(self.root, 'sample', preview['plan_hash'], 1, now=self.now)
        before = {str(p.relative_to(self.flow)): p.read_bytes() for p in (self.flow / 'archive').rglob('*') if p.is_file()}
        with patch.object(role_manage.time, 'time', return_value=self.now):
            plan = self.success('roles.prune.preview', flow_id='sample', role='developer', task_id=self.result['developer']['task'], days=1)
            self.assertEqual(plan['report']['archive'], 'ARCHIVE_READY')
            self.assertTrue(self.apply(plan)['ok'])
        self.assertEqual(before, {str(p.relative_to(self.flow)): p.read_bytes() for p in (self.flow / 'archive').rglob('*') if p.is_file()})
        self.assertEqual(flow_archive.load_snapshot(self.root, 'sample')['artifact_state'], 'ARCHIVE_READY')


if __name__ == '__main__':
    unittest.main()
