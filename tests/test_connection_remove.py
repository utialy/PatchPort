"""Connection removal preserves control ledgers and supports config-free recovery."""
import json
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import connect, connection_remove as removal, connection_guard, interactive_guard, management, records_guard
from agent_bridge.storage import Store, atomic_json, load_config


@unittest.skipUnless(os.name=='nt','Pinned file removal is Windows-only')
class ConnectionRemoveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); (self.root/'input.txt').write_text('ORIGINAL')
        atomic_json(self.root/'bridge.json',dict(project='.',include=['input.txt'],writable=['input.txt'],endpoints={'mock':{'adapter':'command','command':[sys.executable,'-c','pass']}}))
        self.original=(self.root/'bridge.json').read_bytes(); self.config=load_config(self.root/'bridge.json')
        self.api=management.Session(); self.addCleanup(self.api.close); self.seq=0
        plan=connect.plan_install(self.root,sys.executable); connect.install(plan,True)
        prepared=self.call('records.prepare.preview',**self.scope())
        self.call('records.apply',plan_id=prepared['plan_id'],apply=True)

    def scope(self,**extra): return dict(project=str(self.root),config=str(self.root/'bridge.json'),**extra)
    def request(self,action,**args):
        self.seq+=1
        return self.api.handle(dict(protocol=1,id=str(self.seq),action=action,args=args))
    def call(self,action,**args):
        result=self.request(action,**args); self.assertTrue(result['ok'],result); return result['result']
    def preview(self,paths=None,**extra):
        return self.call('connection.remove.preview',**self.scope(paths=paths or ['bridge.py','bridge.json'],**extra))
    def apply(self,plan):
        self.assertTrue(plan['plan_id'],plan)
        return self.call('connection.apply',plan_id=plan['plan_id'],apply=True)
    def receipt(self,op): return dict(project=str(self.root),database=str(self.config['state']/'queue.sqlite3'),operation=op)
    def tree(self): return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_inventory_is_readonly_and_separates_user_files(self):
        before=self.tree(); listing=self.call('connection.inventory',**self.scope())
        self.assertEqual(before,self.tree())
        entries={i['path']:i for i in listing['items']}
        self.assertEqual(entries['bridge.py']['kind'],'managed'); self.assertEqual(entries['bridge.json']['kind'],'user_config')
        self.assertNotIn('input.txt',entries)
        self.assertFalse(self.preview(['input.txt'])['plan_id'])

    def test_exact_removal_preserves_original_backup_ids_and_control(self):
        before=Store(self.config['state']).control(); backups=self.tree()
        plan=self.preview(); result=self.apply(plan)
        self.assertTrue(result['complete'],result); self.assertFalse((self.root/'bridge.json').exists())
        self.assertFalse((self.root/'bridge.py').exists()); self.assertTrue((self.root/'role_flow.py').exists())
        self.assertEqual((self.root/'input.txt').read_text(),'ORIGINAL')
        for name,data in backups.items():
            if name.startswith('.bridge-integration-backups/'): self.assertEqual((self.root/name).read_bytes(),data)
        self.assertEqual(Store(self.config['state']).control(),before)
        manifest=json.loads((self.root/connect.MANIFEST).read_text()); self.assertNotIn('bridge.py',manifest['files'])
        self.assertEqual(self.call('connection.operation',**self.receipt(result['operation']))['state'],'DONE')
        self.assertFalse(self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['ok'])

    def test_modified_and_preexisting_assets_are_never_adopted(self):
        (self.root/'bridge.py').write_text('USER')
        self.assertFalse(self.preview(['bridge.py'])['plan_id'])
        with self.assertRaises(ValueError):
            removal.file_info(self.root,'../input.txt')
        manifest=json.loads((self.root/connect.MANIFEST).read_text()); manifest['files']['input.txt']=connect.digest(b'ORIGINAL')
        atomic_json(self.root/connect.MANIFEST,manifest)
        self.assertFalse(self.preview(['input.txt'])['plan_id'])

    def test_pending_body_service_and_changed_plan_are_blocked(self):
        plan=self.preview(); (self.root/'bridge.py').write_text('changed')
        self.assertFalse(self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        atomic_json(self.config['state']/'service.json',{'state':'unknown'})
        self.assertIn('SERVICE_REGISTRATION_PRESENT',self.preview(['bridge.json'])['report']['blockers'])
        (self.config['state']/'service.json').unlink()
        Store(self.config['state'],config=self.config).submit('pending',['mock'],'BODY',self.config['endpoints'])
        self.assertIn('BODY_CLEANUP_REQUIRED',self.preview(['bridge.json'])['report']['blockers'])

    def test_old_sql_and_cached_config_cannot_write_after_remove(self):
        old=sqlite3.connect(self.config['state']/'queue.sqlite3'); self.addCleanup(old.close)
        old.create_function('records_actor',0,lambda:'@runtime')
        self.apply(self.preview(['bridge.py']))
        with self.assertRaises(sqlite3.Error): old.execute('UPDATE control SET paused=1')
        with self.assertRaisesRegex(ValueError,'CONNECTION_REMOVED'): Store(self.config['state'],config=self.config).submit('new',['mock'],'x',self.config['endpoints'])
        with self.assertRaisesRegex(ValueError,'CONNECTION_REMOVED'): connect.plan_install(self.root)
        with self.assertRaisesRegex(ValueError,'CONNECTION_REMOVED'): interactive_guard.WriterLease(self.config['state']/'queue.sqlite3',self.root,'new-owner').open()

    def test_locked_file_reports_partial_and_resumes_only_io(self):
        plan=self.preview(); (self.root/'bridge.py').chmod(0o444)
        reply=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)
        self.assertFalse(reply['ok']); partial=reply['result']; self.assertEqual(partial['state'],'PARTIAL')
        self.assertTrue((self.root/'bridge.json').exists())
        (self.root/'bridge.py').chmod(0o666)
        resumed=self.call('connection.resume.preview',**self.receipt(partial['operation']))
        self.assertTrue(self.apply(resumed)['complete'])

    def test_manifest_failure_after_config_removal_resumes_without_config(self):
        plan=self.preview(); original=removal.atomic_json
        def fail_manifest(path,value):
            if Path(path).name==connect.MANIFEST: raise OSError('injected')
            return original(path,value)
        with patch.object(removal,'atomic_json',side_effect=fail_manifest):
            reply=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)
        self.assertFalse(reply['ok']); self.assertFalse((self.root/'bridge.json').exists())
        op=reply['result']['operation']
        self.api.close(); self.api=management.Session()
        self.assertTrue(self.apply(self.call('connection.resume.preview',**self.receipt(op)))['complete'])

    def test_reconnect_is_explicit_and_restores_no_deleted_files(self):
        done=self.apply(self.preview()); controls=Store(self.config['state']).control()
        (self.root/'bridge.json').write_bytes(self.original)
        with self.assertRaisesRegex(ValueError,'CONNECTION_REMOVED'): connect.plan_install(self.root)
        plan=self.call('connection.reconnect.preview',**self.receipt(done['operation']))
        result=self.apply(plan); self.assertTrue(result['reconnected'],result)
        self.assertFalse((self.root/'bridge.py').exists()); self.assertEqual(Store(self.config['state']).control(),controls)
        self.assertTrue(connect.plan_install(self.root)['ok'])
        Store(self.config['state'],config=self.config).submit('explicit-new',['mock'],'new',self.config['endpoints'])

    def test_shared_config_and_recreated_file_block_without_deleting(self):
        atomic_json(self.root/'other.json',json.loads(self.original))
        self.assertIn('CONNECTION_SHARED_REFERENCE',self.preview(known_configs=[str(self.root/'other.json')])['report']['blockers'])
        plan=self.preview(); before=self.tree()
        with patch.object(connection_guard,'freeze',side_effect=OSError('db failure')):
            self.assertFalse(self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['ok'])
        for name,data in before.items(): self.assertEqual((self.root/name).read_bytes(),data)

    def history(self):
        path=self.root/'ledger/interactive.sqlite3'; args=dict(database=str(path),project=str(self.root))
        self.call('interactive.owner.open',**args,owner='owner',apply=True)
        self.call('interactive.begin',**args,owner='owner',session_id='session',node='node',provider='codex',name='NAME',apply=True)
        self.call('interactive.record',database=str(path),owner='owner',session_id='session',event_id='stop',kind='session_stopped',data={'confirmed_exit':True},apply=True)
        self.call('interactive.end',database=str(path),owner='owner',apply=True)
        plan=self.call('history.purge.preview',**args,session_ids=['session'])
        self.call('history.apply',plan_id=plan['plan_id'],apply=True)
        return path,args

    def test_external_redacted_ledger_is_fenced_and_remains_readable(self):
        path,args=self.history(); before=self.call('history.list',**args)
        old=sqlite3.connect(path); self.addCleanup(old.close)
        old.create_function('bridge_owner',0,lambda:'@admin')
        result=self.apply(self.preview(databases=[str(path)])); self.assertTrue(result['complete'])
        self.assertEqual(self.call('history.list',**args)['items'],before['items'])
        with self.assertRaises(sqlite3.Error): old.execute("UPDATE interactive_owners SET state='ACTIVE'")
        self.assertFalse(self.request('interactive.owner.open',**args,owner='new-owner',apply=True)['ok'])

    def test_external_freeze_failure_starts_no_file_deletion(self):
        path,args=self.history(); real=connection_guard.freeze
        def injected(db,project,operation,primary=False):
            if not primary: raise OSError('external failure')
            return real(db,project,operation,primary)
        plan=self.preview(databases=[str(path)])
        with patch.object(connection_guard,'freeze',side_effect=injected):
            partial=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['result']
        self.assertEqual(partial['state'],'PARTIAL'); self.assertTrue((self.root/'bridge.py').exists()); self.assertTrue((self.root/'bridge.json').exists())
        self.assertTrue(self.apply(self.call('connection.resume.preview',**self.receipt(partial['operation'])))['complete'])

    def test_external_batch_queue_and_shared_history_are_preserved(self):
        other=self.root/'other'; other.mkdir(); Store(other/'state')
        plan=self.preview(databases=[str(other/'state/queue.sqlite3')])
        self.assertIn('CONNECTION_SHARED_LEDGER',plan['report']['blockers'])
        path,args=self.history()
        owner=interactive_guard.WriterLease(path,other,'other-owner'); owner.open(); owner.close()
        before=path.read_bytes(); plan=self.preview(databases=[str(path)])
        self.assertIn('CONNECTION_SHARED_LEDGER',plan['report']['blockers']); self.assertEqual(path.read_bytes(),before)

    def test_marker_failure_is_discoverable_and_does_not_allow_reinstall(self):
        plan=self.preview(); real=removal.atomic_json
        def failed(path,data):
            if Path(path).name==connection_guard.MARKER: raise OSError('marker failure')
            return real(path,data)
        with patch.object(removal,'atomic_json',side_effect=failed):
            result=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['result']
        self.assertEqual(result['deleted'],0)
        discovered=self.call('connection.inventory',**self.scope())
        self.assertEqual(discovered['receipt']['operation'],result['operation'])
        with self.assertRaisesRegex(ValueError,'CONNECTION_REMOVED'): connect.plan_install(self.root)
        self.assertTrue(self.apply(self.call('connection.resume.preview',**self.receipt(result['operation'])))['complete'])

    def test_completion_receipt_failure_is_partial_and_config_free(self):
        plan=self.preview(); real=removal.persist; failed=False
        def injected(data):
            nonlocal failed
            if data['phase']=='DONE' and not failed:
                failed=True; raise sqlite3.OperationalError('commit failure')
            return real(data)
        with patch.object(removal,'persist',side_effect=injected):
            result=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['result']
        self.assertFalse(result['complete']); self.assertFalse((self.root/'bridge.json').exists())
        self.assertTrue(self.apply(self.call('connection.resume.preview',**self.receipt(result['operation'])))['complete'])

    def test_recreated_deleted_file_never_becomes_a_new_removal_target(self):
        plan=self.preview(); real=removal.persist; failed=False
        def injected(data):
            nonlocal failed
            if any(i['status']=='DELETED' for i in data['items']) and not failed:
                failed=True; raise OSError('after one deletion')
            return real(data)
        with patch.object(removal,'persist',side_effect=injected):
            result=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['result']
        (self.root/'bridge.py').write_text('NEW USER FILE')
        reply=self.request('connection.resume.preview',**self.receipt(result['operation']))
        self.assertFalse(reply['ok']); self.assertEqual((self.root/'bridge.py').read_text(),'NEW USER FILE')

    def test_wal_preview_is_rejected_without_creating_sidecars(self):
        (self.config['state']/'queue.sqlite3-wal').write_bytes(b'pending')
        before=self.tree(); self.assertFalse(self.request('connection.inventory',**self.scope())['ok'])
        self.assertEqual(before,self.tree())

    def test_unknown_external_database_is_not_claimed_by_filename(self):
        path=self.root/'unknown/interactive.sqlite3'; path.parent.mkdir()
        db=sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE private_data(value TEXT)'); db.commit()
        finally: db.close()
        before=path.read_bytes(); plan=self.preview(databases=[str(path)])
        self.assertIsNone(plan['plan_id']); self.assertIn('HISTORY_PREPARATION_REQUIRED',plan['report']['blockers']); self.assertEqual(path.read_bytes(),before)

    def test_connection_trigger_without_metadata_is_not_ignored(self):
        with records_guard.connect(self.config['state'],actor='@admin') as db:
            db.execute('CREATE TRIGGER connection_guard_unexpected BEFORE UPDATE ON control BEGIN SELECT 1; END')
        reply=self.request('connection.inventory',**self.scope())
        self.assertFalse(reply['ok']); self.assertEqual(reply['problem']['code'],'CONNECTION_GUARD_DAMAGED')

    def test_connection_receipt_does_not_enter_body_resume_namespace(self):
        plan=self.preview(['bridge.py']); (self.root/'bridge.py').chmod(0o444)
        result=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['result']
        (self.root/'bridge.py').chmod(0o666)
        self.assertEqual(self.call('records.inventory',**self.scope())['pending_body_operations'],[])
        self.assertEqual(self.call('records.operation',**self.receipt(result['operation']))['kind'],'connection_remove')
        reply=self.request('records.resume.preview',**self.scope(operation=result['operation']))
        self.assertEqual(reply['problem']['code'],'OPERATION_NAMESPACE_MISMATCH')

    def test_partial_result_query_is_successful_even_when_removal_is_incomplete(self):
        plan=self.preview(['bridge.py']); (self.root/'bridge.py').chmod(0o444)
        result=self.request('connection.apply',plan_id=plan['plan_id'],apply=True)['result']
        (self.root/'bridge.py').chmod(0o666)
        reply=self.call('connection.operation',**self.receipt(result['operation']))
        self.assertTrue(reply['found']); self.assertFalse(reply['complete']); self.assertEqual(reply['state'],'PARTIAL')

    def test_consumed_calls_limits_and_deleted_task_ids_survive_removal(self):
        from agent_bridge.runner import run
        store=Store(self.config['state'],config=self.config)
        task=store.submit('completed',['mock'],'request',self.config['endpoints'])[0]
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(run(self.config,True),0)
        purge=self.call('records.purge.preview',**self.scope(task_ids=[task]))
        self.call('records.apply',plan_id=purge['plan_id'],apply=True)
        store.pause(True); store.set_limit(7); before=store.control(); self.assertEqual(before['calls_started'],1)
        rows=store.rows()
        result=self.apply(self.preview()); self.assertTrue(result['complete'])
        self.assertEqual(store.control(),before); self.assertEqual(store.rows(),rows)


if __name__=='__main__': unittest.main()
