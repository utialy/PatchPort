import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agent_bridge import posix_install as installer, runner_manager
from agent_bridge.storage import atomic_json, canonical_selected_path, FileLock


class PosixDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.wheel = self.home / 'local_agent_bridge-0.1.0a2-py3-none-any.whl'
        self.make_wheel()

    def make_wheel(self, extra=None, metadata=None):
        entries = {'agent_bridge/management.py': b'PROTOCOL = 1\n',
                   'local_agent_bridge-0.1.0a2.dist-info/METADATA': metadata or
                   b'Metadata-Version: 2.1\nName: local-agent-bridge\nVersion: 0.1.0a2\n'}
        entries.update(extra or {})
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
        self.sha = hashlib.sha256(self.wheel.read_bytes()).hexdigest()

    def test_checksum_precedes_package_use(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            installer.wheel_info(self.wheel, '0' * 64)
        self.assertEqual(installer.wheel_info(self.wheel, self.sha)['version'], '0.1.0a2')

    def test_missing_venv_prerequisites_explain_failure_before_writes(self):
        target = self.home / 'install'
        with patch.object(installer, 'run', side_effect=RuntimeError('Probe failed')):
            with self.assertRaisesRegex(ValueError, 'venv and ensurepip'):
                installer.preview_install(self.wheel, self.sha, target, 'test-build')
        self.assertFalse(target.exists())

    def test_dependency_and_startup_hooks_are_rejected(self):
        self.make_wheel(metadata=b'Name: local-agent-bridge\nVersion: 1\nRequires-Dist: external-package\n')
        with self.assertRaises(ValueError):
            installer.wheel_info(self.wheel, self.sha)
        self.make_wheel(extra={'agent_bridge/startup.pth': b'import os'})
        with self.assertRaises(ValueError):
            installer.wheel_info(self.wheel, self.sha)

    def test_traversal_case_collision_and_unexpected_package_are_rejected(self):
        for name in ('../escape', 'agent_bridge/../escape', 'agent_bridge/MANAGEMENT.py', 'other/package.py'):
            self.make_wheel(extra={name: b'bad'})
            with self.subTest(name=name), self.assertRaises(ValueError):
                installer.wheel_info(self.wheel, self.sha)

    def test_preview_wrong_token_and_changed_wheel_never_create_root(self):
        target = self.home / 'install'
        with patch.object(installer, 'python_identity', return_value={'path': sys.executable, 'version': [3, 14, 4]}):
            plan = installer.preview_install(self.wheel, self.sha, target, 'test-build')
            self.assertFalse(target.exists())
            with self.assertRaises(ValueError):
                installer.apply(plan, 'wrong-token')
            plan = installer.preview_install(self.wheel, self.sha, target, 'test-build')
            self.wheel.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                installer.apply(plan, plan.token)
        self.assertFalse(target.exists())

    def test_changed_target_and_consumed_plan_are_rejected(self):
        target = self.home / 'install'
        with patch.object(installer, 'python_identity', return_value={'path': sys.executable}):
            plan = installer.preview_install(self.wheel, self.sha, target, 'test-build')
            target.mkdir()
            (target / 'user-data').write_bytes(b'keep')
            with self.assertRaises(ValueError):
                installer.apply(plan, plan.token)
            with self.assertRaises(ValueError):
                installer.apply(plan, plan.token)
        self.assertEqual((target / 'user-data').read_bytes(), b'keep')


@unittest.skipUnless(os.name == 'posix', 'POSIX path and launcher behavior')
class PosixOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'owned install'
        self.root.mkdir()
        self.key = 'test-' + 'a' * 12
        self.version = self.root / 'versions' / self.key
        (self.version / 'venv').mkdir(parents=True)
        (self.version / 'venv/keep-runtime').write_bytes(b'preserved')
        self.launcher = self.root / 'bin' / ('patchport-' + self.key)
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(b'#!/bin/sh\nexit 0\n')
        atomic_json(self.root / installer.MARKER,
                    dict(schema=1, product='PatchPort', platform=sys.platform, root=str(self.root)))
        self.record = dict(schema=1, product='PatchPort', platform=sys.platform, key=self.key, build='test',
                           directory=str(self.version), wheel_sha256='a' * 64, state='READY',
                           launcher=str(self.launcher.relative_to(self.root)),
                           launcher_sha256=hashlib.sha256(self.launcher.read_bytes()).hexdigest())
        atomic_json(self.version / installer.RECEIPT, self.record)

    def test_remove_only_launcher_and_repeat_without_removing_runtime(self):
        plan = installer.preview_remove(self.root, self.key)
        self.assertTrue(self.launcher.exists())
        self.assertEqual(installer.apply(plan, plan.token)['outcome'], 'LAUNCHER_REMOVED')
        self.assertEqual((self.version / 'venv/keep-runtime').read_bytes(), b'preserved')
        self.assertFalse(self.launcher.exists())
        plan = installer.preview_remove(self.root, self.key)
        self.assertEqual(installer.apply(plan, plan.token)['outcome'], 'UNCHANGED')

    def test_modified_launcher_and_concurrent_operation_block_removal(self):
        plan = installer.preview_remove(self.root, self.key)
        with FileLock(self.root / '.install.lock'):
            with self.assertRaises(RuntimeError):
                installer.apply(plan, plan.token)
        self.assertTrue(self.launcher.exists())
        self.launcher.write_bytes(b'user edits')
        with self.assertRaises(ValueError):
            installer.preview_remove(self.root, self.key)

    def test_partial_removal_resumes_only_from_new_preview(self):
        plan = installer.preview_remove(self.root, self.key)
        with patch.object(installer, 'remove_owned_launcher', side_effect=OSError('Injected failure')):
            with self.assertRaises(OSError):
                installer.apply(plan, plan.token)
        self.assertEqual(installer.read_receipt(self.version)['state'], 'REMOVING')
        plan = installer.preview_remove(self.root, self.key)
        self.assertEqual(installer.apply(plan, plan.token)['outcome'], 'LAUNCHER_REMOVED')

    def test_resume_after_unlink_finalizes_receipt_without_touching_runtime(self):
        self.record['state'] = 'REMOVING'
        atomic_json(self.version / installer.RECEIPT, self.record)
        self.launcher.unlink()
        plan = installer.preview_remove(self.root, self.key)
        self.assertEqual(plan.report['outcome'], 'PREVIEW')
        self.assertEqual(installer.apply(plan, plan.token)['outcome'], 'LAUNCHER_REMOVED')
        self.assertEqual(installer.read_receipt(self.version)['state'], 'LAUNCHER_REMOVED')
        self.assertEqual((self.version / 'venv/keep-runtime').read_bytes(), b'preserved')

    def test_moved_installation_and_external_venv_links_are_rejected(self):
        moved = self.home / 'moved'
        self.root.rename(moved)
        with self.assertRaises(ValueError):
            installer.root_state(moved)
        venv = moved / 'versions' / self.key / 'venv'
        (venv / 'outside').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.inventory(venv)

    def test_parent_alias_is_frozen_but_selected_directory_link_is_rejected(self):
        alias = self.home / 'parent-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(canonical_selected_path(alias / 'versions'), self.root / 'versions')
        with self.assertRaises(ValueError):
            canonical_selected_path(alias)

    def test_management_accepts_parent_alias_but_rejects_state_link(self):
        project = self.root / 'project'
        project.mkdir()
        (project / 'input.txt').write_bytes(b'input')
        atomic_json(project / 'bridge.json', dict(project='.', state='.agent-bridge', include=['input.txt'],
            writable=['input.txt'], parallel=1, timeout=10,
            endpoints={'mock': dict(adapter='command', command=[sys.executable], parallel=1)}))
        alias = self.home / 'parent-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        config = runner_manager.checked_config(alias / 'project/bridge.json')
        self.assertEqual(Path(config['config_path']), project / 'bridge.json')
        (project / 'real-state').mkdir()
        (project / '.agent-bridge').symlink_to(project / 'real-state', target_is_directory=True)
        with self.assertRaises(ValueError):
            runner_manager.checked_config(project / 'bridge.json')


if __name__ == '__main__':
    unittest.main()
