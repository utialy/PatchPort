import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
import test_context_plan as fixtures
from agent_bridge import context, workspace
from agent_bridge.cleanup import prune
from agent_bridge.runner import run
from agent_bridge.storage import Store, FileLock
from agent_bridge.usage import report

class ExecutionPlanTests(unittest.TestCase):
    setUp=fixtures.PlanTests.setUp
    save=fixtures.PlanTests.save
    write=fixtures.PlanTests.write
    preview=fixtures.PlanTests.preview
    call=fixtures.PlanTests.call
    def submit(self,batch='work',targets=None):
        targets=targets or ['one']
        frozen=context.freeze_plan(self.c,'request',self.preview(),targets)
        Store(self.c['state']).submit(batch,targets,'request',self.c['endpoints'],frozen)
        return frozen
    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(self.c,True)
    def row(self,batch='work'):
        return Store(self.c['state']).rows(batch)[0]
    def test_selected_copy_and_plan_file_change(self):
        frozen=self.submit()
        self.plan_path.write_text('broken')
        self.assertEqual(self.execute(),0)
        row=self.row()
        self.assertEqual(row['state'],'DONE')
        result=json.loads(row['result'])
        home=Path(result['workspace'])
        self.assertFalse((home/'project/src/blob').exists())
        self.assertEqual((home/'project/src/a').read_bytes(),(self.root/'src/a').read_bytes())
        self.assertNotIn('src/blob',json.loads((home/'baseline.json').read_text()))
        self.assertEqual(result['changes'],[])
        self.assertEqual(result['context']['validation'],'VALID')
        self.assertEqual(result['context']['plan_sha256'],frozen['plan_sha256'])
        code,out,_=self.call('result','--id','work')
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out)[0]['context_plan'],frozen)
    def test_original_and_candidate_changes_block_without_call(self):
        for name in ['src/a','HANDOFF.md','src/new']:
            batch=name.replace('/','-')
            frozen=self.submit(batch)
            path=self.root/name
            before=path.read_bytes() if path.exists() else None
            path.write_bytes(b'changed')
            with patch('agent_bridge.runner.subprocess.Popen') as launch:
                self.execute()
                launch.assert_not_called()
            row=self.row(batch)
            self.assertEqual(row['state'],'ERROR')
            self.assertEqual(json.loads(row['result'])['context']['validation'],'FAILED')
            if before is None: path.unlink()
            else: path.write_bytes(before)
    def test_omitted_contents_may_change(self):
        self.submit()
        (self.root/'src/blob').write_bytes(b'new omitted')
        self.execute()
        self.assertEqual(self.row()['state'],'DONE')

    def test_changed_bridge_instructions_reject_pending_plan(self):
        self.submit()
        with patch.object(context, 'PREFIX', context.PREFIX + 'Changed instructions.\n'), \
             patch('agent_bridge.runner.subprocess.Popen') as launch:
            self.execute()
            launch.assert_not_called()
        self.assertEqual(self.row()['state'], 'ERROR')
        self.assertIn('prompt changed', json.loads(self.row()['result'])['error'])
    def test_settings_endpoint_and_prompt_changes_block(self):
        for case in ['budget','writable','endpoint','prompt']:
            self.submit(case)
            altered=copy.deepcopy(self.c)
            if case=='budget': altered['context_budget']={'max_bytes':99999}
            if case=='writable': altered['writable']=['src/a']
            if case=='endpoint': altered['endpoints']['one']['command']+=['extra']
            if case=='prompt':
                with Store(self.c['state']).connect() as db:
                    db.execute('UPDATE tasks SET prompt=? WHERE batch=?',('changed',case))
            with patch('agent_bridge.runner.subprocess.Popen') as launch:
                run(altered,True)
                launch.assert_not_called()
            self.assertEqual(self.row(case)['state'],'ERROR')
    def test_copy_race_blocks_provider(self):
        self.submit()
        original=workspace.create
        def changed(config,id_,selected=None):
            home=original(config,id_,selected)
            (home/'project/src/a').write_bytes(b'changed')
            return home
        with patch('agent_bridge.runner.workspace.create',side_effect=changed),patch('agent_bridge.runner.subprocess.Popen') as launch:
            self.execute()
            launch.assert_not_called()
        self.assertEqual(self.row()['state'],'ERROR')
        self.assertEqual(json.loads(self.row()['result'])['context']['validation'],'FAILED')
    def test_missing_or_corrupt_plan_fails_closed(self):
        for case in ['missing','corrupt']:
            self.submit(case)
            with Store(self.c['state']).connect() as db:
                if case=='missing': db.execute('DELETE FROM context_plans WHERE id=?',(case+'--one',))
                else: db.execute('UPDATE context_plans SET data=? WHERE id=?',('{',case+'--one'))
            with patch('agent_bridge.runner.subprocess.Popen') as launch:
                self.execute()
                launch.assert_not_called()
            self.assertEqual(self.row(case)['state'],'ERROR')
            self.assertEqual(json.loads(self.row(case)['result'])['context']['validation'],'FAILED')
            code,out,_=self.call('result','--id',case)
            self.assertEqual(code,2)
            self.assertEqual(json.loads(out)[0]['state'],'ERROR')
    def test_fanout_transaction_rolls_back_plan_and_tasks(self):
        self.c['endpoints']['two']=copy.deepcopy(self.c['endpoints']['one'])
        frozen=context.freeze_plan(self.c,'request',self.preview(),['one','two'])
        store=Store(self.c['state'])
        with store.connect() as db:
            db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON context_plans WHEN NEW.id='batch--two' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.submit('batch',['one','two'],'request',self.c['endpoints'],frozen)
        self.assertEqual(store.rows(),[])
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM context_plans').fetchone()[0],0)
    def test_legacy_sql_cannot_claim_or_recover_plan(self):
        self.submit()
        store=Store(self.c['state'])
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks WHERE state='QUEUED'").fetchone()[0],0)
            self.assertEqual(db.execute("UPDATE tasks SET state='RUNNING' WHERE state='QUEUED'").rowcount,0)
        self.assertTrue(store.claim('work--one'))
        self.assertFalse(store.claim('work--one'))
        with store.connect() as db:
            self.assertEqual(db.execute("UPDATE tasks SET state='INTERRUPTED' WHERE state='RUNNING'").rowcount,0)
        self.assertEqual(report(store)['summary']['states'],{'RUNNING':1})
        store.recover()
        self.assertEqual(self.row()['state'],'INTERRUPTED')
        self.assertEqual(store.control()['calls_started'],1)
        self.assertIsNotNone(store.context_plan('work--one'))
    def test_pause_limit_and_runner_lock(self):
        self.submit()
        store=Store(self.c['state'])
        store.pause()
        self.assertEqual(self.execute(),2)
        self.assertEqual(self.row()['state'],'QUEUED')
        store.pause(False)
        store.set_limit(0)
        self.assertEqual(self.execute(),2)
        store.set_limit(None)
        with FileLock(self.c['state']/'runner.lock'):
            with self.assertRaises(RuntimeError): self.execute()
        self.execute()
        self.assertEqual(self.row()['state'],'DONE')
        self.assertEqual(store.control()['calls_started'],1)
    def test_omitted_path_recreation_cannot_overwrite_original(self):
        self.c['endpoints']['one']['command'][-1]="from pathlib import Path; Path('src/blob').write_bytes(b'proposal'); print('ok')"
        self.submit()
        self.execute()
        row=self.row()
        self.assertEqual(row['state'],'DONE')
        changes=json.loads(row['result'])['changes']
        self.assertEqual(changes[0]['path'],'src/blob')
        self.assertIsNone(changes[0]['before'])
        with self.assertRaisesRegex(ValueError,'Original changed'):
            workspace.review(self.c,'work--one',['src/blob'])
        self.assertEqual((self.root/'src/blob').read_bytes(),bytes(range(256)))
    def test_prune_retains_stored_plan(self):
        frozen=self.submit()
        self.execute()
        store=Store(self.c['state'])
        home=Path(json.loads(self.row()['result'])['workspace'])
        old=time.time()-40*86400
        for path in sorted(home.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            os.utime(path,(old,old))
        os.utime(home,(old,old))
        with store.connect() as db:
            db.execute('UPDATE tasks SET finished=?',(old,))
        result=prune(self.c,30,['work--one'],True)
        self.assertTrue(result['tasks'][0]['deleted'])
        self.assertEqual(store.context_plan('work--one'),frozen)
