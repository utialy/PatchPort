import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agent_bridge import connect, install_manager as install
from agent_bridge.storage import FileLock, atomic_json, checked_local_path


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / 'install spaces \ud55c\uae00'
        self.package = self.make_package('one')

    def make_package(self, version, extra=None):
        files = {'core/python.exe': b'fake runtime', 'core/Lib/module.py': b'pass\n',
                 'gui/PatchPort.exe': b'fake GUI', 'gui/_internal/library.dll': b'fake DLL',
                 'notices/PYTHON-LICENSE.txt': b'Test notice',
                 'package.json': json.dumps(dict(schema=1, core_python='core/python.exe', gui='gui/PatchPort.exe')).encode()}
        files.update(extra or {})
        table = {name: dict(size=len(data), sha256=hashlib.sha256(data).hexdigest()) for name, data in files.items()}
        path = self.home / (version + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr('install-manifest.json', json.dumps(dict(schema=1, product='PatchPort', version=version, files=table)))
        return path

    def apply(self, plan):
        return install.apply(plan, verify=lambda python: None)

    def installed(self):
        plan = install.preview_install(self.package, self.root, shortcut=False)
        self.assertEqual(self.apply(plan)['outcome'], 'INSTALLED')
        return plan.version

    def test_preview_cancel_and_repeated_install_are_non_destructive(self):
        plan = install.preview_install(self.package, self.root, shortcut=False)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.apply(plan)['outcome'], 'INSTALLED')
        before = snapshot(self.root)
        second = install.preview_install(self.package, self.root, shortcut=False)
        self.assertEqual(self.apply(second)['outcome'], 'UNCHANGED')
        self.assertEqual(before, snapshot(self.root))
        with self.assertRaises(ValueError):
            self.apply(plan)

    def test_expired_plan_does_not_create_root(self):
        plan = install.preview_install(self.package, self.root, shortcut=False)
        plan.deadline = 0
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertFalse(self.root.exists())

    def test_payload_changed_after_preview_does_not_create_root(self):
        plan = install.preview_install(self.package, self.root, shortcut=False)
        self.package.write_bytes(b'bad archive')
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            self.apply(plan)
        self.assertFalse(self.root.exists())

    def test_populated_unowned_root_and_destination_change_are_rejected(self):
        plan = install.preview_install(self.package, self.root, shortcut=False)
        self.root.mkdir()
        (self.root / 'user.txt').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.apply(plan)
        with self.assertRaises(ValueError):
            install.preview_install(self.package, self.root, shortcut=False)
        self.assertEqual(snapshot(self.root), {'user.txt': b'keep'})

    def test_upgrade_is_side_by_side_and_does_not_touch_projects(self):
        old = self.installed()
        before = snapshot(old)
        project = self.home / 'project'
        project.mkdir()
        (project / 'answer.txt').write_bytes(b'keep answer')
        package = self.make_package('two', {'gui/PatchPort.exe': b'new GUI'})
        plan = install.preview_install(package, self.root, shortcut=False)
        self.assertEqual(self.apply(plan)['outcome'], 'INSTALLED')
        self.assertNotEqual(old, plan.version)
        self.assertEqual(snapshot(old), before)
        self.assertEqual(snapshot(project), {'answer.txt': b'keep answer'})

    def test_modified_file_blocks_reinstall_and_removal(self):
        version = self.installed()
        (version / 'gui/PatchPort.exe').write_bytes(b'user change')
        for action in (lambda: install.preview_install(self.package, self.root, shortcut=False),
                       lambda: install.preview_remove(self.root, version.name)):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual((version / 'gui/PatchPort.exe').read_bytes(), b'user change')

    def test_failed_verification_is_recorded_and_never_reused(self):
        plan = install.preview_install(self.package, self.root, shortcut=False)
        def fail(python):
            raise RuntimeError('Injected probe failure')
        result = install.apply(plan, verify=fail)
        self.assertEqual(result['outcome'], 'PARTIAL')
        self.assertEqual(install.receipt(plan.version)['state'], 'INSTALL_FAILED')
        with self.assertRaises(ValueError):
            install.preview_install(self.package, self.root, shortcut=False)
        self.assertTrue((plan.version / 'core/python.exe').exists())

    def test_archive_paths_case_collisions_and_symlinks_are_rejected(self):
        bad = ['../escape', 'gui/../escape', 'gui/NUL.txt', 'gui/x:stream', 'gui/trailing.',
               'GUI/PatchPort.exe', 'gui\\escape']
        for index, name in enumerate(bad):
            path = self.make_package('bad' + str(index), {name: b'data'})
            with self.subTest(name=name), self.assertRaises(ValueError):
                install.Package(path)
        path = self.make_package('link')
        with zipfile.ZipFile(path, 'a') as archive:
            info = zipfile.ZipInfo('gui/link')
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b'elsewhere')
        with self.assertRaises(ValueError):
            install.Package(path)

    def test_hardlink_install_paths_are_rejected(self):
        original = self.home / 'file'
        original.write_bytes(b'private')
        alias = self.home / 'alias'
        try:
            os.link(original, alias)
        except OSError:
            self.skipTest('Hardlinks unavailable')
        with self.assertRaises(ValueError):
            checked_local_path(alias)

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive deletion')
    def test_remove_only_owned_gui_and_keep_core_unknown_files_and_projects(self):
        version = self.installed()
        (version / 'gui/user.txt').write_bytes(b'user data')
        core_before = snapshot(version / 'core')
        plan = install.preview_remove(self.root, version.name)
        self.assertTrue((version / 'gui/PatchPort.exe').exists())
        self.assertEqual(self.apply(plan)['outcome'], 'GUI_REMOVED')
        self.assertFalse((version / 'gui/PatchPort.exe').exists())
        self.assertEqual(snapshot(version / 'core'), core_before)
        self.assertEqual((version / 'gui/user.txt').read_bytes(), b'user data')
        self.assertEqual(self.apply(install.preview_remove(self.root, version.name))['outcome'], 'UNCHANGED')

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive deletion')
    def test_open_file_blocks_all_deletion_before_journal_change(self):
        version = self.installed()
        plan = install.preview_remove(self.root, version.name)
        before = snapshot(self.root)
        with (version / 'gui/_internal/library.dll').open('rb'):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertEqual(before, snapshot(self.root))

    @unittest.skipUnless(os.name == 'nt', 'Windows exclusive deletion')
    def test_partial_removal_is_recorded_and_requires_new_preview(self):
        version = self.installed()
        @contextlib.contextmanager
        def partial(root, entries):
            def remove():
                path = entries[0][0]
                path.unlink()
                yield path
                raise OSError('Injected removal failure')
            yield remove
        with patch.object(install, 'guarded_removal', partial):
            result = self.apply(install.preview_remove(self.root, version.name))
        self.assertEqual(result['outcome'], 'PARTIAL')
        self.assertEqual(install.receipt(version)['state'], 'REMOVE_FAILED')
        self.assertEqual(self.apply(install.preview_remove(self.root, version.name))['outcome'], 'GUI_REMOVED')
        self.assertTrue((version / 'core/python.exe').exists())


class ReconnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'input.txt').write_text('input', encoding='utf-8')
        atomic_json(self.project / 'bridge.json', dict(project='.', state='.agent-bridge',
            include=['input.txt'], writable=['input.txt'], parallel=1, timeout=10,
            endpoints={'mock': dict(adapter='command', command=[sys.executable], parallel=1)}))
        connect.install(connect.plan_install(self.project), True)
        manifest = json.loads((self.project / connect.MANIFEST).read_text(encoding='utf-8'))
        manifest['python'] = str(self.project / 'previous-python.exe')
        atomic_json(self.project / connect.MANIFEST, manifest)

    def test_active_runner_blocks_reconnection_without_changing_manifest(self):
        plan = connect.plan_install(self.project)
        before = (self.project / connect.MANIFEST).read_bytes()
        with FileLock(self.project / '.agent-bridge/runner.lock'):
            with self.assertRaises(RuntimeError):
                connect.install(plan, True)
        self.assertEqual((self.project / connect.MANIFEST).read_bytes(), before)

    def test_pending_start_blocks_reconnection(self):
        atomic_json(self.project / '.agent-bridge/runner-launch.json', dict(run_id='unknown', outcome='PENDING'))
        plan = connect.plan_install(self.project)
        with self.assertRaisesRegex(ValueError, 'unknown'):
            connect.install(plan, True)

    def test_stopped_reconnection_preserves_existing_queue_bytes(self):
        from agent_bridge.storage import Store
        store = Store(self.project / '.agent-bridge')
        store.set_limit(4)
        store.pause(True)
        before = store.db.read_bytes()
        result = connect.install(connect.plan_install(self.project), True)
        self.assertTrue(result['ok'])
        self.assertEqual(before, store.db.read_bytes())
        self.assertEqual(json.loads((self.project / connect.MANIFEST).read_text(encoding='utf-8'))['python'], str(Path(sys.executable).resolve()))


if __name__ == '__main__':
    unittest.main()
