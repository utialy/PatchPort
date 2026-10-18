"""Reviewed cleanup composition preserves control data across interruption and config removal."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import connect,management,p1,project_cleanup as cleanup,records_body,connection_remove
from agent_bridge.runner import run
from agent_bridge.storage import Store,atomic_json,load_config


@unittest.skipUnless(os.name=='nt','Pinned file removal is Windows-only')
class ProjectCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();(self.root/'input.txt').write_text('ORIGINAL')
        endpoint=dict(adapter='command',command=[sys.executable,'-c',"print('PRIVATE_BODY')"])
        atomic_json(self.root/'bridge.json',dict(project='.',include=['input.txt'],writable=['input.txt'],endpoints={'mock':endpoint}))
        atomic_json(self.root/'role_workflow.json',dict(developer=endpoint,reviewer=endpoint,checks=[[sys.executable,'-c','pass']]))
        # The runtime uses fixed helpers; this fixture policy is explicitly mock-only.
        with patch('agent_bridge.role_policy.readonly',return_value=True): connect.install(connect.plan_install(self.root,sys.executable),True)
        self.config=load_config(self.root/'bridge.json'); self.database=self.config['state']/'queue.sqlite3'
        self.api=management.Session(); self.addCleanup(lambda:self.api.close()); self.serial=0
        plan=self.call('records.prepare.preview',**self.scope())
        self.call('records.apply',plan_id=plan['plan_id'],apply=True)

    def scope(self,**extra):return dict(project=str(self.root),config=str(self.root/'bridge.json'),**extra)
    def receipt(self,operation):return dict(project=str(self.root),database=str(self.database),operation=operation)
    def request(self,action,**args):
        self.serial+=1;return self.api.handle(dict(protocol=1,id=str(self.serial),action=action,args=args))
    def call(self,action,**args):
        result=self.request(action,**args);self.assertTrue(result['ok'],result);return result['result']
    def apply(self,plan):
        self.assertTrue(plan['plan_id'],plan);return self.call('cleanup.apply',plan_id=plan['plan_id'],apply=True)
    def preview(self,selection):return self.call('cleanup.preview',**self.scope(selection=selection))
    def task(self,name='task'):
        store=Store(self.config['state'],config=self.config);task=store.submit(name,['mock'],'PRIVATE_REQUEST',self.config['endpoints'])[0]
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(run(self.config,True),0)
        return task
    def history(self):
        args=dict(database=str(self.database),project=str(self.root))
        self.call('interactive.owner.open',**args,owner='owner',apply=True)
        self.call('interactive.begin',**args,owner='owner',session_id='session',node='node',provider='codex',name='PRIVATE_NAME',apply=True)
        self.call('interactive.record',database=str(self.database),owner='owner',session_id='session',event_id='body',kind='completed',data={'answer':'PRIVATE_BODY'},apply=True)
        self.call('interactive.record',database=str(self.database),owner='owner',session_id='session',event_id='stop',kind='session_stopped',data={'confirmed_exit':True},apply=True)
        self.call('interactive.end',database=str(self.database),owner='owner',apply=True)
        return dict(database=str(self.database),session_ids=['session'])
    def flow(self):
        plan=self.call('records.roles.prepare.preview',**self.scope());self.call('records.apply',plan_id=plan['plan_id'],apply=True)
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result=p1.helper('role_flow').develop_review(self.root,'PRIVATE_BODY','flow',allow_mock=True)
        self.assertEqual(result['state'],'REVIEW_DONE',result)
        return 'flow'
    def tree(self):return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_full_reviewed_order_and_config_free_receipt(self):
        flow=self.flow(); task=self.task(); history=self.history(); store=Store(self.config['state']);store.set_limit(9);store.pause(True);before=store.control()
        selection=dict(history=[history],task_ids=[task],flow_ids=[flow],paths=['bridge.py','bridge.json'])
        initial=self.tree();plan=self.preview(selection);self.assertEqual(initial,self.tree())
        self.assertEqual([s['kind'] for s in plan['report']['stages']],['history','tasks','flow','connection'])
        result=self.apply(plan);op=result['operation'];self.assertFalse(result['scope_complete']);self.assertTrue((self.root/'bridge.json').exists())
        for expected in ('tasks','flow','connection'):
            plan=self.call('cleanup.next.preview',**self.receipt(op));self.assertEqual(plan['report']['next_kind'],expected)
            result=self.apply(plan)
        self.assertTrue(result['scope_complete']);self.assertFalse((self.root/'bridge.json').exists());self.assertEqual(before,store.control())
        self.assertEqual(result['residual']['task_bodies'],0);self.assertTrue(result['residual']['control_ledger_retained']);self.assertFalse(result['residual']['forensic_erasure'])
        self.api.close();self.api=management.Session()
        self.assertTrue(self.call('cleanup.operation',**self.receipt(op))['scope_complete'])
        self.assertEqual(self.call('cleanup.inventory',project=str(self.root),database=str(self.database))['operations'][0]['operation'],op)
        self.assertEqual((self.root/'input.txt').read_text(),'ORIGINAL')

    def test_partial_stage_resumes_original_io_and_never_reexecutes_task(self):
        task=self.task();plan=self.preview(dict(task_ids=[task],paths=['bridge.json']))
        with patch.object(records_body,'guarded_removal',side_effect=OSError('locked')):
            reply=self.request('cleanup.apply',plan_id=plan['plan_id'],apply=True)
        self.assertFalse(reply['ok']);result=reply['result'];op=result['operation'];child=result['stages'][0]['operation']
        self.assertEqual(result['state'],'PARTIAL');self.assertTrue((self.root/'bridge.json').exists())
        next_=self.call('cleanup.next.preview',**self.receipt(op));result=self.apply(next_)
        self.assertEqual(result['stages'][0]['operation'],child);self.assertEqual(result['completed_stages'],1)
        self.assertEqual(Store(self.config['state']).control()['calls_started'],1)

    def test_missing_child_receipt_requires_new_explicit_preview(self):
        task=self.task();plan=self.preview(dict(task_ids=[task]))
        real=cleanup.child
        def fail_apply(session,step,action,args):
            if action=='records.apply':raise OSError('before acceptance')
            return real(session,step,action,args)
        with patch.object(cleanup,'child',side_effect=fail_apply):reply=self.request('cleanup.apply',plan_id=plan['plan_id'],apply=True)
        self.assertFalse(reply['ok']);op=reply['result']['operation'];previous=reply['result']['stages'][0]['operation']
        plan=self.call('cleanup.next.preview',**self.receipt(op));result=self.apply(plan)
        self.assertTrue(result['scope_complete']);self.assertNotEqual(result['stages'][0]['operation'],previous)
        self.assertEqual(Store(self.config['state']).control()['calls_started'],1)

    def test_response_loss_is_resolved_before_next_stage(self):
        task=self.task();plan=self.preview(dict(task_ids=[task],paths=['bridge.py']))
        real=cleanup.child
        def lost(session,step,action,args):
            value=real(session,step,action,args)
            if action=='records.apply':raise OSError('response lost')
            return value
        with patch.object(cleanup,'child',side_effect=lost):reply=self.request('cleanup.apply',plan_id=plan['plan_id'],apply=True)
        op=reply['result']['operation'];self.assertEqual(reply['result']['completed_stages'],1)
        next_=self.call('cleanup.next.preview',**self.receipt(op));self.assertEqual(next_['report']['next_kind'],'connection')
        self.assertTrue(self.apply(next_)['scope_complete'])

    def test_final_receipt_failure_has_explicit_finalize_without_deleting_again(self):
        task=self.task();plan=self.preview(dict(task_ids=[task]));real=cleanup.save
        def fail_done(data,**args):
            if data['phase']=='DONE':raise OSError('receipt failed')
            return real(data,**args)
        with patch.object(cleanup,'save',side_effect=fail_done):reply=self.request('cleanup.apply',plan_id=plan['plan_id'],apply=True)
        self.assertEqual(reply['result']['state'],'FINALIZE_PENDING');op=reply['result']['operation']
        next_=self.call('cleanup.next.preview',**self.receipt(op));self.assertEqual(next_['report']['next_kind'],'finalize')
        with patch.object(cleanup,'child',side_effect=AssertionError('No child action allowed')):self.assertTrue(self.apply(next_)['scope_complete'])

    def test_selection_outside_cleanup_scope_blocks_connection_before_deletion(self):
        a=self.task('a');b=self.task('b');before=self.tree()
        reply=self.request('cleanup.preview',**self.scope(selection=dict(task_ids=[a],paths=['bridge.json'])))
        self.assertFalse(reply['ok']);self.assertEqual(reply['problem']['code'],'CLEANUP_SELECTION_INCOMPLETE');self.assertEqual(before,self.tree())
        self.assertEqual(len(Store(self.config['state']).rows()),2)

    def test_other_tasks_original_and_backups_remain_visible_as_residuals(self):
        a=self.task('a');b=self.task('b');before=Store(self.config['state']).rows('b')
        result=self.apply(self.preview(dict(task_ids=[a])))
        self.assertTrue(result['scope_complete']);self.assertEqual(result['residual']['task_bodies'],1);self.assertGreater(result['residual']['backup_files'],0)
        self.assertEqual(before,Store(self.config['state']).rows('b'));self.assertTrue((self.root/'bridge.json').exists())

    def test_cancel_expiry_and_cross_namespace_never_apply(self):
        task=self.task();plan=self.preview(dict(task_ids=[task]))
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        self.call('cleanup.cancel',plan_id=plan['plan_id'])
        self.assertFalse(self.request('cleanup.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        self.assertEqual(Store(self.config['state']).rows()[0]['body_state'],'NORMAL')

    def test_config_change_and_queue_swap_are_not_adopted(self):
        task=self.task();plan=self.preview(dict(task_ids=[task]));raw=json.loads((self.root/'bridge.json').read_text());raw['timeout']=71;atomic_json(self.root/'bridge.json',raw)
        self.assertFalse(self.request('cleanup.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        self.assertEqual(Store(self.config['state']).rows()[0]['body_state'],'NORMAL')

    def test_historical_receipt_does_not_undo_explicit_reconnection(self):
        result=self.apply(self.preview(dict(paths=['bridge.py'])))
        child=result['stages'][0]['operation'];self.assertTrue(result['connection_removed'])
        plan=self.call('connection.reconnect.preview',**self.receipt(child));self.call('connection.apply',plan_id=plan['plan_id'],apply=True)
        queried=self.call('cleanup.operation',**self.receipt(result['operation']))
        self.assertTrue(queried['scope_complete']);self.assertFalse(queried['connection_removed'])

    def test_replaced_primary_database_is_rejected_even_with_identical_rows(self):
        task=self.task();result=self.apply(self.preview(dict(task_ids=[task])))
        copy=self.database.with_name('replacement.sqlite3');copy.write_bytes(self.database.read_bytes());os.replace(copy,self.database)
        reply=self.request('cleanup.operation',**self.receipt(result['operation']))
        self.assertFalse(reply['ok']);self.assertEqual(reply['problem']['code'],'OPERATION_IDENTITY_CHANGED')

    def test_receipt_accepts_verified_windows_short_path_alias(self):
        import ctypes
        buffer=ctypes.create_unicode_buffer(32768)
        length=ctypes.windll.kernel32.GetShortPathNameW(str(self.root),buffer,len(buffer))
        self.assertGreater(length,0)
        alias=Path(buffer.value)
        if str(alias).casefold()==str(self.root).casefold():self.skipTest('Volume has no distinct 8.3 alias')
        result=self.apply(self.preview(dict(paths=['bridge.py'])))
        queried=self.call('cleanup.operation',project=str(alias),database=str(alias/'.agent-bridge/queue.sqlite3'),operation=result['operation'])
        self.assertTrue(queried['scope_complete']);self.assertEqual(Path(queried['project']),self.root)


if __name__=='__main__':unittest.main()
