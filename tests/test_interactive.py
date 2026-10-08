import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_bridge import interactive, management
from agent_bridge.storage import Store, atomic_json


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.path = self.project / 'state' / 'interactive.sqlite3'
        self.config = self.project / 'bridge.json'
        atomic_json(self.config, dict(project='.', state='state', include=['input.txt'], writable=['input.txt'],
                    endpoints={'peer': dict(adapter='claude', command=['claude-2'], launch={'shell': 'powershell'})}))
        (self.project / 'input.txt').write_text('original')
        self.api = management.Session()
        self.serial = 0

    def call(self, action, **args):
        self.serial += 1
        result = self.api.handle(dict(protocol=1, id=str(self.serial), action=action, args=args))
        self.assertTrue(result['ok'], result)
        return result['result']

    def begin(self, id_='s1'):
        interactive.initialize(self.path)
        interactive.begin(self.path, id_, 'n1', self.project, 'claude', 'Peer', 'owner1')

    def test_missing_database_reads_create_nothing(self):
        before = list(self.project.iterdir())
        self.assertFalse(self.call('project.inspect', project=str(self.project))['database'])
        report = self.call('usage', config=str(self.config))
        self.assertEqual(report['summary']['tasks'], 0)
        self.assertEqual(self.call('interactive.report', database=str(self.path), project=str(self.project))['sessions'], [])
        self.assertEqual(before, list(self.project.iterdir()))
        self.assertFalse(self.path.parent.exists())

    def test_existing_queue_and_usage_reads_are_unchanged(self):
        store = Store(self.path.parent)
        store.submit('job1', ['peer'], 'question', {'peer': {}})
        self.assertTrue(store.claim('job1--peer'))
        store.finish('job1--peer', 'DONE', {'provider_result': {'type': 'result', 'usage': {'input_tokens': 10, 'output_tokens': 3, 'cache_read_input_tokens': 2, 'cache_creation_input_tokens': 1}, 'total_cost_usd': 0.1}})
        before = store.db.read_bytes()
        report = self.call('usage', config=str(self.config))
        self.assertEqual(report['summary']['metrics']['input_tokens']['known_sum'], 13)
        self.assertEqual(before, store.db.read_bytes())
        interactive.initialize(store.db)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
        self.assertEqual(store.control()['calls_started'], 1)

    def test_legacy_database_has_no_usage_table_and_is_not_migrated(self):
        self.path.parent.mkdir()
        queue = self.path.parent / 'queue.sqlite3'
        with sqlite3.connect(queue) as db:
            db.execute('CREATE TABLE tasks (id TEXT,batch TEXT,endpoint TEXT,state TEXT,created REAL,started REAL,finished REAL,result TEXT)')
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)', ('j--peer', 'j', 'peer', 'ERROR', 1, 1, 2, '{}'))
        db.close()
        before = queue.read_bytes()
        report = self.call('usage', config=str(self.config), group_by='model')
        self.assertEqual(report['summary']['metrics']['cost_usd']['unknown_tasks'], 1)
        self.assertEqual(before, queue.read_bytes())

    def test_events_and_cumulative_usage_deduplicate_and_reject_conflicts(self):
        self.begin()
        metrics = dict(input_tokens=40, output_tokens=4, cost_usd=0.02)
        data = dict(sequence=1, metrics=metrics, source='fixture', model='model', coverage='test')
        self.assertFalse(interactive.record(self.path, 's1', 'e1', 'usage', data=data)['duplicate'])
        self.assertTrue(interactive.record(self.path, 's1', 'e1', 'usage', data=data)['duplicate'])
        with self.assertRaises(ValueError): interactive.record(self.path, 's1', 'e1', 'usage', data={**data, 'sequence': 2})
        interactive.record(self.path, 's1', 'e2', 'usage', data={**data, 'sequence': 2, 'metrics': {**metrics, 'input_tokens': 50}})
        interactive.record(self.path, 's1', 'e3', 'usage', data=data)
        result = interactive.report(self.path, self.project)
        self.assertEqual(result['summary']['metrics']['input_tokens']['known_sum'], 50)
        self.assertEqual(result['summary']['metrics']['cache_read_tokens']['unknown_tasks'], 1)
        self.assertEqual(len(result['events']), 3)

    def test_end_owner_marks_pending_uncertain_and_never_resends(self):
        self.begin()
        interactive.delivery(self.path, 'owner1', dict(id='d1', status='sent', text='peer context', project=str(self.project)))
        interactive.delivery(self.path, 'owner1', dict(id='d2', status='queued', text='context', project=str(self.project)))
        interactive.end_owner(self.path, 'owner1')
        result = interactive.report(self.path, self.project)
        self.assertEqual(result['sessions'][0]['state'], 'UNCERTAIN')
        self.assertEqual({d['status'] for d in result['deliveries']}, {'uncertain', 'cancelled'})
        with self.assertRaises(ValueError): interactive.delivery(self.path, 'owner1', dict(id='d1', status='sent'))

    def test_invalid_usage_rolls_back_event_and_requires_explicit_initialize(self):
        self.begin()
        with self.assertRaises(ValueError): interactive.record(self.path, 's1', 'bad', 'usage', data={'sequence': True})
        self.assertEqual(interactive.report(self.path, self.project)['events'], [])
        reply = self.api.handle(dict(protocol=1, id='bad', action='interactive.initialize', args={'database': str(self.path), 'apply': False}))
        self.assertEqual(reply['problem']['code'], 'EXPLICIT_APPLY_REQUIRED')

    def test_history_is_bounded_and_full_event_stays_readable(self):
        self.begin()
        answer = 'A' * 20000
        interactive.record(self.path, 's1', 'reply', 'completed', data={'answer': answer})
        before = self.path.read_bytes()
        preview = interactive.report(self.path, self.project, owner='different-owner')
        self.assertEqual(preview['sessions'][0]['state'], 'UNCONFIRMED')
        self.assertEqual(len(preview['events'][0]['data']['answer']), 600)
        self.assertEqual(interactive.read_event(self.path, self.project, 'reply')['data']['answer'], answer)
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(ValueError): interactive.read_event(self.path, self.project / 'other', 'reply')


if __name__ == '__main__':
    unittest.main()
