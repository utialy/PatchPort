import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import connect, setup_project as setup
from agent_bridge.cli import main
from agent_bridge.storage import atomic_json


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.project = self.parent / 'project with spaces \uac80\uc0ac'
        self.project.mkdir()
        self.endpoints = {'mock': dict(adapter='command', command=[sys.executable], parallel=1)}

    def files(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file()}

    def starter_plan(self, **extra):
        return setup.plan_setup(self.project, endpoints=self.endpoints, starter=True, **extra)

    def test_starter_preview_apply_and_repeat_without_provider_or_queue(self):
        plan = self.starter_plan()
        self.assertEqual(self.files(), {})
        original_run = subprocess.run
        calls = []
        def checked_run(argv, **kwargs):
            calls.append(argv)
            self.assertIn('import agent_bridge', argv[2])
            return original_run(argv, **kwargs)
        with patch('agent_bridge.connect.subprocess.run', side_effect=checked_run):
            report = setup.apply_setup(plan)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report['connection'], 'READY')
        self.assertEqual(report['diagnostics']['authentication'], 'NOT_CHECKED')
        self.assertEqual(report['ai_calls'], 0)
        self.assertFalse((self.project / '.agent-bridge').exists())
        self.assertTrue((self.project / setup.STARTER).is_file())
        before = self.files()
        report = setup.apply_setup(setup.plan_setup(self.project))
        self.assertTrue(report['unchanged'])
        self.assertEqual(before, self.files())

    def test_new_directory_preview_is_absent_then_created(self):
        self.project = self.parent / 'new project'
        with self.assertRaises(ValueError): self.starter_plan()
        plan = self.starter_plan(create_project=True)
        self.assertFalse(self.project.exists())
        self.assertTrue(setup.apply_setup(plan)['ok'])
        self.assertTrue((self.project / 'bridge.json').exists())

    def test_existing_project_source_and_rules_are_preserved(self):
        (self.project / 'src').mkdir()
        (self.project / 'src/a.py').write_text('VALUE = 1\n')
        (self.project / 'AGENTS.md').write_text('Keep this rule.\n')
        before = self.files()
        plan = setup.plan_setup(self.project, endpoints=self.endpoints, include=['src', 'AGENTS.md'], writable=['src'])
        self.assertEqual(plan['report']['selected_rules'], ['AGENTS.md'])
        self.assertTrue(setup.apply_setup(plan)['ok'])
        for name, data in before.items(): self.assertEqual(self.files()[name], data)
        with self.assertRaises(ValueError): setup.plan_setup(self.project, include=['src'])

    def test_input_change_or_new_file_after_preview_refuses_all_writes(self):
        (self.project / 'src').mkdir()
        (self.project / 'src/a.py').write_text('old')
        plan = setup.plan_setup(self.project, endpoints=self.endpoints, include=['src'])
        (self.project / 'src/b.py').write_text('new')
        with self.assertRaisesRegex(ValueError, 'inputs changed'): setup.apply_setup(plan)
        self.assertFalse((self.project / 'bridge.json').exists())

    def test_sensitive_files_are_rejected_before_reading_contents(self):
        (self.project / 'src').mkdir()
        secret = self.project / 'src/.env'
        secret.write_text('SYNTHETIC_SECRET')
        with patch('agent_bridge.workspace.digest', side_effect=AssertionError('No content reads expected')):
            with self.assertRaisesRegex(ValueError, 'Sensitive path'):
                setup.plan_setup(self.project, endpoints=self.endpoints, include=['src'])
        self.assertFalse((self.project / 'bridge.json').exists())

    def test_inputs_changed_during_runtime_probe_are_rechecked(self):
        (self.project / 'source.py').write_text('old')
        plan = setup.plan_setup(self.project, endpoints=self.endpoints, include=['source.py'])
        def probe(*args, **kwargs):
            (self.project / 'source.py').write_text('changed')
            return subprocess.CompletedProcess(args[0], 0)
        with patch('agent_bridge.connect.subprocess.run', side_effect=probe):
            with self.assertRaisesRegex(ValueError, 'inputs changed'): setup.apply_setup(plan)
        self.assertFalse((self.project / 'bridge.json').exists())

    def test_missing_js_after_preview_is_not_reported_ready(self):
        script = self.parent / 'provider.js'
        script.write_text('must not run')
        self.endpoints['mock']['command'] = [sys.executable, str(script)]
        plan = self.starter_plan()
        script.unlink()
        with self.assertRaisesRegex(ValueError, 'CLI resolution changed'): setup.apply_setup(plan)
        self.assertEqual(self.files(), {})

    def test_install_asset_input_and_writable_escape_rejected(self):
        (self.project / 'src').mkdir()
        for includes in [['.'], ['../other'], ['.claude'], ['bridge.json']]:
            with self.subTest(includes=includes), self.assertRaises(ValueError):
                setup.plan_setup(self.project, endpoints=self.endpoints, include=includes)
        with self.assertRaisesRegex(ValueError, 'Writable'):
            setup.plan_setup(self.project, endpoints=self.endpoints, include=['src'], writable=['other'])

    def test_missing_cli_never_applies(self):
        self.endpoints['mock']['command'] = ['definitely-missing-bridge-provider']
        plan = self.starter_plan()
        report = setup.apply_setup(plan)
        self.assertFalse(report['ok'])
        self.assertEqual(report['connection'], 'NEEDS_CLI')
        self.assertEqual(self.files(), {})

    def test_conflict_does_not_create_starter_or_config(self):
        (self.project / 'bridge.py').write_text('user launcher')
        before = self.files()
        plan = self.starter_plan()
        self.assertEqual(setup.apply_setup(plan)['connection'], 'CONFLICT')
        self.assertEqual(self.files(), before)

    def test_config_and_manifest_changes_after_preview_refused(self):
        setup.apply_setup(self.starter_plan())
        for filename in ['bridge.json', connect.MANIFEST]:
            with self.subTest(filename=filename):
                plan = setup.plan_setup(self.project)
                path = self.project / filename
                old = path.read_bytes()
                path.write_bytes(old + b'\n')
                before = self.files()
                with self.assertRaisesRegex(ValueError, 'changed after preview'): setup.apply_setup(plan)
                self.assertEqual(before, self.files())
                path.write_bytes(old)

    def test_preexisting_identical_asset_is_not_claimed(self):
        plan = self.starter_plan()
        (self.project / 'bridge.py').write_bytes(plan['install']['contents']['bridge.py'])
        setup.apply_setup(self.starter_plan())
        manifest = json.loads((self.project / connect.MANIFEST).read_text())
        self.assertNotIn('bridge.py', manifest['files'])
        self.assertIn('role_flow.py', manifest['files'])

    def test_partial_failure_reports_evidence_and_does_not_retry(self):
        plan = self.starter_plan()
        real_replace = os.replace
        writes = []
        def replace(source, dest):
            writes.append(str(dest))
            if Path(dest).name == 'bridge.py': raise OSError('simulated disk failure')
            return real_replace(source, dest)
        with patch('agent_bridge.connect.os.replace', side_effect=replace):
            report = setup.apply_setup(plan)
        self.assertEqual(report['connection'], 'PARTIAL')
        self.assertFalse(report['ok'])
        self.assertTrue((Path(report['backup']) / 'error.json').is_file())
        self.assertEqual(sum(Path(p).name == 'bridge.py' for p in writes), 1)
        self.assertFalse((self.project / '.agent-bridge').exists())

    def test_interrupted_write_is_partial_not_cancelled(self):
        plan = self.starter_plan()
        real_replace = os.replace
        def replace(source, dest):
            if Path(dest).name == 'bridge.py': raise KeyboardInterrupt
            return real_replace(source, dest)
        with patch('agent_bridge.connect.os.replace', side_effect=replace):
            self.assertEqual(setup.apply_setup(plan)['connection'], 'PARTIAL')

    def test_new_destination_appears_after_preview(self):
        plan = self.starter_plan()
        (self.project / 'bridge.py').write_text('appeared')
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'Destination changed'): setup.apply_setup(plan)
        self.assertEqual(self.files(), before)

    def test_project_link_rejected_when_supported(self):
        link = self.parent / 'linked'
        try: link.symlink_to(self.project, target_is_directory=True)
        except OSError: self.skipTest('Directory symlink permission unavailable')
        with self.assertRaises(ValueError): setup.plan_setup(link, endpoints=self.endpoints, starter=True)

    def test_parent_alias_is_resolved_and_retargeted_plan_is_refused(self):
        original_resolve = Path.resolve
        alias = self.parent / 'parent-alias' / 'project'
        def resolve(path, *args, **kwargs):
            return self.project if path == alias else original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve), patch.object(Path, 'is_symlink', lambda p: p == alias.parent):
            self.assertEqual(connect.project_path(alias), self.project)
        plan = self.starter_plan()
        with patch.object(connect, 'project_path', return_value=self.parent / 'other'):
            with self.assertRaisesRegex(ValueError, 'location changed'): setup.apply_setup(plan)
        self.assertEqual(self.files(), {})

    def test_cli_preview_does_not_use_cwd_config_or_execute_provider(self):
        (self.parent / 'bridge.json').write_text('invalid unrelated config')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        result = subprocess.run([sys.executable, '-B', '-m', 'agent_bridge', 'setup',
                                 '--non-interactive', '--project', str(self.project), '--starter',
                                 '--provider', 'codex', '--codex', sys.executable],
                                cwd=self.parent, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['ai_calls'], 0)
        self.assertEqual(self.files(), {})

    def test_wizard_cancel_before_apply_preserves_project(self):
        answers = ['', 'n']
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['setup', '--project', str(self.project), '--starter', '--provider', 'codex', '--codex', sys.executable])
        self.assertEqual(code, 0)
        self.assertIn('CANCELLED', output.getvalue())
        self.assertEqual(self.files(), {})

    def test_wizard_applies_with_explicit_confirmation(self):
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['', 'y']), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['setup', '--project', str(self.project), '--starter', '--provider', 'codex', '--codex', sys.executable])
        self.assertEqual(code, 0)
        self.assertIn('READY', output.getvalue())
        self.assertEqual(json.loads((self.project / 'bridge.json').read_text())['endpoints']['codex']['sandbox'], 'read-only')

    def test_wizard_selects_existing_sources_and_hides_private_choices(self):
        (self.project / 'src').mkdir()
        (self.project / 'src/a.py').write_text('pass\n')
        (self.project / '.env').write_text('SYNTHETIC_SECRET')
        answers = ['src', '', 'n']
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['setup', '--project', str(self.project), '--provider', 'codex', '--codex', sys.executable])
        self.assertEqual(code, 0)
        self.assertIn('Top-level choices: src', output.getvalue())
        self.assertNotIn('SYNTHETIC_SECRET', output.getvalue())
        self.assertFalse((self.project / 'bridge.json').exists())

    def test_noninteractive_rejects_ambiguous_candidates(self):
        items = [dict(path=str(i), command=[sys.executable], status='FOUND') for i in range(2)]
        with patch.object(setup, 'discover', return_value=items), self.assertRaisesRegex(ValueError, 'found 2'):
            setup.choose_endpoint('codex', None, False)

    def test_discovery_finds_multiple_native_paths_without_execution(self):
        locations = [self.parent / 'one', self.parent / 'two']
        name = 'codex.exe' if os.name == 'nt' else 'codex'
        for location in locations:
            location.mkdir()
            path = location / name
            path.write_bytes(b'not executed')
            path.chmod(0o755)
        with patch('os.get_exec_path', return_value=[str(p) for p in locations]), patch('pathlib.Path.home', return_value=self.parent), patch.dict(os.environ, {'APPDATA': ''}), patch('subprocess.run', side_effect=AssertionError('No execution')):
            found = setup.discover('codex')
        self.assertEqual(sum(c['status'] == 'FOUND' for c in found), 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows shim behavior')
    def test_windows_shim_is_rejected_but_known_js_can_use_node(self):
        shim = self.parent / 'codex.cmd'
        shim.write_text('must not run')
        with self.assertRaisesRegex(ValueError, 'Shell shims'): setup.command_path(shim)
        script = self.parent / 'node_modules/@openai/codex/bin/codex.js'
        script.parent.mkdir(parents=True)
        script.write_text('must not run')
        with patch('os.get_exec_path', return_value=[str(self.parent)]), patch('pathlib.Path.home', return_value=self.parent), patch.dict(os.environ, {'APPDATA': ''}), patch('shutil.which', return_value=sys.executable):
            found = setup.discover('codex')
        self.assertEqual({c['status'] for c in found}, {'FOUND', 'UNSUPPORTED'})
        self.assertEqual(next(c for c in found if c['status'] == 'FOUND')['command'][1], str(script.resolve()))


if __name__ == '__main__':
    unittest.main()
