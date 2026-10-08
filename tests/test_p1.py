"""P1 plans, durable execution, promotion conflicts and role integration."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_bridge import context, interactive, management, p1, workspace
from agent_bridge.runner import run
from agent_bridge.storage import Store, atomic_json, load_config


class P1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/x.py').write_text('VALUE=1\n', encoding='utf-8')
        (self.root / 'AGENTS.md').write_text('Preserve the rules.\n', encoding='utf-8')
        self.endpoint = dict(adapter='command', command=[sys.executable, '-c',
            "from pathlib import Path; Path('src/x.py').write_text('VALUE=2\\n'); print('MOCK_DONE')"])
        self.raw = dict(project='.', state='.agent-bridge', include=['AGENTS.md', 'src'], writable=['src'],
                        parallel=1, timeout=10, endpoints={'local': self.endpoint})
        self.path = self.root / 'bridge.json'
        atomic_json(self.path, self.raw)
        self.config = load_config(self.path)
        self.session = management.Session()
        self.sequence = 0

    def call(self, action, **args):
        self.sequence += 1
        reply = self.session.handle(dict(protocol=1, id=str(self.sequence), action=action, args=args))
        return reply

    def success(self, action, **args):
        reply = self.call(action, **args)
        self.assertTrue(reply['ok'], reply)
        return reply['result']

    def plan(self, batch='b1', plan=None):
        if plan is None:
            plan = dict(schema=1, files=[dict(path=i['path'], mode='full', reason='Explicit input')
                                        for i in context.preview(self.config)['files']])
        return self.success('batch.preview', config=str(self.path), batch_id=batch, targets=['local'], prompt='Change value', plan=plan)

    def apply(self, preview):
        return self.success('operation.apply', plan_id=preview['plan_id'], apply=True)

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run(self.config, True)

    def completed(self):
        task = self.apply(self.plan())['tasks'][0]
        self.execute()
        self.assertEqual(Store(self.config['state']).rows()[0]['state'], 'DONE')
        return task

    def test_inventory_preview_readonly_and_required_rules(self):
        report = self.success('context.inventory', config=str(self.path))
        self.assertEqual(report['required'][0]['path'], 'AGENTS.md')
        self.plan()
        self.assertFalse(self.config['state'].exists())
        plan = dict(schema=1, files=[dict(path=i['path'], mode='omit', reason='skip') for i in report['files']])
        self.assertFalse(self.call('batch.preview', config=str(self.path), batch_id='b2', targets=['local'], prompt='x', plan=plan)['ok'])
        self.assertFalse(self.config['state'].exists())

    def test_submission_is_one_use_and_does_not_start_runner(self):
        preview = self.plan()
        result = self.apply(preview)
        self.assertFalse(result['starts_runner'])
        self.assertEqual(Store(self.config['state']).control()['calls_started'], 0)
        self.assertEqual(self.call('operation.apply', plan_id=preview['plan_id'], apply=True)['problem']['code'], 'PLAN_EXPIRED')
        other = self.plan()
        self.assertFalse(self.call('operation.apply', plan_id=other['plan_id'], apply=True)['ok'])
        self.assertEqual(len(Store(self.config['state']).rows()), 1)

    def test_preview_changes_and_config_changes_consume_without_submission(self):
        for changed in ('file', 'config'):
            preview = self.plan(changed)
            if changed == 'file':
                (self.root / 'src/x.py').write_text('VALUE=99\n')
            else:
                atomic_json(self.path, dict(self.raw, parallel=2))
            self.assertFalse(self.call('operation.apply', plan_id=preview['plan_id'], apply=True)['ok'])
            self.assertFalse(self.config['state'].exists())

    def test_budget_failure_and_unknown_endpoint_are_readonly(self):
        atomic_json(self.path, dict(self.raw, context_budget=dict(max_bytes=1)))
        self.assertFalse(self.call('batch.preview', config=str(self.path), batch_id='bad', targets=['local'], prompt='x', plan=dict(schema=1, files=[]))['ok'])
        self.assertFalse(self.config['state'].exists())
        for targets in ([{}], ['missing'], ['local', 'local']):
            self.assertFalse(self.call('batch.preview', config=str(self.path), batch_id='bad', targets=targets, prompt='x', plan={})['ok'])

    def test_sensitive_inputs_are_rejected_before_hashing_or_copying(self):
        secret = self.root / 'src/.env'
        secret.write_text('SECRET_FIXTURE=local-only', encoding='utf-8')
        with patch.object(context, 'preview', side_effect=AssertionError('Sensitive inventory must not be hashed')):
            self.assertFalse(self.call('context.inventory', config=str(self.path))['ok'])
            self.assertFalse(self.call('batch.preview', config=str(self.path), batch_id='secret', targets=['local'], prompt='x', plan={})['ok'])
        self.assertFalse(self.config['state'].exists())

    def test_diff_selected_promotion_and_recovery_preserve_calls(self):
        task = self.completed()
        report = self.success('proposal.inspect', config=str(self.path), task_id=task, path='src/x.py')
        self.assertEqual(report['diff']['before'].replace('\r\n', '\n'), 'VALUE=1\n')
        self.assertEqual(report['diff']['after'].replace('\r\n', '\n'), 'VALUE=2\n')
        promoted = self.apply(self.success('proposal.preview', config=str(self.path), task_id=task, paths=['src/x.py']))
        self.assertEqual((self.root / 'src/x.py').read_text(), 'VALUE=2\n')
        recovered = self.apply(self.success('recovery.preview', config=str(self.path), task_id=task, backup=promoted['backup']))
        self.assertEqual(recovered['state'], 'ROLLED_BACK')
        self.assertEqual((self.root / 'src/x.py').read_text(), 'VALUE=1\n')
        self.assertEqual(Store(self.config['state']).control()['calls_started'], 1)

    def test_original_and_proposal_conflicts_prevent_writes(self):
        task = self.completed()
        preview = self.success('proposal.preview', config=str(self.path), task_id=task, paths=['src/x.py'])
        (self.root / 'src/x.py').write_text('NEW EDIT\n')
        self.assertFalse(self.call('operation.apply', plan_id=preview['plan_id'], apply=True)['ok'])
        self.assertEqual((self.root / 'src/x.py').read_text(), 'NEW EDIT\n')
        (self.root / 'src/x.py').write_text('VALUE=1\n')
        preview = self.success('proposal.preview', config=str(self.path), task_id=task, paths=['src/x.py'])
        (self.config['state'] / 'workspaces' / task / 'project/src/x.py').write_text('ALTERED\n')
        self.assertFalse(self.call('operation.apply', plan_id=preview['plan_id'], apply=True)['ok'])
        self.assertEqual((self.root / 'src/x.py').read_text(), 'VALUE=1\n')

    def test_failed_provider_copy_remains_reviewable_without_automatic_retry(self):
        endpoint = dict(adapter='command', command=[sys.executable, '-c', "from pathlib import Path; import sys; Path('src/x.py').write_text('VALUE=3\\n'); sys.exit(1)"])
        atomic_json(self.path, dict(self.raw, endpoints={'local': endpoint}))
        self.config = load_config(self.path)
        task = self.apply(self.plan('failed-copy'))['tasks'][0]
        self.execute()
        store = Store(self.config['state'])
        self.assertEqual(store.rows()[0]['state'], 'ERROR')
        view = self.success('proposal.inspect', config=str(self.path), task_id=task, path='src/x.py')
        self.assertEqual(view['diff']['after'].replace('\r\n', '\n'), 'VALUE=3\n')
        self.assertEqual((self.root / 'src/x.py').read_text(), 'VALUE=1\n')
        self.assertEqual(store.control()['calls_started'], 1)

    def test_other_connection_expiry_and_cross_plan_type(self):
        preview = self.plan()
        other = management.Session().handle(dict(protocol=1, id='other', action='operation.apply', args=dict(plan_id=preview['plan_id'], apply=True)))
        self.assertEqual(other['problem']['code'], 'PLAN_EXPIRED')
        self.assertEqual(self.call('setup.apply', plan_id=preview['plan_id'], apply=True)['problem']['code'], 'PLAN_EXPIRED')
        self.success('operation.cancel', plan_id=preview['plan_id'])
        self.assertFalse(self.config['state'].exists())

    def test_exact_provider_resume_project_and_identity_are_readonly(self):
        database = self.config['state'] / 'interactive.sqlite3'
        interactive.initialize(database)
        interactive.begin(database, session_id='managed', node='node', project=str(self.root), provider='claude', name='Claude', owner='owner')
        sid = 'caa20871-3e56-4e86-a99e-443d43f36fa9'
        interactive.record(database, session_id='managed', event_id='event', kind='session_started', data=dict(provider_session=sid))
        before = database.read_bytes()
        result = self.success('interactive.resume', database=str(database), project=str(self.root), session_id='managed')
        self.assertEqual(result['provider_session'], sid)
        self.assertFalse(result['automatic_resend'])
        self.assertFalse(self.call('interactive.resume', database=str(database), project=str(self.root.parent), session_id='managed')['ok'])
        self.assertEqual(database.read_bytes(), before)

    def test_background_role_flow_test_log_and_parent_budget(self):
        fake = self.root / 'fake-codex.py'
        fake.write_text("import sys,json\nfrom pathlib import Path\nprompt=sys.stdin.read()\nif 'Role: developer.' in prompt: Path('src/x.py').write_text('VALUE=2\\n')\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'MOCK_OK'}}))\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}))\n", encoding='utf-8')
        developer = dict(adapter='codex', command=[sys.executable, str(fake)], sandbox='workspace-write')
        reviewer = dict(developer, sandbox='read-only')
        atomic_json(self.root / 'role_workflow.json', dict(developer=developer, reviewer=reviewer,
            checks=[[sys.executable, '-c', "from pathlib import Path; assert 'VALUE=2' in Path('src/x.py').read_text(); print('CHECK_OK')"]]))
        Store(self.config['state']).set_limit(2)
        preview = self.success('roles.preview', config=str(self.path), flow_id='flow1', prompt='Change the value')
        self.assertEqual(preview['report']['max_provider_calls'], 2)
        self.apply(preview)
        result_file = self.root / '.role-flows/flow1/result.json'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                receipt = json.loads((self.config['state'] / 'p1-jobs/flow1.json').read_text(encoding='utf-8'))
            except (PermissionError, FileNotFoundError):
                # Windows may briefly deny readers during an atomic replacement.
                time.sleep(.05)
                continue
            if receipt['state'] == 'FAILED':
                self.fail((self.config['state'] / 'p1-jobs/flow1.log').read_text(encoding='utf-8') + repr(receipt))
            if receipt['state'] == 'FINISHED' and result_file.exists():
                saved = json.loads(result_file.read_text(encoding='utf-8'))
                if saved['state'] in ('REVIEW_DONE', 'ERROR', 'TESTS_FAILED', 'DEVELOPER_FAILED', 'REVIEWER_FAILED'):
                    break
            time.sleep(.05)
        else:
            self.fail('Role job did not complete: ' + (self.config['state'] / 'p1-jobs/flow1.log').read_text(encoding='utf-8'))
        self.assertEqual(saved['state'], 'REVIEW_DONE', saved)
        self.assertEqual(Store(self.config['state']).control()['calls_started'], 2)
        self.assertEqual((self.root / 'src/x.py').read_text(), 'VALUE=1\n')
        overview = self.success('roles.overview', config=str(self.path))
        self.assertEqual(overview['flows'][0]['saved_phase'], 'REVIEW_DONE')
        log = self.success('roles.log', config=str(self.path), flow_id='flow1', log='test-0.log')
        self.assertIn('CHECK_OK', log['text'])
        task = saved['developer']['task']
        self.success('roles.inspect', config=str(self.path), flow_id='flow1', role='developer', path='src/x.py')
        self.assertFalse(self.call('proposal.inspect', config=str(self.path), task_id=task)['ok'])
        promoted = self.apply(self.success('roles.promote.preview', config=str(self.path), flow_id='flow1', role='developer', task_id=task, paths=['src/x.py']))
        self.apply(self.success('roles.recover.preview', config=str(self.path), flow_id='flow1', role='developer', task_id=task, backup=promoted['backup']))
        self.assertEqual((self.root / 'src/x.py').read_text(), 'VALUE=1\n')
        self.assertFalse(self.call('roles.preview', config=str(self.path), flow_id='flow1', prompt='Repeat')['ok'])
        self.assertFalse(self.call('roles.log', config=str(self.path), flow_id='flow1', log='../bridge.json')['ok'])

    def test_reviewed_summary_plan_revalidates_provenance(self):
        from test_summaries import SummaryTests
        fixture = SummaryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        preview = self.success('batch.preview', config=str(fixture.config_path), batch_id='summary-1',
                               targets=['one'], prompt=fixture.prompt, plan=fixture.plan)
        self.assertEqual(preview['report']['context']['summary_status'], 'VALID')
        (fixture.root / 'docs/history.md').write_text('Source changed', encoding='utf-8')
        self.assertFalse(self.call('operation.apply', plan_id=preview['plan_id'], apply=True)['ok'])
        self.assertFalse(fixture.c['state'].exists())


class P1ArchiveTests(unittest.TestCase):
    from test_flow_cleanup import FlowCleanupTests as Fixture
    run_flow = Fixture.run_flow
    preview = Fixture.preview
    snapshot = Fixture.snapshot
    call = P1Tests.call
    success = P1Tests.success
    apply = P1Tests.apply

    def setUp(self):
        self.Fixture.setUp(self)
        self.session = management.Session()
        self.sequence = 0
        self.path = self.root / 'bridge.json'

    def test_archive_and_resumed_archive_preserve_originals(self):
        before = self.snapshot()
        writer = p1.helper('flow_archive_writer')
        with patch.object(p1.time, 'time', return_value=self.now):
            preview = self.success('archive.preview', config=str(self.path), flow_id='sample', days=1)
            self.assertTrue(preview['report']['eligible'])
            with patch.object(writer, 'durable_write', side_effect=OSError('Interrupted write')):
                self.assertFalse(self.call('operation.apply', plan_id=preview['plan_id'], apply=True)['ok'])
            journal = json.loads((self.flow / 'cleanup.json').read_text(encoding='utf-8'))
            resume = self.success('archive.resume.preview', config=str(self.path), flow_id='sample', operation=journal['operation'])
            result = self.apply(resume)
        self.assertEqual(result['artifact_state'], 'ARCHIVE_READY')
        after = self.snapshot()
        for name, data in before.items():
            self.assertEqual(after[name], data, name)

    def test_role_prune_is_explicit_and_preserves_answer_and_original(self):
        task = self.result['developer']['task']
        original = (self.root / 'src/x.py').read_bytes()
        with patch.object(p1.time, 'time', return_value=self.now):
            preview = self.success('roles.prune.preview', config=str(self.path), flow_id='sample', role='developer', task_id=task, days=1)
            self.assertTrue(preview['report']['artifact']['eligible'])
            result = self.apply(preview)
        self.assertTrue(result['artifact']['deleted'])
        self.assertEqual((self.root / 'src/x.py').read_bytes(), original)
        result = self.success('result', config=str(self.path), task_id=task)
        self.assertTrue(result['answer']['text'])


if __name__ == '__main__':
    unittest.main()
