import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_bridge.storage import FileLock, Store, atomic_json, load_config, probe_lock, readonly_database
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import connect_project
import flow_cleanup
import flow_lifecycle
import role_flow
import role_manage


class ReadOnlyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_probe_never_creates_and_observes_real_lock(self):
        path = self.root/'missing/runner.lock'
        self.assertEqual(probe_lock(path), 'MISSING')
        self.assertFalse(path.parent.exists())
        with FileLock(path):
            self.assertEqual(probe_lock(path), 'LOCKED')
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(probe_lock(path), 'FREE')
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_database_snapshot_is_memory_only_and_query_only(self):
        Store(self.root)
        path = self.root/'queue.sqlite3'
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with contextlib.closing(readonly_database(path)) as db:
            self.assertEqual(db.execute('SELECT calls_started FROM control').fetchone()[0], 0)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('UPDATE control SET calls_started=5')
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['queue.sqlite3'])

    def test_sidecars_and_wal_headers_are_not_ignored(self):
        Store(self.root)
        path = self.root/'queue.sqlite3'
        for suffix in ('-wal','-shm','-journal'):
            side = Path(str(path)+suffix)
            side.write_bytes(b'pending')
            with self.assertRaisesRegex(ValueError, 'SQLITE_SIDECAR'): readonly_database(path)
            self.assertEqual(side.read_bytes(), b'pending')
            side.unlink()
        data = bytearray(path.read_bytes()); data[18:20] = b'\x02\x02'; path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'rollback-mode'): readonly_database(path)


class FlowCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'src').mkdir()
        (self.root/'src/x.py').write_bytes(b'VALUE=1\n')
        (self.root/'src/data.txt').write_bytes(b'x'*65536)
        atomic_json(self.root/'bridge.json', dict(project='.', state='.agent-bridge', include=['src'],
            writable=['src/x.py'], endpoints={'local':dict(adapter='command',command=[sys.executable,'-c','pass'])}))
        self.policy = dict(
            developer=dict(adapter='command',command=[sys.executable,'-c',"from pathlib import Path; Path('src/x.py').write_text('VALUE=2\\n'); print('DEV')"]),
            reviewer=dict(adapter='command',command=[sys.executable,'-c',"print('REVIEW')"]),
            checks=[[sys.executable,'-B','-c',"print('CHECK_OK')"]])
        atomic_json(self.root/'role_workflow.json',self.policy)
        self.result = self.run_flow('sample')
        self.assertEqual(self.result['state'],'REVIEW_DONE',self.result)
        self.base = load_config(self.root/'bridge.json')
        self.flow = self.root/'.role-flows/sample'
        self.now = time.time()+3*86400

    def run_flow(self, name):
        with contextlib.redirect_stderr(io.StringIO()):
            return role_flow.develop_review(self.root,'Change x.py.',name,allow_mock=True)

    def preview(self):
        return flow_cleanup.preview(self.root,'sample',days=1,now=self.now)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():(p.read_bytes(),p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_complete_preview_is_read_only_and_deduplicates_evidence(self):
        before = self.snapshot()
        with patch.object(Store,'__init__',side_effect=AssertionError('must not initialize queues')):
            result = self.preview()
            again = self.preview()
        self.assertTrue(result['eligible'],result)
        self.assertFalse(result['apply_supported'])
        self.assertGreater(result['estimated_net_reclaim_bytes'],0)
        self.assertEqual(result['plan_hash'],again['plan_hash'])
        self.assertEqual(before,self.snapshot())
        self.assertTrue(any(i['path']=='test-0.log' and i['source']=='flow_metadata' for i in result['archive_entries']))
        self.assertGreater(sum(i['size'] for i in result['archive_entries']),result['archive_new_bytes'])
        self.assertEqual(result['replace_entries'][0]['path'],'result.json')
        self.assertFalse(any('queue.sqlite3' in i['path'] for i in result['delete_entries']))

    def test_retention_and_future_activity(self):
        self.assertIn('RETENTION_PERIOD',flow_cleanup.preview(self.root,'sample',days=1)['reasons'])
        log = self.flow/'test-0.log'
        os.utime(log,(self.now+60,self.now+60))
        self.assertIn('FUTURE_TIMESTAMP',self.preview()['reasons'])

    def test_flow_runner_and_promotion_locks_block_preview(self):
        guard = flow_lifecycle.lock_path(self.root,self.base,'sample')
        paths = [(guard,'FLOW_BUSY'),(self.base['state']/'runner.lock','RUNNER_BUSY'),
                 (self.base['state']/'promotion.lock','PROMOTION_BUSY'),
                 (self.flow/'developer-state/runner.lock','RUNNER_BUSY'),
                 (self.flow/'review-input/.agent-bridge/promotion.lock','PROMOTION_BUSY')]
        for path,reason in paths:
            with self.subTest(path=path),FileLock(path):
                self.assertIn(reason,self.preview()['reasons'])

    def test_lifetime_lock_covers_host_checks_and_blocks_management(self):
        observed=[]
        def check(argv,**kwargs):
            other=self.root/'.role-flows/second'
            saved=json.loads((other/'result.json').read_text())
            saved['state']='REVIEW_DONE'
            atomic_json(other/'result.json',saved)
            observed.append(flow_cleanup.preview(self.root,'second')['reasons'])
            with self.assertRaises(RuntimeError):
                role_manage.promote(self.root,'second','developer',saved['developer']['task'],['src/x.py'],True)
            return subprocess.CompletedProcess(argv,0,stdout=b'checked')
        with patch.object(role_flow.subprocess,'run',side_effect=check):
            result=self.run_flow('second')
        self.assertEqual(result['state'],'REVIEW_DONE',result)
        self.assertEqual(observed,[['FLOW_BUSY']])
        self.assertEqual(probe_lock(flow_lifecycle.lock_path(self.root,self.base,'second')),'FREE')
        self.assertEqual((self.root/'src/x.py').read_text(),'VALUE=1\n')

    def test_lifetime_lock_released_on_stage_error(self):
        with patch.object(role_flow,'stage',side_effect=ValueError('injected')):
            result=self.run_flow('failed')
        self.assertEqual(result['state'],'ERROR')
        self.assertEqual(probe_lock(flow_lifecycle.lock_path(self.root,self.base,'failed')),'FREE')

    def test_active_queued_role_blocks_stale_finished_phase(self):
        with Store(self.base['state']).connect() as db:
            db.execute("UPDATE tasks SET state='ROLE_QUEUED' WHERE id=?",(self.result['reviewer']['task'],))
        self.assertIn('ACTIVE_TASK',self.preview()['reasons'])

    def test_backups_are_protected_until_rolled_back(self):
        task=self.result['developer']['task']
        applied=role_manage.promote(self.root,'sample','developer',task,['src/x.py'],True)
        protected=self.preview()
        self.assertIn('RECOVERY_DATA_PROTECTED',protected['reasons'])
        self.assertFalse(any(applied['backup'] in item['path'] for item in protected['delete_entries']))
        self.assertTrue(any(item['reason']=='RECOVERY_DATA_PROTECTED' for item in protected['preserve_entries']))
        role_manage.recover(self.root,'sample','developer',task,applied['backup'],True)
        self.assertTrue(self.preview()['eligible'])
        self.assertEqual((self.root/'src/x.py').read_text(),'VALUE=1\n')

    def test_unknown_file_empty_directory_and_excluded_payload_are_preserved(self):
        path=self.flow/'unexpected.txt'; path.write_text('keep')
        self.assertIn('UNKNOWN_ARTIFACT',self.preview()['reasons'])
        path.unlink()
        unknown=self.flow/'developer-state/unexpected'; unknown.mkdir()
        self.assertIn('UNKNOWN_ARTIFACT',self.preview()['reasons'])
        unknown.rmdir()
        (self.flow/'original-input/.env').write_text('private')
        with self.assertRaisesRegex(ValueError,'snapshot'):
            self.preview()

    def test_plan_hash_changes_when_test_log_changes(self):
        first=self.preview()['plan_hash']
        (self.flow/'test-0.log').write_text('More evidence\n')
        self.assertNotEqual(first,self.preview()['plan_hash'])

    def test_unknown_cache_is_protected_but_python_cache_is_discardable(self):
        home=Path(self.result['developer']['result']['workspace'])
        cache=home/'project/src/__pycache__'; cache.mkdir()
        (cache/'sample.pyc').write_bytes(b'cached data')
        report=self.preview()
        self.assertTrue(report['eligible'],report)
        self.assertTrue(any(i['reason']=='DISCARD_CACHE' for i in report['delete_entries']))
        (cache/'private.txt').write_text('keep')
        self.assertIn('UNEXPECTED_EXCLUDED_DATA',self.preview()['reasons'])

    def test_missing_and_invalid_requests_create_nothing(self):
        before=self.snapshot()
        for flow,days in (('missing',1),('../escape',1),('sample',0),('sample',True)):
            with self.subTest(flow=flow,days=days),self.assertRaises((OSError,ValueError)):
                flow_cleanup.preview(self.root,flow,days=days)
        self.assertEqual(before,self.snapshot())

    def test_directory_link_is_rejected(self):
        link=self.flow/'external-link'
        try: link.symlink_to(self.root/'src',target_is_directory=True)
        except OSError as exc: self.skipTest(str(exc))
        with self.assertRaises(ValueError): self.preview()
        self.assertEqual((self.root/'src/x.py').read_text(),'VALUE=1\n')

    def test_archive_incompatible_path_is_protected(self):
        original=flow_cleanup.archive_path
        def reject(path):
            if path=='src/data.txt': raise ValueError('unsupported fixture path')
            return original(path)
        before=self.snapshot()
        with patch.object(flow_cleanup,'archive_path',side_effect=reject):
            report=self.preview()
        self.assertFalse(report['eligible'])
        self.assertIn('UNSUPPORTED_ARCHIVE_PATH',report['reasons'])
        self.assertFalse(any(i['path'].endswith('/src/data.txt') for i in report['delete_entries']))
        self.assertEqual(before,self.snapshot())

    def test_previous_workspace_prune_is_not_assumed_complete(self):
        task=self.result['developer']['task']
        home=Path(self.result['developer']['result']['workspace'])
        moved=home.with_name(home.name+'-retained')
        home.rename(moved)
        Store(self.base['state']).mark_artifacts(task,'DELETED')
        self.assertIn('PREVIOUSLY_PRUNED',self.preview()['reasons'])
        self.assertTrue(moved.exists())

    def test_wal_rejection_does_not_create_sidecars(self):
        side=self.base['state']/'queue.sqlite3-wal'; side.write_bytes(b'pending')
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'SQLITE_SIDECAR'): self.preview()
        self.assertEqual(before,self.snapshot())

    def test_legacy_coordination_and_overlapping_state_are_not_eligible(self):
        value=json.loads((self.flow/'result.json').read_text()); value.pop('flow_lock_schema')
        atomic_json(self.flow/'result.json',value)
        self.assertIn('UPGRADE_REQUIRED',self.preview()['reasons'])
        raw=json.loads((self.root/'bridge.json').read_text()); raw['state']='.role-flows'
        atomic_json(self.root/'bridge.json',raw)
        with self.assertRaisesRegex(ValueError,'UNSUPPORTED_STATE_LAYOUT'): self.preview()

    def test_failed_checks_have_no_reviewer_and_keep_evidence(self):
        self.policy['checks']=[[sys.executable,'-c',"print('FAILED_CHECK'); raise SystemExit(3)"]]
        atomic_json(self.root/'role_workflow.json',self.policy)
        failed=self.run_flow('failed-tests')
        self.assertEqual(failed['state'],'TESTS_FAILED')
        report=flow_cleanup.preview(self.root,'failed-tests',days=1,now=self.now)
        self.assertTrue(report['eligible'],report)
        self.assertEqual(report['roles']['reviewer']['observation'],'NOT_SUBMITTED')

    def test_private_queues_preserved_and_active_peer_blocks_management(self):
        parent=self.base['state']/'queue.sqlite3'
        for role,relative in (('developer','developer-state'),('reviewer','review-input/.agent-bridge')):
            task=self.result[role]['task']
            private=self.flow/relative/'queue.sqlite3'
            with contextlib.closing(sqlite3.connect(parent)) as source,contextlib.closing(sqlite3.connect(private)) as dest:
                source.backup(dest)
                dest.execute('DELETE FROM tasks WHERE id<>?',(task,))
                dest.execute('DELETE FROM role_tasks')
                dest.commit()
            self.result[role].pop('queue_state')
            submitted=json.loads((self.flow/(role+'-submitted.json')).read_text()); submitted.pop('queue_state')
            atomic_json(self.flow/(role+'-submitted.json'),submitted)
        with contextlib.closing(sqlite3.connect(parent)) as db:
            db.execute('DELETE FROM role_tasks'); db.execute('DELETE FROM tasks'); db.commit()
        self.result.pop('queue_mode'); atomic_json(self.flow/'result.json',self.result)
        before=self.snapshot()
        report=self.preview()
        self.assertTrue(report['eligible'],report)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(sum(i['path'].endswith('queue.sqlite3') for i in report['preserve_entries']),2)
        with Store(self.flow/'review-input/.agent-bridge').connect() as db:
            db.execute("UPDATE tasks SET state='QUEUED'")
        with self.assertRaisesRegex(ValueError,'active private'):
            role_manage.promote(self.root,'sample','developer',self.result['developer']['task'],['src/x.py'])

    def test_installed_cli_preview_and_apply_rejection(self):
        (self.root/'role_workflow.json').unlink()
        connect_project.install(connect_project.plan_install(self.root,sys.executable),True)
        self.assertTrue((self.root/'flow_cleanup.py').is_file())
        command=[sys.executable,'-B',str(self.root/'bridge.py'),'role-manage','prune-flow','--id','sample']
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        before=self.snapshot()
        result=subprocess.run(command,cwd=self.root,env=env,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('RETENTION_PERIOD',json.loads(result.stdout)['reasons'])
        self.assertEqual(before,self.snapshot())
        result=subprocess.run(command+['--apply'],cwd=self.root,env=env,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,2)
        self.assertEqual(before,self.snapshot())


if __name__=='__main__': unittest.main()
