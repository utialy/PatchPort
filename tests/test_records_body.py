"""Protected ordinary queues preserve control data through body purge and recovery."""
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

from agent_bridge import management, records_body as body, records_guard as guard, workspace, cleanup, runner_manager
from agent_bridge.runner import run
from agent_bridge.storage import Store, FileLock, atomic_json, load_config

SECRET='PRIVATE_TASK_BODY'


@unittest.skipUnless(os.name=='nt','Pinned file deletion is Windows-only')
class RecordsBodyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); (self.root/'input.txt').write_text(SECRET)
        atomic_json(self.root/'bridge.json',dict(project='.',include=['input.txt'],writable=['input.txt'],endpoints={'mock':dict(adapter='command',command=[sys.executable,'-c',"print('PRIVATE_TASK_BODY')"])}))
        self.config=load_config(self.root/'bridge.json')
        self.api=management.Session(); self.addCleanup(self.api.close); self.sequence=0

    def request(self,action,**args):
        self.sequence+=1
        return self.api.handle(dict(protocol=1,id=str(self.sequence),action=action,args=args))

    def call(self,action,**args):
        reply=self.request(action,**args); self.assertTrue(reply['ok'],reply); return reply['result']

    def scope(self,**extra):
        return dict(project=str(self.root),config=str(self.root/'bridge.json'),**extra)

    def prepare(self):
        plan=self.call('records.prepare.preview',**self.scope())
        self.assertTrue(plan['plan_id'],plan)
        return self.call('records.apply',plan_id=plan['plan_id'],apply=True)

    def ready(self,names=('a',)):
        self.prepare(); store=Store(self.config['state'],config=self.config)
        ids=[store.submit(name,['mock'],SECRET,self.config['endpoints'])[0] for name in names]
        output=io.StringIO()
        with contextlib.redirect_stdout(output): self.assertEqual(run(self.config,True),0)
        self.assertNotIn(SECRET,output.getvalue())
        return store,ids

    def preview(self,ids):
        return self.call('records.purge.preview',**self.scope(task_ids=ids))

    def receipt(self,operation):
        return self.call('records.operation',project=str(self.root),database=str(self.config['state']/'queue.sqlite3'),operation=operation)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():p.read_bytes() if p.is_file() else None for p in self.root.rglob('*')}

    def test_prepare_preview_and_inventory_never_create_state(self):
        before=self.snapshot()
        plan=self.call('records.prepare.preview',**self.scope())
        self.assertEqual(before,self.snapshot()); self.assertFalse(self.config['state'].exists())
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=False)['ok'])

    def test_legacy_rows_preserved_and_not_adopted_by_preparation(self):
        store=Store(self.config['state']); task=store.submit('legacy',['mock'],SECRET,self.config['endpoints'])[0]
        store.finish(task,'ERROR',dict(answer={'text':SECRET})); before=store.rows()
        self.prepare(); after=Store(self.config['state']).rows()
        self.assertEqual([{k:v for k,v in row.items() if not k.startswith('body_')} for row in after],before)
        self.assertIn('LEGACY_OWNERSHIP_UNCONFIRMED',self.preview([task])['report']['blockers'])

    def test_existing_old_sql_connection_cannot_write_after_prepare(self):
        store=Store(self.config['state']); old=sqlite3.connect(store.db); self.addCleanup(old.close)
        self.prepare()
        with self.assertRaises(sqlite3.Error): old.execute('UPDATE control SET calls_started=0')
        old.rollback()
        with self.assertRaises(sqlite3.Error): old.execute("INSERT INTO tasks VALUES ('old','old','mock','BODY','QUEUED',1,NULL,NULL,NULL)")

    def test_prepare_rejects_active_legacy_role_and_service_state(self):
        store=Store(self.config['state']); store.submit('pending',['mock'],SECRET,self.config['endpoints'])
        self.assertIn('TASK_NOT_TERMINAL',self.call('records.prepare.preview',**self.scope())['report']['blockers'])
        atomic_json(self.config['state']/'service.json',{'state':'UNKNOWN'})
        (self.root/'.role-flows').mkdir(); (self.root/'.role-flows/unknown').mkdir()
        report=self.call('records.prepare.preview',**self.scope())['report']
        self.assertIn('ROLE_QUEUE_UPGRADE_REQUIRED',report['blockers']); self.assertIn('SERVICE_REGISTRATION_PRESENT',report['blockers'])

    def test_atomic_body_scrub_preserves_usage_control_ids_other_task_and_original(self):
        store,ids=self.ready(('a','b')); original=(self.root/'input.txt').read_bytes(); controls=store.control(); b=store.rows('b')
        with store.connect() as db: usage=[tuple(row) for row in db.execute('SELECT * FROM usage_snapshots ORDER BY id')]
        plan=self.preview([ids[0]]); before=self.snapshot(); self.assertEqual(before,self.snapshot())
        result=self.call('records.apply',plan_id=plan['plan_id'],apply=True)
        self.assertTrue(result['body_deleted']); self.assertEqual(result['items'][0]['state'],'DELETED')
        self.assertEqual(controls,store.control()); self.assertEqual(b,store.rows('b'))
        with store.connect() as db: self.assertEqual(usage,[tuple(row) for row in db.execute('SELECT * FROM usage_snapshots ORDER BY id')])
        self.assertEqual(original,(self.root/'input.txt').read_bytes()); self.assertNotIn(SECRET,json.dumps(store.rows('a')))
        self.assertEqual(self.receipt(result['operation'])['state'],'DONE')
        self.assertFalse((self.config['state']/'workspaces'/ids[0]).exists())
        self.assertEqual(self.call('result',config=str(self.root/'bridge.json'),task_id=ids[0])['answer']['status'],'BODY_DELETED')

    def test_guard_rejects_same_id_body_rewrite_and_answer_recapture(self):
        store,ids=self.ready(); plan=self.preview(ids); self.call('records.apply',plan_id=plan['plan_id'],apply=True)
        with self.assertRaises((ValueError,sqlite3.Error)): store.submit('a',['mock'],SECRET,self.config['endpoints'])
        with self.assertRaises(sqlite3.Error):
            with store.connect() as db: db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps({'answer':SECRET}),ids[0]))
        with self.assertRaisesRegex(ValueError,'BODY_DELETED'): workspace.create(self.config,ids[0])
        with self.assertRaisesRegex(ValueError,'BODY_DELETED'): workspace.review(self.config,ids[0])
        self.assertEqual(cleanup.candidate(self.config,store.rows()[0],time.time())['reason'],'BODY_DELETED')

    def test_runner_lease_blocks_purge_until_exit_and_releases_cleanly(self):
        store,ids=self.ready()
        writer=guard.Writer(self.config).open()
        try: self.assertIn('RECORDS_WRITER_ACTIVE',self.preview(ids)['report']['blockers'])
        finally: writer.close(True)
        self.assertTrue(self.preview(ids)['report']['eligible'])

    def test_lost_lease_remains_unconfirmed_even_after_lock_release(self):
        store,ids=self.ready(); writer=guard.Writer(self.config).open(); writer.release()
        self.assertIn('RECORDS_WRITER_ACTIVE',self.preview(ids)['report']['blockers'])

    def test_prepared_queue_requires_project_scope_and_runner_for_claim(self):
        self.prepare(); unbound=Store(self.config['state'])
        with self.assertRaisesRegex(ValueError,'RECORDS_PROJECT_REQUIRED'): unbound.submit('x',['mock'],SECRET,self.config['endpoints'])
        store=Store(self.config['state'],config=self.config); task=store.submit('a',['mock'],SECRET,self.config['endpoints'])[0]
        with self.assertRaises(sqlite3.Error): store.claim(task)
        self.assertEqual(store.control()['calls_started'],0)

    def test_prepared_role_launch_is_rejected_before_flow_files(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError,'ROLE_QUEUE_UPGRADE_REQUIRED'): guard.reject_roles(self.config)
        self.assertFalse((self.root/'.role-flows').exists())

    def test_backup_and_new_excluded_artifact_block_before_mutation(self):
        store,ids=self.ready(); home=self.config['state']/'workspaces'/ids[0]
        (home/'backup-protected').mkdir(); (home/'backup-protected/original').write_text(SECRET)
        reply=self.request('records.purge.preview',**self.scope(task_ids=ids))
        self.assertFalse(reply['ok']); self.assertEqual(reply['problem']['code'],'RECOVERY_OR_EXCLUDED_DATA')
        self.assertIn(SECRET,json.dumps(store.rows()))

    def test_changed_file_same_size_and_mtime_rejects_original_plan(self):
        store,ids=self.ready(); plan=self.preview(ids); path=self.config['state']/'workspaces'/ids[0]/'answer.txt'; stamp=path.stat()
        path.write_bytes(b'X'*stamp.st_size); os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['problem']['code'],'PLAN_CHANGED')
        self.assertEqual(store.rows()[0]['body_state'],'NORMAL')

    def test_sql_failure_rolls_back_scrub_and_receipt_before_any_file_move(self):
        store,ids=self.ready(); plan=self.preview(ids); before=store.rows(); real=guard.connect
        @contextlib.contextmanager
        def fail(*args,**kwargs):
            with real(*args,**kwargs) as db:
                class Proxy:
                    def execute(self,sql,*params):
                        if sql.startswith('INSERT INTO records_operations'): raise sqlite3.OperationalError('fixture failure')
                        return db.execute(sql,*params)
                yield Proxy()
        with patch.object(guard,'connect',fail): self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        self.assertEqual(before,store.rows()); self.assertFalse(self.receipt(plan['plan_id'])['found'])
        self.assertTrue((self.config['state']/'workspaces'/ids[0]).exists())

    def partial(self):
        store,ids=self.ready(); plan=self.preview(ids)
        with patch.object(body,'guarded_removal',side_effect=OSError('fixture locked file')):
            response=self.request('records.apply',plan_id=plan['plan_id'],apply=True)
        self.assertEqual(response['problem']['code'],'OPERATION_INCOMPLETE')
        result=response['result']; self.assertEqual(result['state'],'PARTIAL')
        self.assertNotIn(SECRET,json.dumps(result)); self.assertEqual(store.rows()[0]['body_state'],'PURGING')
        self.assertFalse((self.config['state']/'workspaces'/ids[0]).exists())
        return store,ids,result

    def test_partial_file_failure_is_explicitly_resumed_without_rerun(self):
        store,ids,result=self.partial(); controls=store.control()
        self.assertFalse(self.receipt(result['operation'])['complete'])
        with self.assertRaises(sqlite3.Error): store.submit('b',['mock'],SECRET,self.config['endpoints'])
        plan=self.call('records.resume.preview',**self.scope(operation=result['operation']))
        completed=self.call('records.apply',plan_id=plan['plan_id'],apply=True)
        self.assertEqual(completed['operation'],result['operation']); self.assertTrue(completed['body_deleted']); self.assertEqual(controls,store.control())

    def test_resume_refuses_new_file_or_original_path_recreation(self):
        store,ids,result=self.partial()
        quarantine=self.config['state']/'record-bodies'/result['operation']/ids[0]
        (quarantine/'new-file').write_text('new data')
        self.assertEqual(self.request('records.resume.preview',**self.scope(operation=result['operation']))['problem']['code'],'PLAN_CHANGED')
        self.assertTrue((quarantine/'new-file').exists())

    def test_operation_identity_refuses_replaced_database(self):
        store,ids,result=self.partial(); copy=store.db.with_suffix('.replacement'); copy.write_bytes(store.db.read_bytes()); copy.replace(store.db)
        reply=self.request('records.operation',project=str(self.root),database=str(store.db),operation=result['operation'])
        self.assertEqual(reply['problem']['code'],'OPERATION_IDENTITY_CHANGED')

    def test_finalization_failure_does_not_claim_deleted_and_resume_repairs_metadata(self):
        store,ids=self.ready(); plan=self.preview(ids); real=guard.connect
        @contextlib.contextmanager
        def fail(*args,**kwargs):
            with real(*args,**kwargs) as db:
                class Proxy:
                    def execute(self,sql,*params):
                        if sql.startswith('UPDATE records_operations SET data='): raise sqlite3.OperationalError('finalization fixture')
                        return db.execute(sql,*params)
                yield Proxy()
        with patch.object(guard,'connect',fail):
            failed=self.request('records.apply',plan_id=plan['plan_id'],apply=True)
        self.assertEqual(failed['result']['state'],'PARTIAL')
        self.assertNotEqual(failed['result']['items'][0]['state'],'DELETED')
        self.assertEqual(store.rows()[0]['body_state'],'PURGING')
        resumed=self.call('records.resume.preview',**self.scope(operation=plan['plan_id']))
        self.assertTrue(self.call('records.apply',plan_id=resumed['plan_id'],apply=True)['body_deleted'])
        self.assertEqual(store.rows()[0]['body_state'],'DELETED')

    def test_pending_purge_blocks_background_start_before_launch_receipt(self):
        _,_,_=self.partial()
        with self.assertRaisesRegex(ValueError,'BODY_PURGE_PENDING'):
            runner_manager.start(runner_manager.checked_config(self.root/'bridge.json'))
        self.assertFalse((self.config['state']/'runner-launch.json').exists())

    def test_real_background_runner_closes_lease_and_never_logs_bodies(self):
        self.prepare(); config=runner_manager.checked_config(self.root/'bridge.json'); store=Store(config['state'],config=config)
        task=store.submit('background',['mock'],SECRET,config['endpoints'])[0]
        launched,code=runner_manager.start(config); self.assertEqual(code,0,launched)
        try:
            deadline=time.monotonic()+10
            while store.rows()[0]['state']!='DONE' and time.monotonic()<deadline: time.sleep(.05)
            self.assertEqual(store.rows()[0]['state'],'DONE')
            self.assertFalse(self.preview([task])['report']['eligible'])
        finally:
            runner_manager.stop(config,launched['run_id'],cancel=True,timeout=10)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            try:
                if self.preview([task])['report']['eligible']: break
            except AssertionError: pass
            time.sleep(.05)
        self.assertTrue(self.preview([task])['report']['eligible'])
        time.sleep(.1)
        for log in (config['state']/'runner-logs').glob('*.log'):
            self.assertNotIn(SECRET,log.read_text(encoding='utf-8'))

    def test_one_use_cancel_expiry_other_namespace_and_receipt(self):
        store,ids=self.ready(); plan=self.preview(ids)
        self.assertFalse(self.request('history.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        self.call('records.cancel',plan_id=plan['plan_id']); self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        plan=self.preview(ids); _,payload=self.api.plans[plan['plan_id']]; self.api.plans[plan['plan_id']]=(time.monotonic()-1,payload)
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        plan=self.preview(ids); result=self.call('records.apply',plan_id=plan['plan_id'],apply=True)
        self.assertFalse(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['ok']); self.assertTrue(self.receipt(result['operation'])['found'])

    def test_unselected_summary_consumer_must_be_explicitly_selected(self):
        store,ids=self.ready(('a','b'))
        with guard.connect(self.config['state'],actor='@admin') as db:
            db.execute('INSERT INTO context_plans VALUES (?,?)',(ids[1],json.dumps({'payload':{'summaries':[{'record':{'provenance':{'source_task_id':ids[0]}},'body':SECRET}]}})))
        half=self.preview([ids[0]])['report']; self.assertEqual(half['required_ids'],[ids[1]]); self.assertFalse(half['eligible'])
        self.assertTrue(self.preview(ids)['report']['eligible'])

    def test_context_tombstone_is_finalized_and_pending_receipt_is_discoverable(self):
        store,ids,result=self.partial()
        listed=self.call('records.inventory',project=str(self.root))
        self.assertEqual(listed['pending_body_operations'][0]['operation'],result['operation'])
        with guard.connect(self.config['state'],actor='@admin') as db:
            db.execute('INSERT INTO context_plans VALUES (?,?)',(ids[0],json.dumps({'body_state':'PURGING','operation':result['operation']})))
        plan=self.call('records.resume.preview',**self.scope(operation=result['operation']))
        self.call('records.apply',plan_id=plan['plan_id'],apply=True)
        self.assertEqual(store.context_plan(ids[0])['body_state'],'DELETED')
        self.assertEqual(self.call('records.inventory',project=str(self.root))['pending_body_operations'],[])

    def test_missing_or_extra_guard_rejects_new_writes(self):
        store,ids=self.ready()
        with contextlib.closing(sqlite3.connect(store.db)) as db: db.execute('DROP TRIGGER records_guard_tasks_update')
        with self.assertRaisesRegex(ValueError,'RECORDS_GUARD_DAMAGED'): Store(self.config['state'],config=self.config)

    def test_new_owner_after_preview_invalidates_apply(self):
        store,ids=self.ready(); plan=self.preview(ids); writer=guard.Writer(self.config).open()
        try: self.assertEqual(self.request('records.apply',plan_id=plan['plan_id'],apply=True)['problem']['code'],'PLAN_CHANGED')
        finally: writer.close(True)
        self.assertEqual(store.rows()[0]['body_state'],'NORMAL')

    def test_prepared_legacy_copy_mutation_requires_ownership(self):
        store=Store(self.config['state']); task=store.submit('old',['mock'],SECRET,self.config['endpoints'])[0]
        home=workspace.create(self.config,task); (home/'project/input.txt').write_text('legacy proposal')
        changes=workspace.changes(self.config,home)
        store.finish(task,'DONE',dict(workspace=str(home),changes=changes))
        self.prepare()
        self.assertEqual(len(workspace.review(self.config,task)),1)
        preview=self.request('proposal.preview',config=str(self.root/'bridge.json'),task_id=task,paths=['input.txt'])
        self.assertEqual(preview['problem']['code'],'LEGACY_OWNERSHIP_UNCONFIRMED')
        with self.assertRaisesRegex(ValueError,'LEGACY_OWNERSHIP_UNCONFIRMED'): workspace.review(self.config,task,['input.txt'])
        self.assertEqual(cleanup.candidate(self.config,store.rows()[0],time.time())['reason'],'LEGACY_OWNERSHIP_UNCONFIRMED')

    def test_internal_state_input_is_rejected_without_preparation(self):
        raw=json.loads((self.root/'bridge.json').read_text()); raw['state']='runtime'; raw['include']=['runtime/workspaces/copied.txt']
        atomic_json(self.root/'bridge.json',raw)
        reply=self.request('records.prepare.preview',**self.scope())
        self.assertEqual(reply['problem']['code'],'RECORDS_INPUT_SCOPE_UNKNOWN')
        self.assertFalse((self.root/'runtime').exists())

    def test_unconfirmed_writer_blocks_artifact_changes_and_prune(self):
        store,ids=self.ready(); writer=guard.Writer(self.config).open(); writer.release()
        with self.assertRaisesRegex(ValueError,'RECORDS_WRITER_UNCONFIRMED'): workspace.review(self.config,ids[0],['input.txt'])
        self.assertEqual(cleanup.candidate(self.config,store.rows()[0],time.time())['reason'],'RECORDS_WRITER_UNCONFIRMED')

    def test_protected_execution_rejects_sensitive_input_before_copy(self):
        self.prepare(); (self.root/'.env').write_text(SECRET)
        config=dict(self.config,include=['.env']); store=Store(config['state'],config=config)
        task=store.submit('secret',['mock'],SECRET,config['endpoints'])[0]
        with contextlib.redirect_stdout(io.StringIO()): run(config,True)
        self.assertEqual(store.rows()[0]['state'],'ERROR')
        self.assertFalse((config['state']/'workspaces'/task).exists())

    def test_parallel_prepared_tasks_do_not_treat_live_usage_journals_as_damage(self):
        store,ids=self.ready(('a','b','c','d'))
        self.assertEqual([row['state'] for row in store.rows()],['DONE']*4)
        self.assertEqual(store.control()['calls_started'],4)
        self.assertTrue(self.preview(ids)['report']['eligible'])


if __name__=='__main__':
    unittest.main()
