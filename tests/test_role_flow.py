import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from agent_bridge.storage import atomic_json,Store,load_config
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import role_flow

class RoleFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'src').mkdir()
        (self.root/'src/x.py').write_text('VALUE=1\n')
        self.raw=dict(project='.',state='.agent-bridge',include=['src'],writable=['src/x.py'],endpoints={'unused':dict(adapter='command',command=[sys.executable,'-c','pass'])})
        atomic_json(self.root/'bridge.json',self.raw)
        self.policy=dict(developer=dict(adapter='command',command=[sys.executable,'-c',"from pathlib import Path; Path('src/x.py').write_text('VALUE=2\\n'); print('DEV_OK')"]),reviewer=dict(adapter='command',command=[sys.executable,'-c',"from pathlib import Path; import json; assert Path('src/x.py').read_text()=='VALUE=2\\n'; c=json.loads(Path('__bridge_review__/context.json').read_text()); assert c['changes'][0]['path']=='src/x.py'; assert 'VALUE=1' in Path('__bridge_review__/changes.diff').read_text(); print('REVIEW_OK')"]),checks=[[sys.executable,'-c',"from pathlib import Path; assert Path('src/x.py').read_text()=='VALUE=2\\n'"]])
        self.save()
    def save(self):atomic_json(self.root/'role_workflow.json',self.policy)
    def run_flow(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return role_flow.develop_review(self.root,'Change VALUE to 2. Review that exact result.', 'test-flow',allow_mock=True)
    def test_develop_test_review_isolated_no_promotion(self):
        result=self.run_flow()
        self.assertEqual(result['state'],'REVIEW_DONE',result)
        self.assertEqual(result['reviewer']['result']['answer']['text'].strip(),'REVIEW_OK')
        self.assertFalse(result['original_changed_since_snapshot'])
        self.assertFalse(result['automatic_promotion'])
        self.assertEqual((self.root/'src/x.py').read_text(),'VALUE=1\n')
        for role in ['developer','reviewer']:
            c=load_config(result[role]['config'])
            self.assertFalse((c['state']/'queue.sqlite3').exists())
        self.assertEqual(Store(self.root/'.agent-bridge').control()['calls_started'],2)
        self.assertEqual(result['queue_mode'],'SHARED_PARENT')
        with self.assertRaises(FileExistsError): self.run_flow()
    def test_saved_result_read_does_not_launch_agents(self):
        result=self.run_flow()
        out=io.StringIO()
        with patch.object(sys,'argv',['role_flow','--project',str(self.root),'--result','--id','test-flow']), patch.object(role_flow,'develop_review',side_effect=AssertionError('must not run')), contextlib.redirect_stdout(out):
            self.assertEqual(role_flow.main(),0)
        saved=json.loads(out.getvalue())
        self.assertFalse(saved['review_is_approval'])
        self.assertEqual(saved['reviewer']['answer']['text'].strip(),'REVIEW_OK')
        self.assertNotIn('payload',json.dumps(saved))
        result['reviewer']['result']['answer']['text'] = '\uac80\ud1a0 \uc644\ub8cc \u2705'
        atomic_json(self.root/'.role-flows/test-flow/result.json', result)
        proc = subprocess.run([sys.executable, '-X', 'utf8=0', role_flow.__file__, '--project', str(self.root), '--result', '--id', 'test-flow'],
                              env=dict(os.environ, PYTHONIOENCODING='cp949', PYTHONUTF8='0'), capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.decode('utf-8'))['reviewer']['answer']['text'], '\uac80\ud1a0 \uc644\ub8cc \u2705')
    def test_reviewer_reads_all_test_logs_from_its_own_workspace(self):
        outputs = [b'check one\r\n\xff\n', b'second check\n']
        self.policy['checks'] = [[sys.executable, '-c',
            'import sys; sys.stdout.buffer.write(' + repr(data) + ')'] for data in outputs]
        self.policy['reviewer']['command'] = [sys.executable, '-c',
            "import json; from pathlib import Path; "
            "c=json.loads(Path('__bridge_review__/context.json').read_text()); "
            "assert [Path(x['log']).read_bytes() for x in c['checks']]==" + repr(outputs) + "; "
            "assert all(x['exit_code']==0 for x in c['checks']); print('LOGS_READ_OK')"]
        self.save()
        result = self.run_flow()
        self.assertEqual(result['state'], 'REVIEW_DONE', result)
        self.assertEqual(result['reviewer']['result']['answer']['text'].strip(), 'LOGS_READ_OK')
        self.assertEqual(result['reviewer']['result']['changes'], [])
        flow = self.root / '.role-flows/test-flow'
        self.assertEqual([c['log'] for c in result['checks']], ['test-0.log', 'test-1.log'])
        for check, expected in zip(result['checks'], outputs):
            self.assertEqual((flow / check['log']).read_bytes(), expected)
        self.assertEqual(Store(self.root / '.agent-bridge').control()['calls_started'], 2)
    def test_result_missing_does_not_create_workflow(self):
        with patch.object(sys,'argv',['role_flow','--project',str(self.root),'--result','--id','missing']),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(role_flow.main(),2)
        self.assertFalse((self.root/'.role-flows').exists())
    def test_developer_failure_stops_review(self):
        self.policy['developer']['command']=[sys.executable,'-c','raise SystemExit(7)']
        self.save()
        result=self.run_flow()
        self.assertEqual(result['state'],'DEVELOPER_FAILED')
        self.assertNotIn('reviewer',result)
    def test_test_failure_stops_review(self):
        self.policy['checks']=[[sys.executable,'-c','raise SystemExit(3)']]
        self.save()
        result=self.run_flow()
        self.assertEqual(result['state'],'TESTS_FAILED')
        self.assertNotIn('reviewer',result)
    def test_reviewer_mutation_detected_and_developer_preserved(self):
        self.policy['reviewer']['command']=[sys.executable,'-c',"from pathlib import Path; Path('src/x.py').write_text('VALUE=3\\n')"]
        self.save()
        result=self.run_flow()
        self.assertEqual(result['state'],'REVIEWER_MODIFIED_FILES')
        dev=Path(result['developer']['result']['workspace'])/'project/src/x.py'
        self.assertEqual(dev.read_text(),'VALUE=2\n')
        self.assertEqual((self.root/'src/x.py').read_text(),'VALUE=1\n')
    def test_parent_pause_and_cap_do_not_get_bypassed(self):
        store=Store(self.root/'.agent-bridge')
        store.pause(True)
        with self.assertRaises(ValueError):self.run_flow()
        store.pause(False)
        store.set_limit(0)
        with self.assertRaises(ValueError):self.run_flow()
        self.assertFalse((self.root/'.role-flows').exists())
    def test_reviewer_permission_validation(self):
        self.assertFalse(role_flow.readonly(self.policy['reviewer']))
        self.assertTrue(role_flow.readonly(dict(adapter='claude',command=['claude','--tools','Read'])))
        self.assertFalse(role_flow.readonly(dict(adapter='claude',command=['claude','--tools','Read,Edit'])))
        self.assertTrue(role_flow.readonly(dict(adapter='codex',sandbox='read-only',command=['codex'])))
        with self.assertRaises(ValueError): role_flow.develop_review(self.root,'test','invalid')
    def test_shared_cap_two_allows_both_roles_and_one_stops_before_review(self):
        store=Store(self.root/'.agent-bridge')
        store.set_limit(1)
        result=self.run_flow()
        self.assertEqual(result['state'],'ERROR')
        self.assertEqual(result['developer']['state'],'DONE')
        self.assertNotIn('reviewer',result)
        self.assertEqual(store.control()['calls_started'],1)
        self.assertEqual(len(store.rows()),1)
        store.set_limit(3)
        with contextlib.redirect_stderr(io.StringIO()):
            result=role_flow.develop_review(self.root,'Change VALUE to 2.', 'second-flow',allow_mock=True)
        self.assertEqual(result['state'],'REVIEW_DONE',result)
        self.assertEqual(store.control()['calls_started'],3)
    def test_resident_runner_executes_shared_flow(self):
        from agent_bridge.health import inspect
        store=Store(self.root/'.agent-bridge')
        store.set_limit(2)
        process=subprocess.Popen([sys.executable,'-m','agent_bridge','--config',str(self.root/'bridge.json'),'run'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                status=inspect(self.root/'.agent-bridge')
                if status['lock_held'] and status['heartbeat_status']=='FRESH':break
                if process.poll() is not None:self.fail(process.stderr.read().decode())
                time.sleep(.05)
            else:self.fail('Runner startup timed out')
            resident_pid=status['health']['pid']
            result=self.run_flow()
            self.assertEqual(result['state'],'REVIEW_DONE',result)
            self.assertEqual(store.control()['calls_started'],2)
            self.assertEqual(inspect(self.root/'.agent-bridge')['health']['pid'],resident_pid)
        finally:
            process.terminate()
            process.communicate(timeout=15)
if __name__=='__main__':unittest.main(verbosity=2)
