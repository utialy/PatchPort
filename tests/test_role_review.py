import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import workspace
from agent_bridge.storage import Store, atomic_json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import role_flow
import role_review


class RoleReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'src').mkdir()
        (self.root / 'src/x.py').write_text('old\n')
        endpoint = dict(adapter='command', command=[sys.executable, '-c', 'pass'])
        atomic_json(self.root / 'bridge.json', dict(project='.', include=['src'], writable=['src'], endpoints={'local': endpoint}))
        atomic_json(self.root / 'role_workflow.json', dict(
            developer=dict(adapter='command', command=[sys.executable, '-c', "from pathlib import Path; Path('src/x.py').write_text('new\\n')"]),
            reviewer=endpoint, checks=[[sys.executable, '-c', 'pass']]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.result = role_flow.develop_review(self.root, 'edit', 'sample', allow_mock=True)
        self.assertEqual(self.result['state'], 'REVIEW_DONE', self.result)
        self.flow = self.root / '.role-flows/sample'
        self.home = Path(self.result['developer']['result']['workspace'])

    def inspect(self, role='developer'):
        return role_review.inspect(self.root, 'sample', role)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_shared_read_only_and_reviewer(self):
        before = self.snapshot()
        value = self.inspect()
        self.assertEqual(value['queue_mode'], 'SHARED_PARENT')
        self.assertTrue(value['manifest_matches'])
        self.assertTrue(value['changes'][0]['original_matches'])
        self.assertEqual(self.inspect('reviewer')['changes'], [])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(Store(self.root / '.agent-bridge').control()['calls_started'], 2)

    def test_private_history(self):
        state = self.flow / 'developer-state'
        with contextlib.closing(sqlite3.connect(self.root / '.agent-bridge/queue.sqlite3')) as source:
            with contextlib.closing(sqlite3.connect(state / 'queue.sqlite3')) as target:
                source.backup(target)
        self.result.pop('queue_mode')
        self.result['developer'].pop('queue_state')
        atomic_json(self.flow / 'result.json', self.result)
        before = self.snapshot()
        self.assertEqual(self.inspect()['queue_mode'], 'PRIVATE')
        self.assertEqual(before, self.snapshot())

    def test_original_and_proposal_conflicts(self):
        (self.root / 'src/x.py').write_text('later\n')
        self.assertFalse(self.inspect()['changes'][0]['original_matches'])
        (self.home / 'project/src/x.py').write_text('different\n')
        self.assertFalse(self.inspect()['manifest_matches'])
        (self.home / 'project/src/x.py').write_text('old\n')
        self.assertFalse(self.inspect()['manifest_matches'])

    def test_paths_metadata_and_config_rejected(self):
        path = self.flow / 'result.json'
        original = path.read_bytes()
        for value in ({}, [], dict(self.result, id='elsewhere')):
            atomic_json(path, value)
            with self.assertRaises(ValueError): self.inspect()
        path.write_bytes(original)
        self.result['developer']['config'] = str(self.root / 'bridge.json')
        atomic_json(path, self.result)
        with self.assertRaises(ValueError): self.inspect()
        path.write_bytes(original)
        config_path = self.flow / 'developer-config.json'
        config = json.loads(config_path.read_text())
        config['state'] = str(self.root / '.agent-bridge')
        atomic_json(config_path, config)
        with self.assertRaises(ValueError): self.inspect()

    def test_active_missing_and_corrupt_artifacts(self):
        task = self.result['developer']['task']
        store = Store(self.root / '.agent-bridge')
        with store.connect() as db:
            db.execute('UPDATE tasks SET state=? WHERE id=?', ('ROLE_RUNNING', task))
        with self.assertRaisesRegex(ValueError, 'not terminal'): self.inspect()
        with store.connect() as db:
            db.execute('UPDATE tasks SET state=? WHERE id=?', ('DONE', task))
        (self.home / 'baseline.json').write_text('{')
        with self.assertRaises(ValueError): self.inspect()
        (self.home / 'baseline.json').unlink()
        with self.assertRaises(OSError): self.inspect()

    def test_recovery_backup_protection(self):
        backup = self.home / 'backup-test'
        backup.mkdir()
        atomic_json(backup / 'journal.json', dict(state='APPLIED'))
        self.assertEqual(self.inspect()['recovery_protection']['reason'], 'RECOVERY_DATA_PROTECTED')
        atomic_json(backup / 'journal.json', dict(state='ROLLED_BACK', changes=[dict(path='src/x.py', before='0'*64, after=None)]))
        self.assertEqual(self.inspect()['recovery_protection']['reason'], 'UNSAFE_OR_INCOMPLETE')

    def test_link_rejected(self):
        link = self.home / 'project/link'
        try:
            link.symlink_to(self.root / 'src', target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError): self.inspect()

    def test_reparse_ancestor_rejected(self):
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.root / '.role-flows':
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, 'lstat', metadata):
            with self.assertRaises(ValueError): self.inspect()

    def test_traversal_and_missing_flow_create_nothing(self):
        before = self.snapshot()
        with self.assertRaises(ValueError): role_review.inspect(self.root, '../sample', 'developer')
        with self.assertRaises(OSError): role_review.inspect(self.root, 'missing', 'developer')
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
