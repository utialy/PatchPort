import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge.storage import Store, atomic_json
from agent_bridge.usage import snapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import project_overview


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.endpoints = {'mock': dict(adapter='command', command=[sys.executable, '-c', 'pass'])}
        atomic_json(self.root / 'bridge.json', dict(project='.', state='.agent-bridge',
                    include=['src'], writable=['src'], endpoints=self.endpoints))

    def task(self, state, finish=True):
        store = Store(state)
        task = store.submit('same', ['mock'], 'test', self.endpoints)[0]
        store.claim(task)
        store.record_usage(task, snapshot('codex', dict(usage=dict(input_tokens=100, cached_input_tokens=80, output_tokens=5))))
        if finish:
            store.finish(task, 'DONE', dict(answer=dict(text='Unverified bounds; inspect manually.')))
        return store, task

    def test_missing_read_creates_nothing(self):
        before = sorted(self.root.rglob('*'))
        result = project_overview.overview(self.root)
        self.assertEqual(before, sorted(self.root.rglob('*')))
        self.assertEqual(result['project']['observation'], 'MISSING')
        self.assertFalse(result['usage']['coverage_complete'])
        self.assertIsNone(result['usage']['summary']['metrics']['cost_usd']['known_sum'])

    def test_distinct_role_ids_no_cache_double_count_no_writes(self):
        self.task(self.root / '.agent-bridge')
        flow = self.root / '.role-flows/f1'
        self.task(flow / 'developer-state')
        self.task(flow / 'review-input/.agent-bridge')
        atomic_json(flow / 'result.json', dict(state='REVIEW_DONE', developer=dict(usage={'stale': 999})))
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(Store, '__init__', side_effect=AssertionError('must not open writable Store')):
            result = project_overview.overview(self.root)
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result['usage']['summary']['tasks'], 3)
        self.assertEqual(len({tuple(r['identity']) for r in result['usage']['tasks']}), 3)
        self.assertEqual(result['usage']['summary']['metrics']['input_tokens']['known_sum'], 300)
        self.assertEqual(result['usage']['summary']['metrics']['cost_usd']['unknown_tasks'], 3)
        role = result['flows'][0]
        self.assertFalse(role['review_is_approval'])
        self.assertEqual(role['user_action'], 'REVIEW_RESULTS')
        self.assertIn('Unverified', role['roles']['reviewer']['tasks'][0]['answer']['text'])

    def test_stale_phase_running_and_failed_tasks_are_observations(self):
        self.task(self.root / '.agent-bridge')
        flow = self.root / '.role-flows/f1'
        store, task = self.task(flow / 'developer-state', finish=False)
        atomic_json(flow / 'result.json', dict(state='DEVELOPING'))
        result = project_overview.overview(self.root)
        self.assertEqual(result['flows'][0]['execution_liveness'], 'UNKNOWN')
        self.assertEqual(result['flows'][0]['roles']['developer']['tasks'][0]['state'], 'RUNNING')
        self.assertFalse(result['usage']['coverage_complete'])
        store.finish(task, 'INTERRUPTED', {})
        result = project_overview.overview(self.root)
        self.assertEqual(result['flows'][0]['saved_phase'], 'DEVELOPING')
        self.assertEqual(result['flows'][0]['roles']['developer']['tasks'][0]['state'], 'INTERRUPTED')
        self.assertEqual(store.control()['calls_started'], 1)

    def test_bad_manifest_still_counts_database_and_bad_database_is_partial(self):
        self.task(self.root / '.agent-bridge')
        flow = self.root / '.role-flows/f1'
        self.task(flow / 'developer-state')
        (flow / 'result.json').write_text('{bad')
        broken = flow / 'review-input/.agent-bridge'
        broken.mkdir(parents=True)
        (broken / 'queue.sqlite3').write_bytes(b'not sqlite')
        result = project_overview.overview(self.root)
        self.assertEqual(result['usage']['summary']['tasks'], 2)
        self.assertEqual(len(result['errors']), 2)
        self.assertFalse(result['usage']['coverage_complete'])

    def test_legacy_usage_fallback_and_parent_control(self):
        store, task = self.task(self.root / '.agent-bridge')
        store.pause(True)
        store.set_limit(3)
        with store.connect() as db:
            db.execute('DROP TABLE usage_snapshots')
            db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps(dict(provider_result=dict(
                type='result', usage=dict(input_tokens=10, cache_read_input_tokens=20,
                cache_creation_input_tokens=30, output_tokens=4), total_cost_usd=0.01))), task))
        result = project_overview.overview(self.root)
        self.assertEqual(result['usage']['summary']['metrics']['input_tokens']['known_sum'], 60)
        self.assertEqual(result['project']['control']['dispatch'], 'PAUSED')
        self.assertEqual(result['project']['control']['remaining'], 2)
        with store.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='usage_snapshots'").fetchone())


if __name__ == '__main__':
    unittest.main()
