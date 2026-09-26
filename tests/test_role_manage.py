import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch
import test_role_review as fixtures
from agent_bridge import workspace
from agent_bridge.storage import FileLock, Store, atomic_json, load_config
import role_manage
import project_overview


class RoleManageTests(unittest.TestCase):
    setUp = fixtures.RoleReviewTests.setUp

    def args(self, role='developer'):
        return (self.root, 'sample', role, self.result[role]['task'])

    def promote(self, apply=False):
        return role_manage.promote(*self.args(), ['src/x.py'], apply=apply)

    def age(self):
        old = time.time() - 10 * 86400
        queue = self.root / '.agent-bridge' if 'queue_mode' in self.result else self.flow / 'developer-state'
        with Store(queue).connect() as db:
            db.execute('UPDATE tasks SET finished=? WHERE id=?', (old, self.args()[-1]))
        for p in list(self.home.rglob('*')) + [self.home]:
            if p.name == 'journal.json':
                value = json.loads(p.read_text())
                value['updated'] = old
                atomic_json(p, value)
            os.utime(p, (old, old))
        for p in [p for p in self.home.rglob('*') if p.is_dir()] + [self.home]:
            os.utime(p, (old, old))

    def test_promote_recover_and_protection(self):
        self.assertFalse(self.promote()['apply'])
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')
        applied = self.promote(True)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'new\n')
        self.age()
        self.assertEqual(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['reason'], 'RECOVERY_DATA_PROTECTED')
        recovered = role_manage.recover(*self.args(), applied['backup'], apply=True)
        self.assertEqual(recovered['journal']['state'], 'ROLLED_BACK')
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')
        self.age()
        self.assertTrue(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['deleted'])
        self.assertFalse((self.flow / 'developer-state/queue.sqlite3').exists())

    def test_selection_reviewer_and_writable(self):
        for paths in (None, [], ['src/x.py', 'src/x.py'], ['../x'], ['src/unknown']):
            with self.assertRaises(ValueError): role_manage.promote(*self.args(), paths, True)
        with self.assertRaises(ValueError): role_manage.promote(*self.args('reviewer'), ['src/x.py'], True)
        with self.assertRaises(ValueError): role_manage.prune(self.root, 'sample', 'developer', 'wrong', apply=True)
        config = json.loads((self.root / 'bridge.json').read_text())
        config['writable'] = ['other']
        atomic_json(self.root / 'bridge.json', config)
        with self.assertRaises(ValueError): self.promote(True)

    def test_original_proposal_manifest_conflicts(self):
        (self.root / 'src/x.py').write_text('later\n')
        with self.assertRaises(ValueError): self.promote(True)
        (self.root / 'src/x.py').write_text('old\n')
        (self.home / 'project/src/x.py').write_text('other\n')
        with self.assertRaises(ValueError): self.promote(True)
        workspace.changes(load_config(self.flow / 'developer-config.json'), self.home)
        with self.assertRaisesRegex(ValueError, 'terminal task record'): self.promote(True)

    def test_locks_and_active_flow(self):
        for state in (self.root / '.agent-bridge', self.flow / 'developer-state'):
            for name in ('runner.lock', 'promotion.lock'):
                with FileLock(state / name):
                    with self.assertRaises(RuntimeError): self.promote(True)
                    with self.assertRaises(RuntimeError): role_manage.prune(*self.args(), apply=True)
        self.result['state'] = 'TESTING'
        atomic_json(self.flow / 'result.json', self.result)
        with self.assertRaisesRegex(ValueError, 'active or incomplete'): self.promote(True)

    def test_interrupted_apply_recovery_later_edit(self):
        original = workspace.replace
        def fail(source, dest, digest):
            if Path(source).is_relative_to(self.home / 'project'): raise OSError('injected failure')
            return original(source, dest, digest)
        with patch.object(workspace, 'replace', fail):
            with self.assertRaises(OSError): self.promote(True)
        backup = next(self.home.glob('backup-*')).name
        self.assertEqual(json.loads((self.home / backup / 'journal.json').read_text())['state'], 'APPLYING')
        (self.root / 'src/x.py').write_text('later\n')
        with self.assertRaises(ValueError): role_manage.recover(*self.args(), backup, apply=True)
        (self.root / 'src/x.py').write_text('old\n')
        role_manage.recover(*self.args(), backup, apply=True)

    def test_prune_preserves_answer_usage_original(self):
        store = Store(self.root / '.agent-bridge')
        with store.connect() as db:
            row = db.execute('SELECT result FROM tasks WHERE id=?', (self.args()[-1],)).fetchone()
            result = json.loads(row['result'])
            result.pop('answer', None)
            result.pop('provider_result', None)
            db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps(result), self.args()[-1]))
        (self.home / 'answer.txt').write_text('retained answer', encoding='utf-8')
        self.age()
        usage = project_overview.overview(self.root)['usage']
        self.assertTrue(role_manage.prune(*self.args(), days=1)['artifact']['eligible'])
        self.assertTrue(self.home.exists())
        self.assertTrue(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['deleted'])
        self.assertEqual(store.artifact_status(self.args()[-1])['state'], 'DELETED')
        row = next(r for r in store.rows() if r['id'] == self.args()[-1])
        self.assertEqual(json.loads(row['result'])['answer']['text'], 'retained answer')
        self.assertEqual(project_overview.overview(self.root)['usage'], usage)
        self.assertEqual(store.control()['calls_started'], 2)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')
        self.assertTrue(Path(self.result['reviewer']['result']['workspace']).exists())
        self.assertEqual(role_manage.prune(*self.args(), days=1)['artifact']['reason'], 'ARTIFACTS_MISSING')

    def test_retention_delete_failure_damaged_backup(self):
        self.assertEqual(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['reason'], 'RETENTION_PERIOD')
        self.age()
        with patch.object(role_manage.shutil, 'rmtree', side_effect=OSError('injected failure')):
            result = role_manage.prune(*self.args(), days=1, apply=True)
        self.assertEqual(result['artifact']['reason'], 'DELETE_FAILED')
        backup = self.home / 'backup-broken'
        backup.mkdir()
        atomic_json(backup / 'journal.json', dict(state='ROLLED_BACK', changes=[dict(path='src/x.py', before='0'*64, after=None)]))
        self.assertEqual(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['reason'], 'UNSAFE_OR_INCOMPLETE')

    def test_private_history(self):
        state = self.flow / 'developer-state'
        with contextlib.closing(sqlite3.connect(self.root / '.agent-bridge/queue.sqlite3')) as source:
            with contextlib.closing(sqlite3.connect(state / 'queue.sqlite3')) as dest: source.backup(dest)
        self.result.pop('queue_mode')
        self.result['developer'].pop('queue_state')
        atomic_json(self.flow / 'result.json', self.result)
        applied = self.promote(True)
        role_manage.recover(*self.args(), applied['backup'], apply=True)
        self.age()
        self.assertTrue(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['deleted'])
        self.assertEqual(Store(state).artifact_status(self.args()[-1])['state'], 'DELETED')
        self.assertIsNone(Store(self.root / '.agent-bridge').artifact_status(self.args()[-1]))

    def test_cli_missing_paths(self):
        argv = ['role_manage', '--project', str(self.root), 'promote', '--id', 'sample', '--role', 'developer', '--task', self.args()[-1], '--apply']
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(role_manage.main(), 2)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')

    def test_prune_rechecks_retention_and_preserve_failure(self):
        self.age()
        original = role_manage.candidate
        calls = 0
        def changed(config, row, cutoff):
            nonlocal calls
            calls += 1
            if calls == 2: (self.home / 'answer.txt').write_text('recent edit')
            return original(config, row, cutoff)
        with patch.object(role_manage, 'candidate', changed):
            self.assertEqual(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['reason'], 'RETENTION_PERIOD')
        self.age()
        with patch.object(role_manage, 'preserve_before_prune', side_effect=OSError('archive failed')):
            self.assertEqual(role_manage.prune(*self.args(), days=1, apply=True)['artifact']['reason'], 'DELETE_FAILED')
        self.assertTrue(self.home.exists())

    def test_reviewer_cleanup_keeps_developer(self):
        review_home = Path(self.result['reviewer']['result']['workspace'])
        old = time.time() - 10 * 86400
        with Store(self.root / '.agent-bridge').connect() as db:
            db.execute('UPDATE tasks SET finished=? WHERE id=?', (old, self.args('reviewer')[-1]))
        for p in list(review_home.rglob('*')) + [review_home]: os.utime(p, (old, old))
        self.assertTrue(role_manage.prune(*self.args('reviewer'), days=1, apply=True)['artifact']['deleted'])
        self.assertTrue(self.home.exists())

    def test_installed_launcher_preview(self):
        import connect_project
        import subprocess
        # Installation needs a supported read-only policy; no provider is invoked.
        (self.root / 'role_workflow.json').unlink()
        connect_project.install(connect_project.plan_install(self.root, sys.executable), True)
        command = [sys.executable, str(self.root / 'bridge.py'), 'role-manage', 'promote',
                   '--id', 'sample', '--role', 'developer', '--task', self.args()[-1], '--paths', 'src/x.py']
        result = subprocess.run(command, cwd=self.root, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertFalse(json.loads(result.stdout)['apply'])
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')

    def test_partial_multifile_apply_and_recovery(self):
        (self.home / 'project/src/y.py').write_text('created\n')
        config = load_config(self.flow / 'developer-config.json')
        changes = workspace.changes(config, self.home)
        store = Store(self.root / '.agent-bridge')
        with store.connect() as db:
            row = db.execute('SELECT result FROM tasks WHERE id=?', (self.args()[-1],)).fetchone()
            value = json.loads(row['result'])
            value['changes'] = changes
            db.execute('UPDATE tasks SET result=? WHERE id=?', (json.dumps(value), self.args()[-1]))
        original = workspace.replace
        def fail_second(source, dest, digest):
            if Path(source) == self.home / 'project/src/y.py': raise OSError('second file failed')
            return original(source, dest, digest)
        with patch.object(workspace, 'replace', fail_second):
            with self.assertRaises(OSError): role_manage.promote(*self.args(), ['src/x.py', 'src/y.py'], True)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'new\n')
        self.assertFalse((self.root / 'src/y.py').exists())
        backup = next(self.home.glob('backup-*')).name
        role_manage.recover(*self.args(), backup, apply=True)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')
        role_manage.promote(*self.args(), ['src/y.py'], True)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'old\n')
        self.assertEqual((self.root / 'src/y.py').read_text(), 'created\n')

    def test_active_peer_blocks_even_if_saved_phase_is_terminal(self):
        with Store(self.root / '.agent-bridge').connect() as db:
            db.execute("UPDATE tasks SET state='ROLE_RUNNING' WHERE id=?", (self.args('reviewer')[-1],))
        with self.assertRaisesRegex(ValueError, 'active or incomplete'): self.promote(True)
        with self.assertRaisesRegex(ValueError, 'active or incomplete'): role_manage.prune(*self.args(), apply=True)


if __name__ == '__main__': unittest.main()
