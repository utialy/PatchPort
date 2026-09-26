from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from agent_bridge import context
from agent_bridge.runner import run
from agent_bridge.storage import Store, atomic_json, load_config
from agent_bridge.usage import report

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import project_overview


class SharedRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/x.py').write_text('VALUE=1\n')
        self.endpoint = dict(adapter='command', command=[sys.executable, '-c', "print('MOCK_OK')"])
        self.raw = dict(project='.', state='.agent-bridge', include=['src'], writable=['src'],
                        parallel=2, timeout=5, endpoints={'local': self.endpoint})
        atomic_json(self.root / 'bridge.json', self.raw)
        self.config = load_config(self.root / 'bridge.json')
        self.store = Store(self.config['state'])

    def submit_role(self, flow='f1', role='developer', command=None):
        home = self.root / '.role-flows' / flow
        root = self.root if role == 'developer' else home / 'review-input'
        if role == 'reviewer':
            (root / 'src').mkdir(parents=True, exist_ok=True)
            (root / 'src/x.py').write_text('VALUE=1\n')
        state = home / 'developer-state' if role == 'developer' else root / '.agent-bridge'
        endpoint = self.endpoint if command is None else dict(adapter='command', command=command)
        raw = dict(self.raw, project=str(root), state=str(state), endpoints={role: endpoint})
        path = home / (role + '-config.json')
        atomic_json(path, raw)
        config = load_config(path)
        prompt = 'Read src/x.py'
        plan = dict(schema=1, files=[dict(path='src/x.py', mode='full', reason='test')])
        preview = context.plan_preview(config, prompt, plan=plan)
        frozen = context.freeze_plan(config, prompt, preview, [role])
        return self.store.submit(flow + '-' + role, [role], prompt, config['endpoints'], frozen,
                                 role=dict(schema=1, flow=flow, role=role, config=raw))[0]

    def once(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(self.config, True, **kwargs)

    def test_normal_and_role_claims_share_atomic_cap(self):
        ids = [self.submit_role('f' + str(i)) for i in range(6)]
        ids += [self.store.submit('normal' + str(i), ['local'], 'test', self.config['endpoints'])[0] for i in range(6)]
        self.store.set_limit(3)
        barrier = threading.Barrier(len(ids))
        def claim(task):
            store = Store(self.config['state'])
            barrier.wait(timeout=10)
            return store.claim(task)
        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            claimed = list(pool.map(claim, ids))
        self.assertEqual(sum(claimed), 3)
        self.assertEqual(self.store.control()['calls_started'], 3)
        self.store.recover()
        self.assertEqual(sum(r['state'] == 'INTERRUPTED' for r in self.store.rows()), 3)
        self.assertEqual(self.store.control()['calls_started'], 3)

    def test_legacy_claim_and_recovery_cannot_touch_roles(self):
        task = self.submit_role()
        with self.store.connect() as db:
            self.assertEqual(db.execute("UPDATE tasks SET state='RUNNING' WHERE id=? AND state IN ('QUEUED','PLAN_QUEUED')", (task,)).rowcount, 0)
        self.assertTrue(self.store.claim(task))
        with self.store.connect() as db:
            self.assertEqual(db.execute("UPDATE tasks SET state='INTERRUPTED' WHERE state IN ('RUNNING','PLAN_RUNNING')").rowcount, 0)
        self.store.recover()
        self.assertEqual(self.store.rows()[0]['state'], 'INTERRUPTED')
        self.assertFalse(self.store.claim(task))

    def test_role_submission_and_dependency_are_atomic(self):
        with self.assertRaises(ValueError):
            self.submit_role(role='reviewer')
        self.assertEqual(self.store.rows(), [])
        task = self.submit_role()
        self.assertTrue(self.store.claim(task))
        self.store.finish(task, 'ERROR', {})
        with self.assertRaises(ValueError):
            self.submit_role(role='reviewer')
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit_role()
        with self.store.connect() as db:
            for table in ('tasks', 'context_plans', 'role_tasks'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 1)

    def test_pause_limit_and_scoped_runner(self):
        role = self.submit_role()
        normal = self.store.submit('normal', ['local'], 'test', self.config['endpoints'])[0]
        self.store.pause(True)
        self.assertEqual(self.once(task_ids={role}), 2)
        self.assertEqual(self.store.control()['calls_started'], 0)
        self.store.pause(False)
        self.store.set_limit(1)
        self.assertEqual(self.once(task_ids={role}), 0)
        rows = {r['id']: r for r in self.store.rows()}
        self.assertEqual(rows[role]['state'], 'DONE')
        self.assertEqual(rows[normal]['state'], 'QUEUED')
        self.assertEqual(self.once(), 2)
        self.assertEqual(report(self.store)['summary']['tasks'], 1)
        self.assertFalse((self.root / '.role-flows/f1/developer-state/queue.sqlite3').exists())

    def test_missing_metadata_fails_consumes_once_and_never_runs_provider(self):
        task = self.submit_role()
        with self.store.connect() as db:
            db.execute('DELETE FROM role_tasks WHERE id=?', (task,))
        self.assertEqual(self.once(), 0)
        row = self.store.rows()[0]
        self.assertEqual(row['state'], 'ERROR')
        self.assertEqual(self.store.control()['calls_started'], 1)
        self.assertEqual(self.once(), 0)
        self.assertEqual(self.store.control()['calls_started'], 1)

    def test_queued_cancel_races_claim_without_refund(self):
        task = self.submit_role()
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            return self.store.claim(task)
        def cancel():
            barrier.wait()
            return self.store.cancel_queued_role(task, 'wait timeout')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(claim), pool.submit(cancel)
            claimed, cancelled = first.result(), second.result()
        self.assertNotEqual(claimed, cancelled)
        self.assertEqual(self.store.control()['calls_started'], int(claimed))
        self.assertFalse(self.store.claim(task))

    def test_parent_overview_does_not_double_count_shared_role(self):
        self.submit_role()
        self.once()
        atomic_json(self.root / '.role-flows/f1/result.json', dict(state='TESTING', queue_mode='SHARED_PARENT'))
        view = project_overview.overview(self.root)
        self.assertEqual(view['usage']['summary']['tasks'], 1)
        self.assertEqual(view['usage']['tasks'][0]['identity'][:3], ['role', 'f1', 'developer'])
        self.assertEqual(view['flows'][0]['roles']['reviewer']['observation'], 'NOT_SUBMITTED')
        self.assertTrue(view['usage']['coverage_complete'])

    def test_process_dies_after_claim_without_replay(self):
        task = self.submit_role()
        script = 'import os,sys; from agent_bridge.storage import Store; s=Store(sys.argv[1]); assert s.claim(sys.argv[2]); os._exit(9)'
        result = subprocess.run([sys.executable, '-c', script, str(self.config['state']), task], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.once()
        self.assertEqual(self.store.rows()[0]['state'], 'INTERRUPTED')
        self.assertEqual(self.store.control()['calls_started'], 1)
        self.assertFalse((self.root / '.role-flows/f1/developer-state/workspaces').exists())

    def test_pause_after_claim_allows_running_role_to_finish(self):
        self.submit_role(command=[sys.executable, '-c', "import time; time.sleep(.5); print('done')"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.once)
            deadline = time.monotonic() + 5
            while self.store.rows()[0]['state'] == 'QUEUED' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.store.rows()[0]['state'], 'RUNNING')
            self.store.pause(True)
            self.assertEqual(future.result(timeout=10), 0)
        self.assertEqual(self.store.rows()[0]['state'], 'DONE')
        self.assertEqual(self.store.control()['calls_started'], 1)

    def test_corrupt_role_paths_fail_closed_before_provider(self):
        task = self.submit_role()
        with self.store.connect() as db:
            value = json.loads(db.execute('SELECT data FROM role_tasks WHERE id=?', (task,)).fetchone()[0])
            value['config']['state'] = str(self.root / 'unexpected-state')
            db.execute('UPDATE role_tasks SET data=? WHERE id=?', (json.dumps(value), task))
        self.once()
        self.assertEqual(self.store.rows()[0]['state'], 'ERROR')
        self.assertFalse((self.root / 'unexpected-state').exists())
        self.assertEqual(self.store.control()['calls_started'], 1)


if __name__ == '__main__':
    unittest.main()
