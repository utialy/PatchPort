import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import service_manager as service, runner_manager, connect
from agent_bridge.storage import atomic_json, FileLock, Store, guarded_removal


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        (self.project / 'input.txt').write_bytes(b'input')
        self.config_path = self.project / 'bridge.json'
        atomic_json(self.config_path, dict(project='.', include=['input.txt'], writable=['input.txt'],
            parallel=1, timeout=10, endpoints={'mock': dict(adapter='command', command=[sys.executable])}))
        self.config = runner_manager.checked_config(self.config_path)
        self.units = self.root / 'units'
        self.manager = dict(LoadState='not-found', ActiveState='inactive', SubState='dead', UnitFileState='',
                            FragmentPath='', DropInPaths='', NeedDaemonReload='no', MainPID='0')
        self.calls = []
        if os.name == 'nt':
            # Exercise ownership transitions on Windows with its native deletion
            # primitive; the actual Linux path is exercised in WSL.
            def remove(path, expected, root):
                with guarded_removal(root, [(path, expected)]) as perform:
                    list(perform())
            native = patch.object(service, 'remove_owned_launcher', side_effect=remove)
            native.start()
            self.addCleanup(native.stop)
        for item in [patch.object(service.sys, 'platform', 'linux'),
                     patch.object(service.os, 'getuid', return_value=1000, create=True),
                     patch.object(service, 'unit_directory', return_value=self.units),
                     patch.object(service, 'manager_state', side_effect=lambda name: dict(self.manager)),
                     patch.object(service.child, 'verify_service_runtime'),
                     patch.object(service.child, 'systemd_user', side_effect=self.command)]:
            item.start()
            self.addCleanup(item.stop)

    def command(self, args, **kwargs):
        self.calls.append(args)
        name = service.unit_name(self.config, 1000)
        if args == ['daemon-reload']:
            exists = (self.units / name).exists()
            self.manager.update(LoadState='loaded' if exists else 'not-found',
                FragmentPath=str(self.units / name) if exists else '',
                UnitFileState='disabled' if exists else '', NeedDaemonReload='no')
        elif args[0] in ('enable', 'disable'):
            self.manager['UnitFileState'] = 'enabled' if args[0] == 'enable' else 'disabled'
        elif args[0] == 'start':
            self.manager['ActiveState'] = 'activating'
        return 0, ''

    def apply(self, action):
        plan = service.preview(self.config, action)
        return service.apply(self.config, action, plan['plan_token'])

    def test_registration_preview_and_apply_never_start_or_create_queue(self):
        plan = service.preview(self.config, 'register')
        self.assertFalse(self.config['state'].exists())
        self.assertFalse(self.units.exists())
        self.assertFalse(plan['starts_now'])
        result = service.apply(self.config, 'register', plan['plan_token'])
        self.assertEqual(result['outcome'], 'REGISTERED')
        self.assertFalse(result['automatic_start'])
        self.assertEqual(self.calls, [['daemon-reload']])
        self.assertFalse((self.config['state'] / 'queue.sqlite3').exists())
        self.assertEqual(self.apply('register')['outcome'], 'UNCHANGED')

    def test_plan_token_mismatch_and_relative_provider_refuse_writes(self):
        with self.assertRaises(ValueError):
            service.apply(self.config, 'register', 'wrong')
        self.assertFalse(self.config['state'].exists())
        self.config['endpoints']['mock']['command'] = ['mock']
        with self.assertRaises(ValueError):
            service.preview(self.config, 'register')

    def test_enable_disable_and_unregister_are_separate_and_preserve_data(self):
        self.apply('register')
        store = Store(self.config['state'])
        store.set_limit(3)
        before = store.db.read_bytes()
        enabled = self.apply('enable')
        self.assertTrue(enabled['automatic_start'])
        self.assertFalse(enabled['starts_now'])
        with self.assertRaises(ValueError):
            service.preview(self.config, 'unregister')
        self.assertFalse(self.apply('disable')['automatic_start'])
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')
        self.assertIsNone(service.registration(self.config))
        self.assertEqual(store.db.read_bytes(), before)
        self.assertFalse(any(c[0] == 'start' for c in self.calls))

    def test_active_manual_runner_and_unknown_launch_block_registration(self):
        with FileLock(self.config['state'] / 'runner.lock'):
            with self.assertRaises(ValueError):
                service.preview(self.config, 'register')
        atomic_json(self.config['state'] / 'runner-launch.json', dict(run_id='unconfirmed', outcome='PENDING'))
        with self.assertRaises(ValueError):
            service.preview(self.config, 'register')

    def test_foreign_unit_and_modified_owned_unit_are_preserved(self):
        self.units.mkdir()
        path = self.units / service.unit_name(self.config, 1000)
        path.write_bytes(b'foreign unit')
        with self.assertRaises(ValueError):
            service.preview(self.config, 'register')
        self.assertEqual(path.read_bytes(), b'foreign unit')
        path.unlink()
        self.apply('register')
        path.write_bytes(b'user edits')
        for action in ('enable', 'disable', 'unregister'):
            with self.assertRaises(ValueError):
                service.preview(self.config, action)
        self.assertEqual(path.read_bytes(), b'user edits')

    def test_preexisting_automatic_start_without_unit_blocks_registration(self):
        self.manager['UnitFileState'] = 'enabled'
        with self.assertRaises(ValueError):
            service.preview(self.config, 'register')
        self.assertFalse(self.config['state'].exists())

    def test_dropins_and_stale_loaded_definition_block_start(self):
        self.apply('register')
        self.manager['DropInPaths'] = '/some/override.conf'
        with self.assertRaises(ValueError):
            service.start(self.config)
        self.manager['DropInPaths'] = ''
        self.manager['NeedDaemonReload'] = 'yes'
        with self.assertRaises(ValueError):
            service.start(self.config)

    def test_partial_register_blocks_manual_and_can_be_explicitly_unregistered(self):
        plan = service.preview(self.config, 'register')
        with patch.object(service.child, 'systemd_user', side_effect=RuntimeError('Injected reload failure')):
            result = service.apply(self.config, 'register', plan['plan_token'])
        self.assertEqual(result['outcome'], 'PARTIAL')
        with self.assertRaises(ValueError):
            service.guard_execution(self.config, 'manual', None)
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')

    def test_partial_unregistration_after_unlink_can_resume(self):
        self.apply('register')
        plan = service.preview(self.config, 'unregister')
        with patch.object(service.child, 'systemd_user', side_effect=RuntimeError('Injected reload failure')):
            with self.assertRaises(RuntimeError):
                service.apply(self.config, 'unregister', plan['plan_token'])
        self.assertEqual(service.registration(self.config)['state'], 'REMOVING')
        self.manager['NeedDaemonReload'] = 'yes'
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')

    def test_bad_setting_partial_registration_can_be_removed_without_starting(self):
        self.apply('register')
        record = service.registration(self.config)
        record['state'] = 'PARTIAL'
        atomic_json(self.config['state'] / service.RECORD, record)
        self.manager['LoadState'] = 'bad-setting'
        self.assertEqual(self.apply('unregister')['outcome'], 'UNREGISTERED')
        self.assertFalse(any(c[0] == 'start' for c in self.calls))

    def test_runner_controls_route_to_service_and_foreground_is_rejected(self):
        self.apply('register')
        with patch.object(service, 'start', return_value=({'outcome': 'SERVICE'}, 0)) as started:
            self.assertEqual(runner_manager.start(self.config)[0]['outcome'], 'SERVICE')
            started.assert_called_once()
        with patch.object(service, 'stop', return_value=({'outcome': 'SERVICE'}, 0)) as stopped:
            runner_manager.stop(self.config, 'run-id')
            stopped.assert_called_once()
        with self.assertRaises(ValueError):
            service.guard_execution(self.config, 'manual', None)
        self.assertEqual(runner_manager.inspect(self.config)['backend'], 'systemd-user')

    def test_internal_entry_requires_real_service_process_identity(self):
        self.apply('register')
        record = service.registration(self.config)
        with patch.dict(os.environ, {'INVOCATION_ID': ''}):
            with self.assertRaises(ValueError):
                service.guard_execution(self.config, service.BACKEND, record['id'], running=True)
        with patch.dict(os.environ, {'INVOCATION_ID': 'a' * 32}):
            self.manager['MainPID'] = str(os.getpid() + 1)
            with self.assertRaises(ValueError):
                service.guard_execution(self.config, service.BACKEND, record['id'], running=True)
            self.manager['MainPID'] = str(os.getpid())
            service.guard_execution(self.config, service.BACKEND, record['id'], running=True)

    def test_service_start_timeout_is_not_automatically_retried(self):
        self.apply('register')
        result, code = service.start(self.config, timeout=.001)
        self.assertEqual((result['outcome'], code), ('START_UNCONFIRMED', 2))
        with self.assertRaises(ValueError):
            service.start(self.config)
        self.assertEqual(sum(c[0] == 'start' for c in self.calls), 1)

    def test_changed_config_blocks_service_and_reconnection_requires_unregister(self):
        connect.install(connect.plan_install(self.project), True)
        self.apply('register')
        manifest = self.project / connect.MANIFEST
        value = json.loads(manifest.read_text())
        value['python'] = str(self.project / 'old-python')
        atomic_json(manifest, value)
        with self.assertRaisesRegex(ValueError, 'unregister'):
            connect.install(connect.plan_install(self.project), True)
        raw = json.loads(self.config_path.read_text())
        raw['timeout'] = 20
        atomic_json(self.config_path, raw)
        current = runner_manager.checked_config(self.config_path)
        record = service.registration(current)
        with self.assertRaises(ValueError):
            service.guard_execution(current, service.BACKEND, record['id'])

    def test_unit_escaping_and_drain_policy(self):
        identity = dict(python='/path with % and $/python', config='/project "quoted"/config.json', root='/root % $')
        text = service.render(identity, 'a' * 64).decode()
        self.assertIn('%% and $$', text)
        self.assertIn('\\"quoted\\"', text)
        for value in ('Type=exec', 'Restart=no', 'KillMode=mixed', 'TimeoutStopSec=30'):
            self.assertIn(value, text)
        self.assertNotIn('runner start', text)
        with self.assertRaises(ValueError):
            service.quote('/bad\npath')

    def test_corrupt_marker_never_falls_back_to_manual(self):
        atomic_json(self.config['state'] / service.RECORD, {})
        with self.assertRaises(ValueError):
            runner_manager.start(self.config)
        with self.assertRaises(ValueError):
            service.guard_execution(self.config, 'manual', None)


if __name__ == '__main__':
    unittest.main()
