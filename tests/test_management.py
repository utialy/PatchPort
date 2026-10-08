import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import management as api
from agent_bridge.storage import Store, atomic_json


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project \uac80\uc0ac with spaces'
        self.project.mkdir()
        (self.project / 'input.txt').write_text('Original input', encoding='utf-8')
        self.config = self.project / 'bridge.json'
        atomic_json(self.config, dict(project='.', state='.agent-bridge', include=['input.txt'],
                                     writable=['input.txt'], parallel=1, timeout=10,
                                     endpoints={'mock': dict(adapter='command', command=[sys.executable], parallel=1)}))
        self.session = api.Session()
        self.serial = 0

    def request(self, action, **args):
        self.serial += 1
        return dict(protocol=1, id=str(self.serial), action=action, args=args)

    def call(self, action, **args):
        return self.session.handle(self.request(action, **args))

    def preview(self):
        reply = self.call('setup.preview', project=str(self.project))
        self.assertTrue(reply['ok'], reply)
        return reply['result']['plan_id']

    def test_preview_cancel_expiry_and_other_session_do_not_write(self):
        before = {p.name: p.read_bytes() for p in self.project.iterdir()}
        plan = self.preview()
        other = api.Session().handle(self.request('setup.apply', plan_id=plan, apply=True))
        self.assertEqual(other['problem']['code'], 'PLAN_EXPIRED')
        self.assertTrue(self.call('setup.cancel', plan_id=plan)['ok'])
        self.assertEqual(self.call('setup.apply', plan_id=plan, apply=True)['problem']['code'], 'PLAN_EXPIRED')
        plan = self.preview()
        with patch.object(api.time, 'monotonic', return_value=float('inf')):
            self.assertEqual(self.call('setup.apply', plan_id=plan, apply=True)['problem']['code'], 'PLAN_EXPIRED')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.project.iterdir()})

    def test_apply_once_and_no_queue_or_provider(self):
        plan = self.preview()
        with patch.object(api.runner_manager, 'start', side_effect=AssertionError('Unexpected start')):
            reply = self.call('setup.apply', plan_id=plan, apply=True)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['connection'], 'READY')
        self.assertEqual(reply['result']['ai_calls'], 0)
        self.assertFalse((self.project / '.agent-bridge').exists())
        self.assertEqual(self.call('setup.apply', plan_id=plan, apply=True)['problem']['code'], 'PLAN_EXPIRED')

    def test_changed_input_consumes_failed_plan(self):
        plan = self.preview()
        (self.project / 'input.txt').write_text('Changed', encoding='utf-8')
        self.assertEqual(self.call('setup.apply', plan_id=plan, apply=True)['problem']['code'], 'OPERATION_FAILED')
        self.assertFalse((self.project / '.bridge-integration.json').exists())
        self.assertEqual(self.call('setup.apply', plan_id=plan, apply=True)['problem']['code'], 'PLAN_EXPIRED')

    def test_partial_apply_report_and_plan_consumption(self):
        plan = self.preview()
        with patch.object(api.setup_project, 'apply_setup', return_value=dict(ok=False, connection='PARTIAL', backup='saved')):
            reply = self.call('setup.apply', plan_id=plan, apply=True)
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['result']['connection'], 'PARTIAL')
        self.assertEqual(reply['result']['backup'], 'saved')
        self.assertNotIn(plan, self.session.plans)

    def test_capacity_and_report_is_detached_from_plan(self):
        reply = self.call('setup.preview', project=str(self.project))
        reply['result']['report']['include'].append('secret')
        plan = self.session.plans[reply['result']['plan_id']][1]
        self.assertEqual(plan['report']['include'], ['input.txt'])
        with patch.object(api, 'MAX_PLANS', 1):
            self.assertEqual(self.call('setup.preview', project=str(self.project))['problem']['code'], 'PLAN_CAPACITY')

    def test_invalid_requests_are_rejected_before_any_state_write(self):
        cases = [dict(config=str(self.config), apply=False), dict(config=str(self.config), apply=1),
                 dict(config=str(self.config), apply=True, command='secret'),
                 dict(config='bridge.json', apply=True)]
        for args in cases:
            self.assertFalse(self.call('pause', **args)['ok'])
        for value in (True, -1, 2**63, '1', 1.2):
            self.assertFalse(self.call('limit', config=str(self.config), apply=True, max_calls=value)['ok'])
        for action in ('shell', [], None):
            self.assertEqual(self.session.handle(self.request(action))['problem']['code'], 'UNSUPPORTED_ACTION')
        request = self.request('pause', config=str(self.config), apply=True)
        request['protocol'] = True
        self.assertEqual(self.session.handle(request)['problem']['code'], 'UNSUPPORTED_PROTOCOL')
        self.assertFalse((self.project / '.agent-bridge').exists())

    def test_duplicate_request_cannot_repeat_mutation(self):
        request = self.request('pause', config=str(self.config), apply=True)
        self.assertTrue(self.session.handle(request)['ok'])
        self.call('resume', config=str(self.config), apply=True)
        self.assertEqual(self.session.handle(request)['problem']['code'], 'DUPLICATE_REQUEST')
        self.assertFalse(Store(self.project / '.agent-bridge').control()['paused'])

    def test_controls_preserve_claim_count_and_status_is_readonly(self):
        status = self.call('runner.status', config=str(self.config))
        self.assertTrue(status['ok'], status)
        self.assertEqual(status['result']['diagnostics']['authentication'], 'NOT_CHECKED')
        self.assertFalse((self.project / '.agent-bridge').exists())
        store = Store(self.project / '.agent-bridge')
        task = store.submit('sample', ['mock'], 'Do not expose this prompt', ['mock'])[0]
        self.assertTrue(store.claim(task))
        for action, extra in [('pause', {}), ('limit', {'max_calls': 1}), ('resume', {}), ('limit', {'max_calls': None})]:
            reply = self.call(action, config=str(self.config), apply=True, **extra)
            self.assertTrue(reply['ok'], reply)
            self.assertEqual(reply['result']['calls_started'], 1)

    def test_result_readonly_no_prompt_and_preserved_answer(self):
        missing = self.call('result', config=str(self.config), task_id='sample--mock')
        self.assertEqual(missing['problem']['code'], 'RESULT_NOT_FOUND')
        self.assertFalse((self.project / '.agent-bridge').exists())
        store = Store(self.project / '.agent-bridge')
        task = store.submit('sample', ['mock'], 'PRIVATE PROMPT', ['mock'])[0]
        store.finish(task, 'DONE', dict(answer=dict(text='\ub2f5\ubcc0', status='COMPLETE')))
        before = store.db.read_bytes()
        reply = self.call('result', config=str(self.config), task_id=task)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result']['answer']['text'], '\ub2f5\ubcc0')
        self.assertNotIn('PRIVATE PROMPT', json.dumps(reply))
        self.assertEqual(before, store.db.read_bytes())

    def test_task_list_missing_database_creates_nothing(self):
        before = {str(p): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        with patch.object(api, 'Store', side_effect=AssertionError('No initialization')):
            reply = self.call('tasks.list', config=str(self.config))
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['result'], dict(tasks=[], limit=20, more=False))
        self.assertFalse((self.project / '.agent-bridge').exists())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.project.rglob('*') if p.is_file()})

    def test_task_list_bounded_order_normalization_and_no_sensitive_payload(self):
        store = Store(self.project / '.agent-bridge')
        states = ('PLAN_QUEUED', 'ROLE_RUNNING', 'SUMMARY_QUEUED', 'DONE')
        ids = [store.submit('item-' + str(i), ['mock'], 'PRIVATE PROMPT', ['mock'])[0] for i in range(4)]
        self.assertTrue(store.claim(ids[0]))
        store.finish(ids[0], 'DONE', dict(answer=dict(text='PRIVATE ANSWER', status='COMPLETE')))
        store.pause(True)
        store.set_limit(1)
        with store.connect() as db:
            for task_id, state in zip(ids, states):
                db.execute('UPDATE tasks SET created=100,state=? WHERE id=?', (state, task_id))
        before = {str(p): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        control = store.control()
        with patch.object(api, 'Store', side_effect=AssertionError('No mutation')):
            reply = self.call('tasks.list', config=str(self.config), limit=2)
            all_rows = self.call('tasks.list', config=str(self.config), limit=100)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual([row['id'] for row in reply['result']['tasks']], list(reversed(ids[2:])))
        self.assertTrue(reply['result']['more'])
        self.assertEqual([row['state'] for row in all_rows['result']['tasks']], ['DONE', 'QUEUED', 'RUNNING', 'QUEUED'])
        self.assertFalse(all_rows['result']['more'])
        self.assertNotIn('PRIVATE', json.dumps(all_rows))
        self.assertEqual(set(reply['result']['tasks'][0]), {'id','batch','endpoint','state','created','started','finished'})
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.project.rglob('*') if p.is_file()})
        self.assertEqual(control, store.control())

    def test_task_list_rejects_invalid_limits_without_creating_state(self):
        for limit in (True, False, 0, -1, 101, '20', None, 1.5):
            with self.subTest(limit=limit):
                reply = self.call('tasks.list', config=str(self.config), limit=limit)
                self.assertEqual(reply['problem']['code'], 'INVALID_ARGUMENTS')
        self.assertFalse((self.project / '.agent-bridge').exists())

    def test_task_list_corrupt_database_is_error_and_unchanged(self):
        state = self.project / '.agent-bridge'
        state.mkdir()
        db = state / 'queue.sqlite3'
        db.write_bytes(b'not a database')
        reply = self.call('tasks.list', config=str(self.config))
        self.assertFalse(reply['ok'])
        self.assertEqual(db.read_bytes(), b'not a database')
        self.assertEqual(list(state.iterdir()), [db])

    def test_runner_actions_forward_identity_cancel_and_bounded_wait(self):
        for action, extra in [('runner.start', {}), ('runner.stop', {'run_id': 'run-1', 'cancel_active': True})]:
            function = 'start' if action.endswith('start') else 'stop'
            with patch.object(api.runner_manager, function, return_value=({'outcome': 'PENDING'}, 2)) as called:
                reply = self.call(action, config=str(self.config), apply=True, timeout=2, **extra)
            self.assertFalse(reply['ok'])
            self.assertEqual(called.call_args.args[1:], (2,) if function == 'start' else ('run-1', True, 2))
        for timeout in (True, 0, 31, float('nan'), float('inf')):
            self.assertFalse(self.call('runner.start', config=str(self.config), apply=True, timeout=timeout)['ok'])

    def test_pipe_errors_bounds_and_stdout_separation(self):
        request = self.request('discover', provider='codex')
        source = io.BytesIO(b'{"id":1,"id":2}\n' + b'{}\xff\n' + b'{"x":NaN}\n'
                            + json.dumps(request).encode() + b'\n')
        target = io.BytesIO()
        def discover(provider):
            print('Diagnostic goes to stderr')
            return []
        with patch.object(api.setup_project, 'discover', side_effect=discover), patch('sys.stderr', new=io.StringIO()) as logs:
            self.assertEqual(api.serve(source, target), 0)
        replies = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual([r['problem']['code'] for r in replies[:3]], ['INVALID_JSON'] * 3)
        self.assertTrue(replies[3]['ok'])
        self.assertIn('Diagnostic', logs.getvalue())
        target = io.BytesIO()
        self.assertEqual(api.serve(io.BytesIO(b'x' * (api.MAX_REQUEST + 1)), target), 1)
        self.assertEqual(json.loads(target.getvalue())['problem']['code'], 'REQUEST_TOO_LARGE')
        large = api.encode(api.response('one', result='x' * api.MAX_RESPONSE))
        self.assertEqual(json.loads(large)['problem']['code'], 'RESPONSE_TOO_LARGE')

    def test_exception_payload_is_not_returned(self):
        with patch.object(api.setup_project, 'discover', side_effect=ValueError('SECRET ENV VALUE')):
            reply = self.call('discover', provider='codex')
        self.assertNotIn('SECRET', json.dumps(reply))

    def test_bad_config_and_nonfinite_result_are_structured_errors(self):
        self.config.write_text('[]', encoding='utf-8')
        self.assertEqual(self.call('runner.status', config=str(self.config))['problem']['code'], 'OPERATION_FAILED')
        reply = json.loads(api.encode(api.response('one', result=float('nan'))))
        self.assertEqual(reply['problem']['code'], 'INVALID_RESULT')

    def test_idle_partial_input_exits_and_session_capacity_rejects(self):
        import threading
        release = threading.Event()
        class BlockedPipe:
            def readline(self, size):
                release.wait(2)
                return b''
        try:
            self.assertEqual(api.serve(BlockedPipe(), io.BytesIO(), idle_timeout=.02), 0)
        finally:
            release.set()
        with patch.object(api, 'MAX_REQUESTS', 0):
            self.assertEqual(self.call('pause', config=str(self.config), apply=True)['problem']['code'], 'SESSION_FULL')
        self.assertFalse((self.project / '.agent-bridge').exists())

    def test_new_project_uses_selected_cli_and_current_core_python(self):
        target = self.project / 'new project'
        reply = self.call('setup.preview', project=str(target), providers={'codex': sys.executable},
                          starter=True, create_project=True)
        self.assertTrue(reply['ok'], reply)
        plan = self.session.plans[reply['result']['plan_id']][1]
        self.assertEqual(plan['install']['python'], Path(sys.executable).resolve())
        self.assertFalse(target.exists())
        self.assertTrue(self.call('setup.apply', plan_id=reply['result']['plan_id'], apply=True)['ok'])
        data = json.loads((target / 'bridge.json').read_text(encoding='utf-8'))
        self.assertEqual(data['endpoints']['codex']['sandbox'], 'read-only')
        self.assertFalse((target / '.agent-bridge').exists())

    def test_both_process_entrypoints_and_eof(self):
        request = self.request('runner.status', config=str(self.config))
        env = dict(os.environ, PYTHONPATH=str(Path(api.__file__).resolve().parent.parent))
        for module, args in [('agent_bridge', ['manage']), ('agent_bridge.management', [])]:
            proc = subprocess.run([sys.executable, '-B', '-m', module, *args],
                                  input=json.dumps(request).encode() + b'\n', capture_output=True,
                                  env=env, cwd=self.temp.name, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(proc.stdout)['ok'], proc.stdout)
        self.assertFalse((self.project / '.agent-bridge').exists())

    def test_process_exits_on_idle_with_stdin_still_open(self):
        env = dict(os.environ, PYTHONPATH=str(Path(api.__file__).resolve().parent.parent))
        proc = subprocess.Popen([sys.executable, '-B', '-c',
                                 'from agent_bridge.management import serve; serve(idle_timeout=.05)'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()

    def test_pipe_preview_and_apply_while_input_remains_open(self):
        import threading
        env = dict(os.environ, PYTHONPATH=str(Path(api.__file__).resolve().parent.parent))
        proc = subprocess.Popen([sys.executable, '-B', '-m', 'agent_bridge', 'manage'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        watchdog = threading.Timer(20, proc.kill)
        watchdog.start()
        try:
            def send(action, **args):
                proc.stdin.write((json.dumps(self.request(action, **args)) + '\n').encode())
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())
            plan = send('setup.preview', project=str(self.project))
            applied = send('setup.apply', plan_id=plan['result']['plan_id'], apply=True)
            self.assertTrue(applied['ok'], applied)
            self.assertFalse((self.project / '.agent-bridge').exists())
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            watchdog.cancel()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == '__main__':
    unittest.main()
