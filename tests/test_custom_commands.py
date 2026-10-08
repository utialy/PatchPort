"""Custom CLI selection and isolated profile shell execution, without AI calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import child, management, setup_project as setup
from agent_bridge.cli import main
from agent_bridge.runner import argv_for, run
from agent_bridge.storage import Store, atomic_json, load_config
from desktop.model import response_text, status_text


class CommandFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'input.txt').write_text('Input', encoding='utf-8')

    def make_config(self, endpoint, timeout=10):
        config = self.project / 'bridge.json'
        atomic_json(config, dict(project='.', include=['input.txt'], writable=['input.txt'],
                                 parallel=1, timeout=timeout, endpoints={'custom': endpoint}))
        return config


class CustomCommandTests(CommandFixture):

    def test_path_names_select_distinct_commands_without_execution(self):
        for name in ('claude-1', 'claude-2'):
            executable = self.root / (name + '.exe' if os.name == 'nt' else name)
            executable.write_text('not executed')
            executable.chmod(0o755)
        with patch.dict(os.environ, PATH=str(self.root)), patch('subprocess.run', side_effect=AssertionError('No calls')):
            for name in ('claude-1', 'claude-2'):
                endpoint = setup.choose_endpoint('claude', name, False)
                self.assertTrue(Path(endpoint['command'][0]).is_absolute())
                self.assertEqual(Path(endpoint['command'][0]).stem, name)
                self.assertEqual(endpoint['adapter'], 'claude')
                self.assertIn('--output-format', argv_for(endpoint, self.root / 'reply'))
            with self.assertRaises(ValueError):
                setup.choose_endpoint('claude', 'claude-missing', False)

    @unittest.skipUnless(os.name == 'nt', 'Windows shim policy')
    def test_direct_custom_shim_still_rejected(self):
        (self.root / 'claude-1.cmd').write_text('exit /b 0')
        with patch.dict(os.environ, PATH=str(self.root)), self.assertRaisesRegex(ValueError, 'Shell shims'):
            setup.choose_endpoint('claude', 'claude-1', False)

    def test_profile_preview_never_loads_profile_or_invokes_command(self):
        profile = self.root / 'profile.ps1'
        profile.write_text('throw "Must not run"')
        launch = dict(shell='powershell', executable=sys.executable, profile=str(profile))
        with patch('subprocess.run', side_effect=AssertionError('No calls')):
            endpoint = setup.choose_endpoint('claude', 'claude-1', False, launch)
            plan = setup.plan_setup(self.project, endpoints={'claude': endpoint}, include=['input.txt'])
        check = plan['report']['diagnostics']['commands']['claude']
        self.assertEqual(check['command_status'], 'NOT_CHECKED')
        self.assertEqual(check['profile_status'], 'NOT_LOADED')
        self.assertFalse((self.project / 'bridge.json').exists())
        preview = response_text(dict(ok=True, result=plan['report']))
        self.assertIn('claude-1', preview)
        self.assertIn('not loaded', preview)
        self.assertIn('NOT CHECKED', status_text(dict(diagnostics=plan['report']['diagnostics'])))
        profile.unlink()
        with self.assertRaisesRegex(ValueError, 'CLI resolution changed'):
            setup.apply_setup(plan)

    def test_cli_and_management_accept_profile_selection(self):
        profile = self.root / 'profile.ps1'
        profile.write_text('throw "Must not run"')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(['setup', '--non-interactive', '--project', str(self.project),
                         '--claude', 'claude-2', '--claude-shell', 'powershell',
                         '--claude-shell-executable', sys.executable,
                         '--claude-profile', str(profile), '--include', 'input.txt'])
        self.assertEqual(code, 0, output.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(report['diagnostics']['commands']['claude']['command'][0], 'claude-2')
        session = management.Session()
        reply = session.handle(dict(protocol=1, id='custom', action='setup.preview', args=dict(
            project=str(self.project), include=['input.txt'], providers={'claude': {
                'command': 'claude-1', 'launch': dict(shell='powershell', executable=sys.executable, profile=str(profile))}})))
        self.assertTrue(reply['ok'], reply)
        for value in ('relative/claude', {'command': 'claude-1', 'launch': {'shell': 'sh'}},
                      {'command': 'claude-1', 'launch': {'shell': 'bash', 'profile': 'relative'}}):
            with self.subTest(value=value), self.assertRaises(management.Problem):
                management.validate_args('setup.preview', dict(project=str(self.project), providers={'claude': value}))
        management.validate_args('setup.preview', dict(project=str(self.project), providers={'claude': 'claude-1'}))

    def test_invalid_launch_is_rejected_by_config(self):
        for launch in ({}, {'shell': 'unknown'}, {'shell': 'bash', 'profile': 'relative'},
                       {'shell': 'pwsh', 'executable': ''}, {'shell': 'bash', 'extra': True}):
            config = self.make_config(dict(adapter='claude', command=['claude-1'], launch=launch))
            with self.subTest(launch=launch), self.assertRaises(ValueError):
                load_config(config)

@unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'), 'PowerShell is unavailable')
class PowerShellProfileTests(CommandFixture):
    def setUp(self):
        super().setUp()
        self.shell = shutil.which('powershell') or shutil.which('pwsh')
        self.profile = self.root / "profile with spaces ' \uac80\uc0ac.ps1"
        self.profile.write_text('''function claude-1 {
    $prompt = [Console]::In.ReadToEnd()
    $payload = @{prompt=$prompt; arguments=@($args)} | ConvertTo-Json -Compress
    $event = @{type='result'; is_error=$false; result=$payload} | ConvertTo-Json -Compress
    [Console]::Out.WriteLine($event)
}
Set-Alias claude-2 claude-1
function fail-command { throw 'expected failure' }
function slow-command { Start-Sleep -Seconds 30 }
Set-Location $env:TEMP
''', encoding='utf-8-sig')
        self.launch = dict(shell='powershell', executable=self.shell, profile=str(self.profile))

    def test_real_profile_function_and_alias_keep_stdin_and_literal_arguments(self):
        values = ['\ud55c\uae00', "a'b", '$(throw "injection")', '; exit 7', 'a b', '"quoted"', '', 'C:\\path\\']
        for command in ('claude-1', 'claude-2'):
            endpoint = dict(adapter='claude', command=[command, *values], launch=self.launch)
            result = subprocess.run(argv_for(endpoint, self.root / 'reply'), input='\uc9c8\ubb38\nsecond line'.encode('utf-8'),
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(json.loads(result.stdout)['result'])
            self.assertEqual(payload['prompt'], '\uc9c8\ubb38\nsecond line')
            self.assertEqual(payload['arguments'], values + ['-p', '--output-format', 'stream-json', '--verbose'])

    def test_profile_function_forwards_stdin_to_native_process(self):
        mock = self.root / 'native mock.py'
        mock.write_text('import sys,json; print(json.dumps(dict(prompt=sys.stdin.read(), args=sys.argv[1:]), ensure_ascii=False))', encoding='utf-8')
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        with self.profile.open('a', encoding='utf-8') as stream:
            stream.write('\nfunction native-command { & ' + quote(sys.executable) + ' -X utf8 '
                         + quote(mock) + ' @args }\n')
        result = subprocess.run(child.profile_argv(['native-command', '\ud55c\uae00', 'a b'], self.launch),
                                input='\uc9c8\ubb38\n'.encode('utf-8'), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(prompt='\uc9c8\ubb38\n', args=['\ud55c\uae00', 'a b']))

    def test_real_missing_and_failing_commands_do_not_fallback(self):
        for command in ('missing-bridge-command', 'fail-command'):
            result = subprocess.run(argv_for(dict(adapter='claude', command=[command], launch=self.launch), self.root / 'reply'),
                                    input='', encoding='utf-8', capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr)

    def test_runner_profile_output_and_timeout(self):
        for command, expected, timeout in (('claude-2', 'DONE', 10), ('slow-command', 'TIMEOUT', 1)):
            endpoint = dict(adapter='claude', command=[command], launch=self.launch)
            config = load_config(self.make_config(endpoint, timeout))
            store = Store(config['state'])
            store.submit(command, ['custom'], '\uc9c8\ubb38', config['endpoints'])
            with contextlib.redirect_stdout(io.StringIO()):
                run(config, True)
            row = next(r for r in store.rows() if r['id'] == command + '--custom')
            self.assertEqual(row['state'], expected, row)
            if command == 'claude-2':
                self.assertIsNotNone(json.loads(row['result'])['provider_result'])

    def test_profile_directory_change_is_reset_to_selected_copy(self):
        with self.profile.open('a', encoding='utf-8') as stream:
            stream.write('\nfunction where-command { [Console]::Out.WriteLine((Get-Location).Path) }\n')
        argv = child.profile_argv(['where-command'], self.launch, cwd=self.project)
        result = subprocess.run(argv, input=b'', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.decode('utf-8').strip()), self.project)


@unittest.skipUnless(os.name != 'nt' and shutil.which('bash'), 'Native POSIX bash is unavailable')
class BashProfileTests(CommandFixture):
    def test_real_bash_profile_function_alias_and_quoted_arguments(self):
        profile = self.root / "profile ' with spaces"
        mock = self.root / 'mock.py'
        mock.write_text('import json,sys; print(json.dumps(dict(args=sys.argv[1:],prompt=sys.stdin.read())))')
        import shlex
        profile.write_text('claude-1() { ' + shlex.join([sys.executable, str(mock)])
                           + ' "$@"; }\nalias claude-2=claude-1\ncd /\n')
        values = ['\ud55c\uae00', "a'b", '$(touch injected)', '; exit 7', '', 'a b']
        for name in ('claude-1', 'claude-2'):
            argv = child.profile_argv([name, *values], dict(shell='bash', profile=str(profile)))
            result = subprocess.run(argv, input='\uc9c8\ubb38', encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), dict(args=values, prompt='\uc9c8\ubb38'))


if __name__ == '__main__':
    unittest.main()
