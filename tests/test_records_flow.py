"""Shared-flow body deletion preserves execution controls and rejects uncertain ownership."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_bridge import management, p1, records_body as body, records_flow as flow, records_guard as guard
from agent_bridge.storage import Store, atomic_json, load_config

SECRET='PRIVATE_FLOW_BODY'


@unittest.skipUnless(os.name=='nt','Pinned file deletion is Windows-only')
class RecordsFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); (self.root/'input.txt').write_text(SECRET)
        endpoint=dict(adapter='command',command=[sys.executable,'-c',"print('PRIVATE_FLOW_BODY')"])
        atomic_json(self.root/'bridge.json',dict(project='.',include=['input.txt'],writable=['input.txt'],endpoints={'mock':endpoint}))
        atomic_json(self.root/'role_workflow.json',dict(developer=endpoint,reviewer=endpoint,checks=[[sys.executable,'-c','pass']]))
        self.config=load_config(self.root/'bridge.json'); self.api=management.Session(); self.addCleanup(self.api.close); self.sequence=0

    def request(self,action,**args):
        self.sequence+=1
        return self.api.handle(dict(protocol=1,id=str(self.sequence),action=action,args=args))

    def call(self,action,**args):
        reply=self.request(action,**args); self.assertTrue(reply['ok'],reply); return reply['result']

    def scope(self,**args):
        return dict(project=str(self.root),config=str(self.root/'bridge.json'),**args)

    def apply(self,plan):
        self.assertTrue(plan['plan_id'],plan)
        return self.call('records.apply',plan_id=plan['plan_id'],apply=True)

    def prepare(self):
        self.apply(self.call('records.prepare.preview',**self.scope()))
        return self.apply(self.call('records.roles.prepare.preview',**self.scope()))

    def ready(self,name='flow-a'):
        if not flow.supported(self.config): self.prepare()
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result=p1.helper('role_flow').develop_review(self.root,SECRET,name,allow_mock=True)
        self.assertEqual(result['state'],'REVIEW_DONE',result)
        return result

    def preview(self,name='flow-a'):
        return self.call('records.flow.purge.preview',**self.scope(flow_id=name))

    def test_upgrade_is_explicit_and_old_connection_is_fenced(self):
        self.apply(self.call('records.prepare.preview',**self.scope()))
        with self.assertRaisesRegex(ValueError,'ROLE_QUEUE_UPGRADE_REQUIRED'): p1.helper('role_flow').develop_review(self.root,SECRET,'before',allow_mock=True)
        old=sqlite3.connect(self.config['state']/'queue.sqlite3'); self.addCleanup(old.close)
        old.create_function('records_actor',0,lambda:'@runtime')
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        plan=self.call('records.roles.prepare.preview',**self.scope())
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.apply(plan)
        self.assertEqual(guard.binding(self.config)['version'],2)
        with self.assertRaises(sqlite3.Error): old.execute("INSERT INTO role_tasks VALUES ('x','f','developer','{}')")

    def test_complete_flow_scrubs_both_roles_and_preserves_ids_usage_original(self):
        result=self.ready(); self.ready('flow-b'); store=Store(self.config['state'])
        before=store.control(); other=[r for r in store.rows() if r['id'] in {result['developer']['task'],result['reviewer']['task']}]
        with store.connect() as db:
            usage=[tuple(r) for r in db.execute('SELECT * FROM usage_snapshots ORDER BY id')]
            identities=[tuple(r) for r in db.execute('SELECT id,flow,role FROM role_tasks ORDER BY id')]
        keep={p:p.read_bytes() for p in (self.root/'.role-flows/flow-b').rglob('*') if p.is_file()}
        deleted=self.apply(self.preview()); self.assertTrue(deleted['body_deleted'],deleted)
        self.assertEqual(deleted['original_phase'],'REVIEW_DONE')
        self.assertEqual(store.control(),before)
        with store.connect() as db:
            self.assertEqual(usage,[tuple(r) for r in db.execute('SELECT * FROM usage_snapshots ORDER BY id')])
            self.assertEqual(identities,[tuple(r) for r in db.execute('SELECT id,flow,role FROM role_tasks ORDER BY id')])
            for table,column in [('tasks','prompt'),('tasks','result'),('role_tasks','data'),('context_plans','data')]:
                for item in other: self.assertNotIn(SECRET,db.execute('SELECT '+column+' FROM '+table+' WHERE id=?',(item['id'],)).fetchone()[0])
        self.assertEqual(keep,{p:p.read_bytes() for p in (self.root/'.role-flows/flow-b').rglob('*') if p.is_file()})
        self.assertEqual((self.root/'input.txt').read_text(),SECRET)
        self.assertEqual(list((self.root/'.role-flows/flow-a').iterdir()),[self.root/'.role-flows/flow-a/result.json'])
        self.assertNotIn(SECRET,json.dumps(deleted))

    def test_flow_writer_blocks_until_final_close_and_lost_is_not_reset(self):
        self.ready(); owner=flow.reserve(self.config,'other')
        with flow.Writer(self.config,'other',owner):
            self.assertIn('FLOW_WRITER_ACTIVE',self.preview()['report']['blockers'])
        self.assertTrue(self.preview()['plan_id'])
        owner=flow.reserve(self.config,'lost')
        with self.assertRaises(RuntimeError):
            with flow.Writer(self.config,'lost',owner): raise RuntimeError('stop')
        self.assertIn('FLOW_WRITER_LOST',self.preview()['report']['blockers'])

    def test_role_task_cannot_be_deleted_separately_or_relaunched(self):
        result=self.ready()
        plan=self.call('records.purge.preview',**self.scope(task_ids=[result['developer']['task']]))
        self.assertIn('WHOLE_FLOW_REQUIRED',plan['report']['blockers'])
        self.apply(self.preview())
        with self.assertRaises((ValueError,sqlite3.Error)): p1.helper('role_flow').develop_review(self.root,SECRET,'flow-a',allow_mock=True)
        for name,args in [('role_review',(self.root,'flow-a','developer')),('flow_archive',(self.root,'flow-a'))]:
            with self.assertRaisesRegex(ValueError,'BODY_DELETED'):
                (p1.helper(name).inspect if name=='role_review' else p1.helper(name).load_snapshot)(*args)
        listing=self.call('records.inventory',**self.scope(kind='flow'))
        self.assertFalse(listing['items'][0]['body_selectable']); self.assertEqual(listing['items'][0]['body_state'],'DELETED')

    def test_partial_file_failure_and_explicit_resume_without_replay(self):
        self.ready(); plan=self.preview()
        with patch.object(flow,'guarded_removal',side_effect=OSError('locked')):
            response=self.request('records.apply',plan_id=plan['plan_id'],apply=True)
            self.assertFalse(response['ok']); result=response['result']
        self.assertEqual(result['state'],'PARTIAL')
        with self.assertRaisesRegex(ValueError,'BODY_PURGE_PENDING'): p1.helper('role_review').flow_header(self.root,'flow-a')
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        operation=self.call('records.operation',project=str(self.root),database=str(self.config['state']/'queue.sqlite3'),operation=result['operation'])
        self.assertFalse(operation['complete'])
        resumed=self.apply(self.call('records.resume.preview',**self.scope(operation=result['operation'])))
        self.assertTrue(resumed['body_deleted'],resumed)

    def test_changed_files_backup_private_queue_and_partial_archive_block(self):
        self.ready(); home=self.root/'.role-flows/flow-a'; plan=self.preview()
        (home/'added.txt').write_text('new')
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        private=home/'developer-state/queue.sqlite3'; private.write_text('not owned')
        self.assertIn('PRIVATE_QUEUE_UNCONFIRMED',self.preview()['report']['blockers']); private.unlink()
        atomic_json(home/'cleanup.json',dict(state='ARCHIVE_FAILED'))
        self.assertIn('ARCHIVE_INCOMPLETE',self.preview()['report']['blockers']); (home/'cleanup.json').unlink()
        (home/'backup-user').mkdir()
        reply=self.request('records.flow.purge.preview',**self.scope(flow_id='flow-a'))
        self.assertFalse(reply['ok']); self.assertEqual(reply['problem']['code'],'RECOVERY_OR_EXCLUDED_DATA')

    def test_legacy_flow_is_not_adopted(self):
        self.apply(self.call('records.prepare.preview',**self.scope()))
        (self.root/'.role-flows/legacy').mkdir(parents=True)
        self.assertFalse(self.call('records.roles.prepare.preview',**self.scope())['plan_id'])

    def test_unprepared_flow_without_queue_is_readonly_and_blocked(self):
        (self.root/'.role-flows/legacy').mkdir(parents=True)
        (self.root/'.role-flows/legacy/result.json').write_text('{}')
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=self.preview('legacy')
        self.assertIsNone(result['plan_id']); self.assertIn('LEGACY_OWNERSHIP_UNCONFIRMED',result['report']['blockers'])
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_oversized_flow_metadata_is_rejected_before_parsing(self):
        self.ready(); path=self.root/'.role-flows/flow-a/result.json'
        path.write_text(json.dumps({'value':'x'*(body.records.MAX_JSON_BYTES+1)}))
        reply=self.request('records.flow.purge.preview',**self.scope(flow_id='flow-a'))
        self.assertFalse(reply['ok']); self.assertEqual(reply['problem']['code'],'METADATA_LIMIT')

    def test_mutations_after_preview_and_unknown_operation_do_not_scrub(self):
        self.ready(); plan=self.preview(); owner=flow.reserve(self.config,'accepted')
        self.assertTrue(owner)
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        self.assertIn(SECRET,(self.root/'.role-flows/flow-a/review-input/__bridge_review__/context.json').read_text())

    def test_actual_p1_job_lifetime_receipt_and_payload_removal(self):
        self.prepare()
        script=self.root/'mock_codex.py'
        script.write_text("import json,sys\nfrom pathlib import Path\ntext=sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('PRIVATE_FLOW_BODY')\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':3}}))\n",encoding='utf-8')
        endpoint=dict(adapter='codex',command=[sys.executable,str(script)],sandbox='read-only')
        atomic_json(self.root/'role_workflow.json',dict(developer=endpoint,reviewer=endpoint,checks=[[sys.executable,'-c','pass']]))
        preview=self.call('roles.preview',config=str(self.root/'bridge.json'),flow_id='p1-flow',prompt=SECRET)
        accepted=self.call('operation.apply',plan_id=preview['plan_id'],apply=True)
        self.assertEqual(accepted['state'],'ACCEPTED')
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            value=flow.status(self.config,'p1-flow',live=True)
            if value['writer_state']=='CLOSED': break
            time.sleep(.05)
        self.assertEqual(value['writer_state'],'CLOSED')
        ticket=json.loads((self.config['state']/'p1-jobs/p1-flow.json').read_text())
        self.assertEqual(ticket['state'],'FINISHED',ticket); self.assertEqual(ticket['workflow_state'],'REVIEW_DONE',ticket)
        controls=Store(self.config['state']).control(); self.assertEqual(controls['calls_started'],2)
        result=self.apply(self.preview('p1-flow'))
        self.assertTrue(result['body_deleted'],result)
        for suffix in ('.json','.txt','.log'): self.assertFalse((self.config['state']/'p1-jobs'/('p1-flow'+suffix)).exists())
        self.assertEqual(Store(self.config['state']).control(),controls)
        self.assertFalse(self.request('roles.preview',config=str(self.root/'bridge.json'),flow_id='p1-flow',prompt=SECRET)['ok'])

    def test_completed_archive_is_removed_without_rebuilding_its_body(self):
        (self.root/'input.txt').write_text(SECRET*2000)
        self.ready(); future=time.time()+40*86400
        preview=p1.helper('flow_cleanup').preview(self.root,'flow-a',1,now=future)
        self.assertTrue(preview['eligible'],preview['reasons'])
        p1.helper('flow_archive_writer').write_archive(self.root,'flow-a',preview['plan_hash'],1,now=future)
        self.assertTrue((self.root/'.role-flows/flow-a/archive/records.json').exists())
        self.assertTrue(self.apply(self.preview())['body_deleted'])
        with self.assertRaisesRegex(ValueError,'BODY_DELETED'):
            p1.helper('flow_cleanup').preview(self.root,'flow-a',1,now=future)

    def test_resume_rejects_new_files_in_quarantine(self):
        self.ready(); plan=self.preview()
        with patch.object(flow,'guarded_removal',side_effect=OSError('locked')):
            result=self.request('records.apply',plan_id=plan['plan_id'],apply=True)['result']
        (self.config['state']/'record-bodies'/result['operation']/'flow/new.txt').write_text('new')
        reply=self.request('records.resume.preview',**self.scope(operation=result['operation']))
        self.assertFalse(reply['ok']); self.assertEqual(reply['problem']['code'],'PLAN_CHANGED')

    def test_control_commit_failure_does_not_report_success(self):
        self.ready(); original=guard.connect; calls=0
        @contextlib.contextmanager
        def fail_final(*args,**kwargs):
            nonlocal calls
            with original(*args,**kwargs) as db:
                yield db
                if db.execute("SELECT 1 FROM records_flows WHERE body_state='DELETED'").fetchone():
                    calls+=1
                    if calls==1: raise sqlite3.OperationalError('commit rejected')
        plan=self.preview()
        with patch.object(guard,'connect',fail_final):
            result=self.request('records.apply',plan_id=plan['plan_id'],apply=True)['result']
        self.assertEqual(result['state'],'PARTIAL'); self.assertFalse(result['body_deleted'])
        self.assertEqual(flow.status(self.config,'flow-a')['body_state'],'PURGING')
        result=self.apply(self.call('records.resume.preview',**self.scope(operation=result['operation'])))
        self.assertEqual(result['state'],'DONE')

    def test_unowned_files_and_external_task_consumers_block_purge(self):
        result=self.ready(); store=Store(self.config['state'],config=self.config)
        store.submit('consumer',['mock'],'x',self.config['endpoints'],{'payload':{'summaries':[{'record':{'provenance':{'source_task_id':result['developer']['task']}}}]}})
        self.assertIn('RELATED_TASKS_REQUIRED',self.preview()['report']['blockers'])
        (self.root/'.role-flows/unowned').mkdir()
        self.assertIn('ROLE_SCOPE_UNKNOWN',self.preview()['report']['blockers'])

    def test_cancelled_unclaimed_role_uses_closed_flow_lifetime(self):
        self.prepare(); helper=p1.helper('role_flow')
        def pause(*args,**kwargs):
            Store(self.config['state']).pause(True)
            return 2
        with patch.object(helper,'run',side_effect=pause),contextlib.redirect_stderr(io.StringIO()):
            result=helper.develop_review(self.root,SECRET,'cancelled-flow',allow_mock=True)
        self.assertEqual(result['state'],'DEVELOPER_FAILED',result)
        self.assertEqual(result['developer']['state'],'CANCELLED')
        removed=self.apply(self.preview('cancelled-flow'))
        self.assertTrue(removed['body_deleted'],removed)
        self.assertEqual(Store(self.config['state']).control()['calls_started'],0)


if __name__=='__main__': unittest.main()
