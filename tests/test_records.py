"""D4-A observations must remain bounded, body-free and non-writing."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import management, records, workspace
from agent_bridge.storage import FileLock, Store, atomic_json, load_config
import test_flow_cleanup as flow_fixtures
import flow_archive_writer

SECRET = 'SECRET_BODY_DO_NOT_RETURN'


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'input.txt').write_text(SECRET)
        atomic_json(self.root/'bridge.json', dict(project='.', include=['input.txt'], writable=['input.txt'], endpoints={'mock':dict(adapter='command',command=['never-run',SECRET])}))
        self.config = load_config(self.root/'bridge.json')

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) if p.is_file() else None for p in self.root.rglob('*')}

    def inspect(self, **kwargs):
        before = self.snapshot()
        result = records.inspect(str(self.root), **kwargs)
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result['mutation_supported']); self.assertNotIn('plan_id', result)
        return result

    def task(self, batch='job'):
        store = Store(self.config['state'])
        task = store.submit(batch, ['mock'], SECRET, self.config['endpoints'])[0]
        home = workspace.create(self.config,task)
        for name in ('answer.txt','events.jsonl','stderr.log','prompt.txt'):
            (home/name).write_text(SECRET)
        store.claim(task); store.finish(task,'DONE',dict(workspace=str(home), answer={'text':SECRET}, error=SECRET, provider_result={'result':SECRET}))
        store.record_usage(task, {'metrics':{'input_tokens':10}})
        return store, task, home

    def test_missing_database_and_configuration_create_nothing(self):
        result = self.inspect()
        self.assertEqual(result['items'],[]); self.assertTrue(result['complete'])
        self.assertFalse(self.config['state'].exists())
        result = self.inspect(config=str(self.root/'missing.json'))
        self.assertFalse(result['complete']); self.assertIn('CONFIG_UNKNOWN',result['blockers'])

    def test_api_capability_strict_args_and_no_mutations(self):
        api = management.Session(); self.addCleanup(api.close)
        def call(action, **args):
            return api.handle(dict(protocol=1,id=str(len(api.seen)+1),action=action,args=args))
        self.assertEqual(call('capabilities')['result']['records_schema'],1)
        self.assertTrue(call('records.inventory',project=str(self.root))['ok'])
        for args in ({'apply':True},{'kind':'other'},{'limit':True},{'known_configs':['relative.json']},{'cursor':'../escape'}):
            self.assertFalse(call('records.inventory',project=str(self.root),**args)['ok'])
        self.assertFalse(call('records.inspect',project=str(self.root),kind='task',record_id='../outside')['ok'])
        self.assertFalse(call('records.apply',project=str(self.root),apply=True)['ok'])

    def test_task_pagination_is_exact_and_all_reads_avoid_store(self):
        ids = [self.task(str(i))[1] for i in range(5)]
        with patch.object(Store,'__init__',side_effect=AssertionError('Unexpected mutation')):
            first = self.inspect(limit=2)
            second = self.inspect(limit=2,cursor=first['next_cursor'])
            third = self.inspect(limit=2,cursor=second['next_cursor'])
        self.assertEqual([v['id'] for page in (first,second,third) for v in page['items']],sorted(ids))
        self.assertFalse(third['more'])

    def test_task_references_include_body_controls_and_user_original(self):
        store, task, home = self.task()
        report = self.inspect(record_id=task,limit=100)
        self.assertTrue(report['complete'],report['blockers'])
        self.assertEqual(report['selection']['ownership'],'LEGACY_UNATTRIBUTED')
        self.assertIn('LEGACY_OWNERSHIP_UNCONFIRMED',report['blockers'])
        refs = report['references']
        self.assertTrue(any(r['kind']=='tasks.prompt' and r['category']=='body' for r in refs))
        self.assertTrue(any(r['kind']=='usage_snapshots' and r['category']=='control' for r in refs))
        self.assertTrue(any(r['location']==str(home/'answer.txt') and r['category']=='body' for r in refs))
        self.assertEqual(store.control()['calls_started'],1)

    def test_reference_pages_share_full_summary_and_no_duplicates(self):
        _, task, _ = self.task()
        page = self.inspect(record_id=task,limit=3); found=[]; summary=page['summary']
        while True:
            found += [r['id'] for r in page['references']]
            self.assertEqual(summary,page['summary'])
            if not page['more']: break
            page=self.inspect(record_id=task,limit=3,cursor=page['next_cursor'])
        self.assertEqual(len(found),len(set(found))); self.assertEqual(len(found),summary['references'])

    def test_frozen_summary_body_hidden_originals_deduplicated(self):
        store, task, _ = self.task()
        (self.root/'summary.md').write_text(SECRET); (self.root/'summary.json').write_text('{}')
        payload = dict(payload=dict(selected=[dict(path='input.txt')],summaries=[dict(record_path='summary.json',record=dict(body_path='summary.md',sources=[dict(path='input.txt')]),body=SECRET)]))
        with store.connect() as db: db.execute('INSERT INTO context_plans VALUES (?,?)',(task,json.dumps(payload)))
        report=self.inspect(record_id=task,limit=100)
        originals=[r for r in report['references'] if r['kind']=='original_input']
        self.assertEqual({Path(r['location']).name for r in originals},{'input.txt','summary.json','summary.md'})
        self.assertEqual(len(originals),3)

    def test_summary_escape_and_arbitrary_result_path_are_never_followed(self):
        store, task, _ = self.task()
        with store.connect() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps({'workspace':str(self.root.parent/SECRET)}),task))
            db.execute('INSERT INTO context_plans VALUES (?,?)',(task,json.dumps({'payload':{'selected':[{'path':'../'+SECRET}]}})))
        report=self.inspect(record_id=task,limit=100)
        self.assertFalse(report['complete']); self.assertIn('WORKSPACE_SCOPE_UNKNOWN',report['blockers']); self.assertIn('REFERENCE_SCOPE_UNKNOWN',report['blockers'])

    def test_registered_summary_consumer_is_an_explicit_external_reference(self):
        store, task, _=self.task(); _, consumer, _=self.task('consumer')
        saved={'payload':{'summaries':[{'record':{'provenance':{'source_task_id':task}},'body':SECRET}]}}
        with store.connect() as db: db.execute('INSERT INTO context_plans VALUES (?,?)',(consumer,json.dumps(saved)))
        report=self.inspect(record_id=task,limit=100)
        self.assertIn('RELATED_TASK_REFERENCES',report['blockers'])
        self.assertTrue(any(r.get('related_task_id')==consumer and r['category']=='external' for r in report['references']))

    def test_known_config_same_or_nested_state_is_not_exclusive_ownership(self):
        _, task, _=self.task()
        other=json.loads((self.root/'bridge.json').read_text()); other['state']='.agent-bridge/nested'
        atomic_json(self.root/'other.json',other)
        report=self.inspect(record_id=task,known_configs=[str(self.root/'other.json')])
        self.assertIn('SHARED_STATE_UNCONFIRMED',report['blockers'])

    def test_wal_or_corrupt_database_is_unknown_without_repair(self):
        store, task, _=self.task()
        wal=Path(str(store.db)+'-wal'); wal.write_bytes(b'pending')
        self.assertFalse(self.inspect(record_id=task)['complete'])
        wal.unlink(); store.db.write_bytes(b'corrupt')
        self.assertIn('DATABASE_UNREADABLE',self.inspect()['blockers'])

    def test_limits_produce_unknown_totals_not_zero(self):
        _, task, _=self.task()
        with patch.object(records,'MAX_ENTRIES',5):
            report=self.inspect(record_id=task)
        self.assertFalse(report['complete']); self.assertIsNone(report['summary']['references'])
        with patch.object(records,'MAX_DATABASE_BYTES',1):
            self.assertIn('DATABASE_SIZE_LIMIT',self.inspect()['blockers'])
        with patch.object(records,'MAX_JSON_BYTES',1):
            # Defaults are bound at definition time; exercise the explicit metadata reader.
            observer=records.Observer(self.root)
            with self.assertRaises(records.ObservationError): observer.read(self.root/'bridge.json',1)

    def test_unknown_hidden_and_recovery_data_are_classified(self):
        _, task, home=self.task()
        (home/'.hidden').write_text(SECRET)
        backup=home/'backup-1'; backup.mkdir(); (backup/'journal.json').write_text(json.dumps({'state':'APPLIED','error':SECRET}))
        report=self.inspect(record_id=task,limit=100)
        self.assertFalse(report['complete']); self.assertIn('RECOVERY_DATA_PRESERVED',report['blockers'])
        self.assertTrue(any(r['category']=='unknown' for r in report['references']))
        self.assertTrue(any(r['category']=='recovery' for r in report['references']))

    def test_actual_lock_is_observed_without_mutating_it(self):
        _, task, _=self.task()
        target=self.config['state']/'runner.lock'; target.write_bytes(b'0')
        before=self.snapshot()
        with FileLock(target):
            report=records.inspect(str(self.root),record_id=task)
        self.assertEqual(before,self.snapshot())
        self.assertIn('WRITER_ACTIVE',report['blockers'])

    def test_file_change_during_observation_invalidates_totals(self):
        _, task, home=self.task(); original=records.Observer.verify
        def changed(obs):
            (home/'answer.txt').write_text('changed fixture')
            original(obs)
        with patch.object(records.Observer,'verify',changed):
            report=records.inspect(str(self.root),record_id=task)
        self.assertFalse(report['complete']); self.assertIn('DATA_CHANGED',report['blockers'])
        self.assertIsNone(report['summary']['categories']['body']['bytes'])

    def test_manifest_ownership_never_adopts_user_config(self):
        _, task, _=self.task()
        helper=self.root/'helper.py'; helper.write_text('print("fixture")')
        atomic_json(self.root/'.bridge-integration.json',dict(schema=1,files={'helper.py':hashlib.sha256(helper.read_bytes()).hexdigest()}))
        report=self.inspect(record_id=task,limit=100)
        self.assertTrue(any(r.get('ownership')=='MANIFEST_HASH_MATCH' for r in report['references']))
        atomic_json(self.root/'.bridge-integration.json',dict(schema=1,files={'bridge.json':'0'*64}))
        self.assertIn('CONNECTION_MANIFEST_UNKNOWN',self.inspect(record_id=task)['blockers'])

    def test_hardlink_is_unknown_and_never_read_as_owned(self):
        _, task, home=self.task()
        os.link(self.root/'input.txt',home/'linked')
        report=self.inspect(record_id=task,limit=100)
        self.assertFalse(report['complete']); self.assertTrue(any(r.get('reason')=='UNSAFE_OR_UNREADABLE' for r in report['references']))


class FlowRecordTests(unittest.TestCase):
    setUp=flow_fixtures.FlowCleanupTests.setUp
    run_flow=flow_fixtures.FlowCleanupTests.run_flow
    preview=flow_fixtures.FlowCleanupTests.preview
    snapshot=flow_fixtures.FlowCleanupTests.snapshot

    def inspect(self, **kwargs):
        before=self.snapshot()
        report=records.inspect(str(self.root),kind='flow',record_id='sample',limit=100,**kwargs)
        self.assertEqual(before,self.snapshot()); self.assertNotIn('CHECK_OK',json.dumps(report)); self.assertNotIn('VALUE=2',json.dumps(report))
        return report

    def test_shared_roles_full_reference_scope_and_source_preservation(self):
        report=self.inspect()
        self.assertEqual(report['selection']['ownership'],'ROLE_LINK_VERIFIED',report['blockers'])
        self.assertEqual(len(report['selection']['roles']),2)
        self.assertTrue(report['complete'],report['blockers'])
        task=self.result['developer']['task']
        expanded=records.inspect(str(self.root),kind='task',record_id=task)
        self.assertEqual(expanded['selection']['required_scope'],'whole_flow')

    def test_private_queue_ownership_is_derived_from_role_metadata(self):
        source=self.base['state']/'queue.sqlite3'
        for role in ('developer','reviewer'):
            state=Path(self.result[role]['result']['workspace']).parents[1]
            with contextlib.closing(sqlite3.connect(source)) as a, contextlib.closing(sqlite3.connect(state/'queue.sqlite3')) as b:
                a.backup(b)
                for table in ('tasks','context_plans','role_tasks','usage_snapshots'):
                    b.execute('DELETE FROM '+table+' WHERE id!=?',(self.result[role]['task'],))
                b.commit()
            self.result[role].pop('queue_state')
        with contextlib.closing(sqlite3.connect(source)) as db:
            for role in ('developer','reviewer'):
                for table in ('tasks','context_plans','role_tasks','usage_snapshots'):
                    db.execute('DELETE FROM '+table+' WHERE id=?',(self.result[role]['task'],))
            db.commit()
        self.result.pop('queue_mode'); atomic_json(self.flow/'result.json',self.result)
        report=self.inspect()
        self.assertEqual(report['selection']['queue_mode'],'PRIVATE'); self.assertEqual(report['selection']['ownership'],'ROLE_LINK_VERIFIED',report['blockers'])

    def test_archive_journal_and_deduplicated_blobs_are_observed(self):
        flow_archive_writer.write_archive(self.root,'sample',self.preview()['plan_hash'],1,now=self.now)
        report=self.inspect()
        self.assertTrue(report['complete'],report['blockers'])
        all_refs=[]; cursor=''
        while True:
            page=records.inspect(str(self.root),kind='flow',record_id='sample',limit=100,cursor=cursor); all_refs+=page['references']
            if not page['more']: break
            cursor=page['next_cursor']
        blobs=[r for r in all_refs if r['kind']=='archive_blob']
        self.assertEqual(len(blobs),len(list((self.flow/'archive/blobs').iterdir())))
        self.assertTrue(any(r['link_count']>1 for r in blobs))
        self.assertTrue(any(r['location']==str(self.flow/'cleanup.json') and r['category']=='body' for r in all_refs))

    def test_corrupt_archive_is_unknown_not_successful_inventory(self):
        flow_archive_writer.write_archive(self.root,'sample',self.preview()['plan_hash'],1,now=self.now)
        next((self.flow/'archive/blobs').iterdir()).write_bytes(b'changed')
        report=self.inspect(); self.assertFalse(report['complete']); self.assertIn('ARCHIVE_UNKNOWN',report['blockers'])

    def test_p1_receipt_pending_and_payloads_are_kept_separate(self):
        jobs=self.base['state']/'p1-jobs'; jobs.mkdir()
        atomic_json(jobs/'sample.json',dict(flow='sample',config=str(self.root/'bridge.json'),state='RUNNING',expected={'prompt':SECRET}))
        (jobs/'sample.txt').write_text(SECRET); (jobs/'sample.log').write_text(SECRET)
        report=self.inspect(); self.assertNotIn(SECRET,json.dumps(report)); self.assertIn('ROLE_JOB_UNCONFIRMED',report['blockers'])
        self.assertTrue(any(r['kind']=='role_job_identity' and r['category']=='control' for r in report['references']))

    def test_unattributed_staging_is_reported_without_silent_omission(self):
        staging=self.base['state']/'archive-staging-orphan'; staging.mkdir()
        (staging/'fragment').write_text(SECRET)
        report=self.inspect(); self.assertFalse(report['complete']); self.assertIn('STAGING_SCOPE_UNKNOWN',report['blockers'])

    def test_mismatched_frozen_role_binding_never_claims_verified_ownership(self):
        role_config=self.flow/'developer-config.json'
        raw=json.loads(role_config.read_text()); raw['endpoints']['developer']['command']=['changed',SECRET]
        atomic_json(role_config,raw)
        report=self.inspect(); self.assertFalse(report['complete']); self.assertEqual(report['selection']['ownership'],'UNCONFIRMED')

    def test_private_and_parent_identity_collision_is_not_verified(self):
        self.result.pop('queue_mode'); atomic_json(self.flow/'result.json',self.result)
        report=self.inspect(); self.assertFalse(report['complete']); self.assertEqual(report['selection']['ownership'],'UNCONFIRMED')


if __name__=='__main__':
    unittest.main()
