import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge.storage import Store, atomic_json
from agent_bridge.usage import snapshot
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import connect_project
import flow_archive
import project_overview
import role_flow
import role_manage
import role_review


class ArchiveViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'src').mkdir()
        (self.root / 'src/x.py').write_text('VALUE=1\n')
        self.endpoints = {name:dict(adapter='command', command=[sys.executable, '-c', 'pass'])
                          for name in ('developer', 'reviewer')}
        atomic_json(self.root / 'bridge.json', dict(project='.', state='.agent-bridge',
            include=['src'], writable=['src'], endpoints=self.endpoints))
        self.flow = self.root / '.role-flows/sample'
        self.archive = self.flow / 'archive'
        (self.archive / 'blobs').mkdir(parents=True)

    def fixture(self, shared=True, reviewer=True):
        parent = Store(self.root / '.agent-bridge')
        roles = {}
        for role, state in (('developer', 'developer-state'), ('reviewer', 'review-input/.agent-bridge')):
            if role == 'reviewer' and not reviewer:
                roles[role] = None
                continue
            store = parent if shared else Store(self.flow / state)
            options = dict(context_plan={}, role=dict(flow='sample', role=role, config={})) if shared else {}
            task = store.submit('saved', [role], 'historical task', self.endpoints, **options)[0]
            store.claim(task)
            answer = dict(text='Past answer; not an approval.', status='COMPLETE')
            store.finish(task, 'DONE', dict(answer=answer, changes=[]))
            store.record_usage(task, snapshot('codex', dict(usage=dict(input_tokens=100, output_tokens=5))))
            roles[role] = dict(task=task, state='DONE', answer=answer, changes=[])
        data = b'Ran 9 tests\r\nOK\n'
        digest = hashlib.sha256(data).hexdigest()
        (self.archive / 'blobs' / digest).write_bytes(data)
        self.manifest = dict(schema=1, flow='sample', entries=[
            dict(source=source, path=path, kind='file', sha256=digest, size=len(data), mtime_ns=1)
            for source,path in [('flow_metadata','test-0.log'),('reviewer_workspace','__bridge_review__/test-0.log')]])
        self.records = dict(schema=1, flow='sample', queue_mode='SHARED_PARENT' if shared else 'PRIVATE',
                            original_phase='REVIEW_DONE' if reviewer else 'TESTS_FAILED', roles=roles,
                            checks=[dict(argv=['python','tests.py'], exit_code=0, log='test-0.log')])
        self.save()
        return parent

    def save(self):
        data = json.dumps(self.records).encode()
        (self.archive / 'records.json').write_bytes(data)
        self.manifest['records_sha256'] = hashlib.sha256(data).hexdigest()
        self.manifest['archive_digest'] = flow_archive.manifest_digest(self.manifest)
        atomic_json(self.archive / 'manifest.json', self.manifest)
        atomic_json(self.flow / 'result.json', dict(id='sample', state='ARCHIVED', artifact_layout='FLOW_ARCHIVE_V1',
            queue_mode=self.records['queue_mode'], original_phase=self.records['original_phase'],
            archive_digest=self.manifest['archive_digest'], cleanup_operation='cleanup-1'))
        atomic_json(self.flow / 'cleanup.json', dict(schema=1, flow='sample', operation='cleanup-1',
            archive_digest=self.manifest['archive_digest'], state='COMPACTED'))

    def inventory(self):
        return {str(p.relative_to(self.root)):(p.read_bytes(),p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_archived_role_and_shared_overview_are_read_only(self):
        parent = self.fixture()
        before = self.inventory()
        with patch.object(Store, '__init__', side_effect=AssertionError('must not open writable Store')):
            view = role_review.inspect(self.root, 'sample', 'reviewer', evidence_path='__bridge_review__/test-0.log')
            overview = project_overview.overview(self.root)
        self.assertEqual(view['evidence']['content'], 'Ran 9 tests\r\nOK\n')
        self.assertIsNone(view['manifest_matches'])
        self.assertFalse(view['current_files_checked'])
        self.assertFalse(view['promotion_supported'])
        self.assertEqual(overview['flows'][0]['saved_phase'], 'REVIEW_DONE')
        self.assertEqual(overview['flows'][0]['artifact_state'], 'COMPACTED')
        self.assertFalse(overview['errors'])
        self.assertEqual(overview['usage']['summary']['tasks'], 2)
        self.assertEqual(overview['usage']['summary']['metrics']['input_tokens']['known_sum'], 200)
        self.assertEqual(before, self.inventory())
        self.assertEqual(parent.control()['calls_started'], 2)

    def test_private_queues_preserve_usage_and_missing_is_not_replaced(self):
        self.fixture(shared=False)
        overview = project_overview.overview(self.root)
        self.assertTrue(overview['usage']['coverage_complete'])
        self.assertEqual(overview['usage']['summary']['tasks'], 2)
        (self.flow / 'review-input/.agent-bridge/queue.sqlite3').unlink()
        before = self.inventory()
        overview = project_overview.overview(self.root)
        self.assertFalse(overview['usage']['coverage_complete'])
        self.assertEqual(overview['usage']['summary']['tasks'], 1)
        self.assertEqual(overview['flows'][0]['roles']['reviewer']['observation'], 'MISSING')
        self.assertEqual(before, self.inventory())

    def test_unsubmitted_private_role_is_explicit(self):
        self.fixture(shared=False, reviewer=False)
        view = project_overview.overview(self.root)
        self.assertEqual(view['flows'][0]['roles']['reviewer']['observation'], 'NOT_SUBMITTED')
        self.assertTrue(view['usage']['coverage_complete'])
        with self.assertRaisesRegex(ValueError, 'not submitted'):
            role_review.inspect(self.root, 'sample', 'reviewer')

    def test_corrupt_archive_does_not_drop_shared_usage(self):
        self.fixture()
        blob = self.archive / 'blobs' / self.manifest['entries'][0]['sha256']
        blob.write_bytes(b'bad')
        before = self.inventory()
        result = project_overview.overview(self.root)
        self.assertTrue(result['errors'])
        self.assertEqual(result['flows'][0]['evidence_status'], 'CORRUPT')
        self.assertEqual(result['usage']['summary']['tasks'], 2)
        with self.assertRaises(ValueError): role_review.inspect(self.root, 'sample', 'developer')
        self.assertEqual(before, self.inventory())

    def test_queue_mismatch_is_reported_without_archive_usage(self):
        self.fixture()
        self.records['roles']['developer']['task'] = 'other-task'
        self.save()
        result = project_overview.overview(self.root)
        self.assertTrue(result['errors'])
        self.assertFalse(result['usage']['coverage_complete'])
        self.assertEqual(result['usage']['summary']['tasks'], 2)

    def test_records_marker_and_journal_validation(self):
        self.fixture()
        self.records['roles']['developer']['state'] = 'RUNNING'
        self.save()
        with self.assertRaisesRegex(ValueError, 'terminal'): flow_archive.load_view(self.root, 'sample')
        self.records['roles']['developer']['state'] = 'DONE'
        self.records['checks'][0]['log'] = 'missing.log'
        self.save()
        with self.assertRaisesRegex(ValueError, 'log'): flow_archive.load_view(self.root, 'sample')
        self.records['checks'][0]['log'] = 'test-0.log'
        self.save()
        saved = json.loads((self.flow / 'result.json').read_text())
        saved['archive_digest'] = '0'*64
        atomic_json(self.flow / 'result.json', saved)
        with self.assertRaisesRegex(ValueError, 'marker digest'): flow_archive.load_view(self.root, 'sample')
        self.save()
        journal = json.loads((self.flow / 'cleanup.json').read_text())
        journal['operation'] = 'different'
        atomic_json(self.flow / 'cleanup.json', journal)
        with self.assertRaisesRegex(ValueError, 'journal'): flow_archive.load_view(self.root, 'sample')

    def test_incomplete_cleanup_and_mutation_commands(self):
        self.fixture()
        journal = json.loads((self.flow / 'cleanup.json').read_text())
        journal['state'] = 'DELETE_FAILED'
        atomic_json(self.flow / 'cleanup.json', journal)
        view = role_review.inspect(self.root, 'sample', 'developer')
        self.assertEqual(view['artifact_state'], 'DELETE_FAILED')
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, 'FLOW_ARCHIVED'):
            role_manage.promote(self.root, 'sample', 'developer', 'saved--developer', ['src/x.py'], True)
        self.assertEqual(before, self.inventory())

    def test_result_query_never_dispatches(self):
        self.fixture()
        out = io.StringIO()
        with patch.object(sys, 'argv', ['role_flow','--project',str(self.root),'--result','--id','sample']), \
                patch.object(role_flow, 'develop_review', side_effect=AssertionError('must not run')), \
                contextlib.redirect_stdout(out):
            self.assertEqual(role_flow.main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['developer']['task'], 'saved--developer')
        self.assertIsNone(result['developer']['usage'])
        self.assertFalse(result['review_is_approval'])

    def test_installed_launcher_archive_queries(self):
        self.fixture()
        connect_project.install(connect_project.plan_install(self.root, sys.executable), True)
        self.assertTrue((self.root / 'flow_archive.py').is_file())
        before = self.inventory()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        for args in (['role-review','--id','sample','--role','reviewer','--evidence-path','__bridge_review__/test-0.log'],
                     ['overview'], ['develop-review','--id','sample','--result']):
            proc = subprocess.run([sys.executable,'-B',str(self.root/'bridge.py'),*args],
                                  cwd=self.root,env=env,capture_output=True,timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIsInstance(json.loads(proc.stdout), dict)
        self.assertEqual(before, self.inventory())

    def test_invalid_cli_evidence_and_worker_access(self):
        self.fixture()
        base = [sys.executable,'-B',role_review.__file__,'--project',str(self.root),'--id','sample','--role','reviewer']
        for args in (['--evidence-path','../escape'], ['--evidence-source','flow_metadata']):
            proc = subprocess.run(base+args,capture_output=True,timeout=15)
            self.assertEqual(proc.returncode, 2)
            self.assertIn('error', json.loads(proc.stdout))
        old = Path.cwd()
        try:
            os.chdir(self.flow)
            with self.assertRaisesRegex(ValueError, 'Workers'):
                role_review.inspect(self.root, 'sample', 'reviewer')
        finally:
            os.chdir(old)


if __name__ == '__main__':
    unittest.main()
