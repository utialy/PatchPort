"""Synthetic launchctl fixtures; these tests are not native macOS evidence."""
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_bridge import launchd_service as launchd, service_manager as service, runner_manager
from agent_bridge.health import code_identity
from agent_bridge.storage import atomic_json, FileLock, Store, guarded_removal


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project %$ \ud55c\uae00'
        self.project.mkdir()
        (self.project / 'input.txt').write_bytes(b'input')
        self.path = self.project / 'bridge.json'
        atomic_json(self.path, dict(project='.', state='.agent-bridge', include=['input.txt'], writable=['input.txt'],
            parallel=1, timeout=10, endpoints={'mock': dict(adapter='command', command=[sys.executable])}))
        self.config = runner_manager.checked_config(self.path)
        self.manual = self.root / 'Application Support/Agents'
        self.login = self.root / 'LaunchAgents'
        self.uid = os.getuid() if os.name == 'posix' else 501
        self.calls, self.job, self.disabled = [], None, False
        self.on_kickstart = None
        self.active_lock = None
        for item in [patch.object(launchd.sys, 'platform', 'darwin'),
                     patch.object(launchd.os, 'getuid', return_value=self.uid, create=True),
                     patch.object(launchd, 'directories', return_value=(self.manual, self.login)),
                     patch.object(launchd.child, 'verify_service_runtime'),
                     patch.object(launchd.child, 'launchctl_user', side_effect=self.command)]:
            item.start()
            self.addCleanup(item.stop)
        if os.name == 'nt':
            def remove(path, expected, root):
                with guarded_removal(root, [(path, expected)]) as perform:
                    list(perform())
            native = patch.object(launchd, 'remove_owned_launcher', side_effect=remove)
            native.start()
            self.addCleanup(native.stop)
        self.addCleanup(self.unlock)

    def unlock(self):
        if self.active_lock:
            self.active_lock.__exit__(None, None, None)
            self.active_lock = None

    def command(self, args, **kwargs):
        self.calls.append(args)
        domain = 'gui/' + str(self.uid)
        name = launchd.label(self.config, self.uid)
        target = domain + '/' + name
        if args == ['print', domain]:
            return 0, '', ''
        if args == ['print-disabled', domain]:
            return 0, 'disabled services = {\n    "' + name + '" => ' + str(self.disabled).lower() + '\n}\n', ''
        if args == ['print', target]:
            if self.job is None:
                return 113, '', 'Could not find service "' + name + '" in domain for user gui: ' + str(self.uid)
            return 0, self.print_job(target), ''
        if args[0] == 'bootstrap':
            self.assertEqual(args[1], domain)
            data = plistlib.loads(Path(args[2]).read_bytes())
            self.assertFalse(data['RunAtLoad'])
            self.job = dict(path=args[2], program=data['ProgramArguments'][0], arguments=data['ProgramArguments'],
                            state='not running', pid=None)
            return 0, '', ''
        if args == ['kickstart', target]:
            if self.on_kickstart:
                self.on_kickstart()
            return 0, '', ''
        if args == ['bootout', target]:
            self.job = None
            return 0, '', ''
        raise AssertionError(args)

    def print_job(self, target):
        job = self.job
        lines = [target + ' = {', '\tpath = ' + job['path'], '\tstate = ' + job['state'],
                 '\tprogram = ' + job['program'], '\targuments = {']
        lines += ['\t\t' + value for value in job['arguments']]
        lines += ['\t}', '\tenvironment = {', '\t\tSECRET_FIXTURE = do-not-return', '\t}']
        if job['pid']:
            lines.append('\tpid = ' + str(job['pid']))
        return '\n'.join(lines + ['}']) + '\n'

    def apply(self, action):
        plan = service.preview(self.config, action)
        return service.apply(self.config, action, plan['plan_token'])

    def mutations(self):
        return [c for c in self.calls if c[0] in ('bootstrap', 'kickstart', 'bootout', 'enable', 'disable')]

    def fake_running(self):
        registration = launchd.registration(self.config)
        now = time.time()
        record = dict(protocol=1, run_id='synthetic-run', identity=runner_manager.execution_identity(self.config),
                      started=now, mode='RUNNING', acknowledged=None, ended=None,
                      backend=launchd.BACKEND, service_id=registration['id'])
        Store(self.config['state']).register_runner(record)
        self.active_lock = FileLock(self.config['state'] / 'runner.lock')
        self.active_lock.__enter__()
        atomic_json(self.config['state'] / 'health.json', dict(schema=1, pid=12345, started=now,
                    heartbeat=now, active=0, code=code_identity(), management=record))
        self.job.update(state='running', pid=12345)

    def test_registration_preview_and_apply_do_not_bootstrap_or_create_queue(self):
        plan = service.preview(self.config, 'register')
        self.assertFalse(self.config['state'].exists())
        self.assertFalse(self.manual.exists())
        self.assertEqual(plan['backend'], 'launchd-user')
        self.assertEqual(self.apply('register')['outcome'], 'REGISTERED')
        self.assertFalse((self.config['state'] / 'queue.sqlite3').exists())
        self.assertEqual(self.mutations(), [])
        record = launchd.registration(self.config)
        self.assertFalse(plistlib.loads(Path(record['unit']).read_bytes())['RunAtLoad'])
        self.assertFalse(Path(record['login_unit']).exists())
        self.assertEqual(self.apply('register')['outcome'], 'UNCHANGED')

    def test_enable_disable_only_manage_owned_login_copy(self):
        self.apply('register')
        store = Store(self.config['state'])
        store.set_limit(2)
        original = store.db.read_bytes()
        self.assertTrue(self.apply('enable')['automatic_start'])
        record = launchd.registration(self.config)
        login = Path(record['login_unit'])
        self.assertTrue(plistlib.loads(login.read_bytes())['RunAtLoad'])
        self.assertFalse(self.apply('disable')['automatic_start'])
        self.assertFalse(login.exists())
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')
        self.assertEqual(original, store.db.read_bytes())

    def test_active_disable_preserves_loaded_login_job(self):
        self.apply('register')
        self.apply('enable')
        record = launchd.registration(self.config)
        self.command(['bootstrap', 'gui/' + str(self.uid), record['unit']])
        self.job['path'] = record['login_unit']
        self.fake_running()
        before = self.mutations()[:]
        result = self.apply('disable')
        self.assertEqual(result['manager']['pid'], 12345)
        self.assertFalse(result['automatic_start'])
        self.assertEqual(self.mutations(), before)

    def test_start_reuse_and_stop_route_through_launchd(self):
        self.apply('register')
        self.on_kickstart = self.fake_running
        result, code = runner_manager.start(self.config)
        self.assertEqual((result['outcome'], code), ('STARTED', 0))
        self.assertEqual(result['backend'], 'launchd-user')
        self.assertIsNone(launchd.registration(self.config).get('pending_start'))
        self.assertEqual(runner_manager.start(self.config)[0]['outcome'], 'REUSED')
        self.assertEqual(sum(c[0] == 'kickstart' for c in self.calls), 1)
        def stopped(config, run_id, cancel, timeout, **kwargs):
            self.assertTrue(cancel)
            self.assertTrue(kwargs['_service'] and kwargs['_lock_held'])
            Store(config['state']).runner_transition(run_id, mode='STOPPED')
            self.unlock()
            self.job.update(pid=None, state='not running')
            return dict(outcome='STOPPED', run_id=run_id), 0
        with patch.object(runner_manager, 'stop', side_effect=stopped):
            self.assertEqual(service.stop(self.config, result['run_id'], True)[0]['outcome'], 'STOPPED')
        self.assertIsNone(self.job)
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')

    def test_pending_start_blocks_second_kickstart_and_explicit_unregistration_clears_it(self):
        self.apply('register')
        result, code = runner_manager.start(self.config, timeout=.001)
        self.assertEqual((result['outcome'], code), ('START_UNCONFIRMED', 2))
        self.assertIsNotNone(launchd.registration(self.config).get('pending_start'))
        with self.assertRaises(ValueError):
            runner_manager.start(self.config, timeout=.001)
        self.assertEqual(sum(c[0] == 'kickstart' for c in self.calls), 1)
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')

    def test_start_command_failure_preserves_pending_outcome(self):
        self.apply('register')
        original = self.command
        def broken(args, **kwargs):
            if args[0] == 'kickstart':
                raise RuntimeError('Reply lost')
            return original(args, **kwargs)
        with patch.object(launchd.child, 'launchctl_user', side_effect=broken):
            with self.assertRaises(RuntimeError):
                runner_manager.start(self.config)
        self.assertIsNotNone(launchd.registration(self.config).get('pending_start'))

    def test_unexpected_bootstrap_transition_never_kickstarts(self):
        self.apply('register')
        original = self.command
        def transitioning(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == 'bootstrap':
                self.job['state'] = 'spawn scheduled'
            return result
        with patch.object(launchd.child, 'launchctl_user', side_effect=transitioning):
            with self.assertRaises(RuntimeError):
                runner_manager.start(self.config)
        self.assertFalse(any(c[0] == 'kickstart' for c in self.calls))

    def test_damaged_heartbeat_does_not_reuse_or_start_again(self):
        self.apply('register')
        self.on_kickstart = self.fake_running
        self.assertEqual(runner_manager.start(self.config)[1], 0)
        path = self.config['state'] / 'health.json'
        value = json.loads(path.read_text())
        value['management'] = 'damaged'
        atomic_json(path, value)
        with self.assertRaises(ValueError):
            runner_manager.start(self.config)
        self.assertEqual(sum(c[0] == 'kickstart' for c in self.calls), 1)

    def test_foreign_login_copy_and_modified_plist_are_not_adopted(self):
        proposed = launchd.proposed(self.config)
        self.login.mkdir()
        login = Path(proposed['login_unit'])
        login.write_bytes(launchd.render(proposed['identity'], proposed['id'], proposed['name'], automatic=True))
        with self.assertRaises(ValueError):
            service.preview(self.config, 'register')
        login.unlink()
        self.apply('register')
        manual = Path(proposed['unit'])
        manual.write_bytes(b'user changes')
        with self.assertRaises(ValueError):
            service.preview(self.config, 'unregister')
        self.assertEqual(manual.read_bytes(), b'user changes')

    def test_external_disabled_override_is_never_changed(self):
        self.disabled = True
        with self.assertRaises(ValueError):
            service.preview(self.config, 'register')
        self.disabled = False
        self.apply('register')
        self.disabled = True
        with self.assertRaises(ValueError):
            runner_manager.start(self.config)
        with self.assertRaises(ValueError):
            service.preview(self.config, 'enable')
        self.assertTrue(service.status(self.config)['externally_disabled'])
        self.assertEqual(self.mutations(), [])

    def test_unknown_output_and_domain_failure_never_mean_absent(self):
        proposed = launchd.proposed(self.config)
        original = self.command
        def denied(args, **kwargs):
            if args[0] == 'print' and '/' in args[1][4:]:
                return 113, '', 'Permission denied'
            return original(args, **kwargs)
        with patch.object(launchd.child, 'launchctl_user', side_effect=denied):
            with self.assertRaises(RuntimeError):
                launchd.manager_state(proposed)
        with patch.object(launchd.child, 'launchctl_user', side_effect=RuntimeError('GUI domain absent')):
            with self.assertRaises(RuntimeError):
                service.preview(self.config, 'register')
        self.assertFalse(self.config['state'].exists())

    def test_partial_registration_is_preserved_and_explicitly_removed(self):
        plan = service.preview(self.config, 'register')
        with patch.object(launchd, 'private_write', side_effect=OSError('disk failure')):
            result = service.apply(self.config, 'register', plan['plan_token'])
        self.assertEqual(result['outcome'], 'PARTIAL')
        with self.assertRaises(ValueError):
            service.guard_execution(self.config, 'manual', None)
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')

    def test_partial_disable_after_unlink_is_finalized_only_by_explicit_apply(self):
        self.apply('register')
        self.apply('enable')
        record = launchd.registration(self.config)
        Path(record['login_unit']).unlink()
        record.update(state='CHANGING', operation='disable')
        atomic_json(self.config['state'] / service.RECORD, record)
        self.assertFalse(self.apply('disable')['automatic_start'])
        self.assertEqual(launchd.registration(self.config)['state'], 'READY')

    def test_partial_enable_unowned_file_is_preserved(self):
        self.apply('register')
        record = launchd.registration(self.config)
        record.update(state='CHANGING', operation='enable')
        atomic_json(self.config['state'] / service.RECORD, record)
        self.login.mkdir()
        login = Path(record['login_unit'])
        data = launchd.render(record['identity'], record['id'], record['name'], automatic=True)
        login.write_bytes(data)
        with self.assertRaises(ValueError):
            service.preview(self.config, 'disable')
        self.assertEqual(login.read_bytes(), data)

    def test_failed_bootout_retains_registration_for_inspection(self):
        self.apply('register')
        record = launchd.registration(self.config)
        self.command(['bootstrap', 'gui/' + str(self.uid), record['unit']])
        plan = service.preview(self.config, 'unregister')
        original = self.command
        def broken(args, **kwargs):
            if args[0] == 'bootout':
                raise RuntimeError('bootout failed')
            return original(args, **kwargs)
        with patch.object(launchd.child, 'launchctl_user', side_effect=broken):
            with self.assertRaises(RuntimeError):
                service.apply(self.config, 'unregister', plan['plan_token'])
        self.assertEqual(launchd.registration(self.config)['state'], 'REMOVING')
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')

    def test_internal_entry_checks_pid_parent_and_runtime(self):
        self.apply('register')
        record = launchd.registration(self.config)
        self.command(['bootstrap', 'gui/' + str(self.uid), record['unit']])
        self.job.update(state='running', pid=os.getpid())
        with patch.object(launchd.os, 'getppid', return_value=2):
            with self.assertRaises(ValueError):
                service.guard_execution(self.config, launchd.BACKEND, record['id'], running=True)
        with patch.object(launchd.os, 'getppid', return_value=1):
            service.guard_execution(self.config, launchd.BACKEND, record['id'], running=True)
        raw = json.loads(self.path.read_text())
        raw['timeout'] = 20
        atomic_json(self.path, raw)
        changed = runner_manager.checked_config(self.path)
        with self.assertRaises(ValueError):
            service.guard_execution(changed, launchd.BACKEND, record['id'])

    def test_loaded_foreign_arguments_and_path_are_refused(self):
        self.apply('register')
        record = launchd.registration(self.config)
        self.command(['bootstrap', 'gui/' + str(self.uid), record['unit']])
        self.job['arguments'] = self.job['arguments'] + ['unexpected']
        with self.assertRaises(ValueError):
            service.status(self.config)
        self.job['arguments'] = launchd.arguments(record['identity'], record['id'])
        self.job['path'] = str(self.root / 'foreign.plist')
        with self.assertRaises(ValueError):
            service.status(self.config)

    def test_plist_roundtrip_has_no_implicit_restart_or_shell(self):
        record = launchd.proposed(self.config)
        data = plistlib.loads(launchd.render(record['identity'], record['id'], record['name']))
        self.assertFalse(data['RunAtLoad'])
        self.assertFalse(data['KeepAlive'])
        self.assertEqual(data['ProgramArguments'][0], str(Path(sys.executable).resolve()))
        self.assertEqual(data['WorkingDirectory'], str(self.project))
        self.assertEqual(data['ExitTimeOut'], 30)
        self.assertEqual(set(data['EnvironmentVariables']), {'PATH', 'PYTHONUTF8'})
        self.assertNotIn('StartInterval', data)

    def test_parser_rejects_truncated_duplicate_and_hides_environment(self):
        self.apply('register')
        record = launchd.registration(self.config)
        target = 'gui/' + str(self.uid) + '/' + record['name']
        self.command(['bootstrap', 'gui/' + str(self.uid), record['unit']])
        text = self.print_job(target)
        result = launchd.parse_print(text, target)
        self.assertNotIn('SECRET', json.dumps(result))
        for bad in (text[:-3], text.replace('\tstate = not running', '\tstate = not running\n\tstate = running'),
                    text.replace('\tstate = not running', '\tstate = running')):
            with self.assertRaises(ValueError):
                launchd.parse_print(bad, target)
        with self.assertRaises(ValueError):
            launchd.parse_disabled('unknown output', record['name'])

    def test_disabled_parser_supports_explicit_boolean_and_named_states_only(self):
        name = 'com.patchport.fixture'
        for value, expected in [('true', True), ('false', False), ('disabled', True), ('enabled', False)]:
            text = 'disabled services = {\n "' + name + '" => ' + value + '\n}\n'
            self.assertEqual(launchd.parse_disabled(text, name), expected)
        with self.assertRaises(ValueError):
            launchd.parse_disabled('disabled services = {\n "' + name + '" => unknown\n}\n', name)

    def test_stale_plan_and_active_or_enabled_unregister_are_refused(self):
        plan = service.preview(self.config, 'register')
        with self.assertRaises(ValueError):
            service.apply(self.config, 'register', 'wrong-token')
        self.assertFalse(self.config['state'].exists())
        service.apply(self.config, 'register', plan['plan_token'])
        self.apply('enable')
        with self.assertRaises(ValueError):
            service.preview(self.config, 'unregister')
        self.apply('disable')
        with FileLock(self.config['state'] / 'runner.lock'):
            with self.assertRaises(ValueError):
                service.preview(self.config, 'unregister')


if __name__ == '__main__':
    unittest.main()
