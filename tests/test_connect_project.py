import json
from pathlib import Path
import contextlib
import io
import os
import sys
import subprocess
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import connect_project
from agent_bridge.storage import atomic_json
from agent_bridge.cli import main

class ConnectProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name)
        self.project=self.parent/'project'
        (self.project/'src').mkdir(parents=True)
        (self.project/'src/sample.py').write_text('VALUE=1\n')
        self.template=self.parent/'template.json'
        atomic_json(self.template,dict(project='.',state='.agent-bridge',include=['src'],writable=['src'],endpoints={'local-check':dict(adapter='command',command=[sys.executable,'-c',"print('ok')"])}))
    def plan(self,**kwargs):return connect_project.plan_install(self.project,sys.executable,self.template,**kwargs)
    def test_preview_does_not_write_project(self):
        before=list(self.project.rglob('*'))
        result=connect_project.install(self.plan())
        self.assertTrue(result['ok'])
        self.assertEqual(before,list(self.project.rglob('*')))
        self.assertFalse((self.project/'.agent-bridge').exists())
    def test_install_and_repeat_is_idempotent(self):
        first=connect_project.install(self.plan(),True)
        self.assertTrue(first['ok'])
        self.assertTrue((self.project/'.agents/skills/peer-consult/SKILL.md').is_file())
        self.assertTrue((self.project/'.claude/skills/peer-consult/SKILL.md').is_file())
        self.assertFalse((self.project/'.agent-bridge').exists())
        before={str(p.relative_to(self.project)):p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        second=connect_project.install(self.plan(),True)
        self.assertTrue(second['unchanged'])
        self.assertEqual(before,{str(p.relative_to(self.project)):p.read_bytes() for p in self.project.rglob('*') if p.is_file()})
    def test_installed_overview_launcher_reads_without_creating_state(self):
        connect_project.install(self.plan(),True)
        result=subprocess.run([sys.executable,str(self.project/'bridge.py'),'overview'],cwd=self.project,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout.decode('utf-8'))
        self.assertEqual(report['project']['observation'],'MISSING')
        self.assertFalse((self.project/'.agent-bridge').exists())
        self.assertFalse((self.project/'.role-flows').exists())
    def test_unmanaged_file_conflict_and_adopt_backup(self):
        (self.project/'bridge.py').write_text('user launcher')
        rejected=connect_project.install(self.plan(),True)
        self.assertFalse(rejected['ok'])
        self.assertFalse((self.project/'bridge.json').exists())
        installed=connect_project.install(self.plan(adopt=True),True)
        self.assertEqual((Path(installed['backup'])/'bridge.py').read_text(),'user launcher')
    def test_existing_config_request_and_rules_preserved(self):
        original=self.template.read_bytes()
        (self.project/'bridge.json').write_bytes(original)
        (self.project/'tasks').mkdir()
        (self.project/'tasks/request.md').write_text('user request')
        (self.project/'AGENTS.md').write_text('user rules')
        connect_project.install(self.plan(),True)
        self.assertEqual((self.project/'bridge.json').read_bytes(),original)
        self.assertEqual((self.project/'tasks/request.md').read_text(),'user request')
        self.assertEqual((self.project/'AGENTS.md').read_text(),'user rules')
    def test_changed_managed_file_is_not_overwritten(self):
        connect_project.install(self.plan(),True)
        (self.project/'bridge.py').write_text('local customization')
        result=connect_project.install(self.plan(),True)
        self.assertFalse(result['ok'])
        self.assertEqual((self.project/'bridge.py').read_text(),'local customization')
    def test_missing_input_and_worker_target_rejected(self):
        raw=json.loads(self.template.read_text());raw['include']=['missing']
        atomic_json(self.template,raw)
        with self.assertRaises(ValueError):self.plan()
        raw['include']=['src'];atomic_json(self.template,raw)
        (self.parent/'baseline.json').write_text('{}')
        with self.assertRaises(ValueError):self.plan()
    def test_readonly_role_policy_required(self):
        role=self.parent/'roles.json'
        atomic_json(role,dict(developer=dict(adapter='command',command=[sys.executable]),reviewer=dict(adapter='claude',command=['claude','--tools','Read,Edit']),checks=[[sys.executable,'-c','pass']]))
        with self.assertRaises(ValueError):self.plan(roles_template=role)
    def cli(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['connect', '--project', str(self.project),
                         '--config-template', str(self.template), *extra])
        return code, json.loads(output.getvalue())

    def test_cli_preview_matches_legacy_without_state_or_provider_calls(self):
        before = sorted(str(p.relative_to(self.project)) for p in self.project.rglob('*'))
        code, result = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(result, connect_project.install(self.plan()))
        self.assertEqual(result['ai_calls'], 0)
        self.assertFalse(result['runner_started'])
        self.assertEqual(before, sorted(str(p.relative_to(self.project)) for p in self.project.rglob('*')))

    def test_cli_install_conflict_and_repeat(self):
        (self.project/'bridge.py').write_text('custom launcher')
        code, result = self.cli('--apply')
        self.assertEqual(code, 2)
        self.assertFalse(result['ok'])
        self.assertFalse((self.project/'bridge.json').exists())
        code, result = self.cli('--adopt-existing', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual((Path(result['backup'])/'bridge.py').read_text(), 'custom launcher')
        code, result = self.cli('--apply')
        self.assertEqual(code, 0)
        self.assertTrue(result['unchanged'])
        self.assertFalse((self.project/'.agent-bridge').exists())

    def test_cli_valid_role_template_and_missing_input(self):
        role = self.parent/'roles.json'
        atomic_json(role, dict(developer=dict(adapter='codex', command=['codex']),
                              reviewer=dict(adapter='claude', command=['claude', '--tools', 'Read']),
                              checks=[[sys.executable, '-c', 'pass']]))
        code, result = self.cli('--roles-template', str(role))
        self.assertEqual(code, 0)
        self.assertTrue(result['roles_configured'])
        raw = json.loads(self.template.read_text())
        raw['include'] = ['missing']
        atomic_json(self.template, raw)
        code, result = self.cli('--apply')
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertFalse((self.project/'bridge.json').exists())

    def test_cli_does_not_load_calling_directory_config(self):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]/'src')
        (self.parent/'bridge.json').write_text('invalid unrelated config')
        result = subprocess.run([sys.executable, '-B', '-m', 'agent_bridge', 'connect',
                                 '--project', str(self.project), '--config-template', str(self.template)],
                                cwd=self.parent, env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertFalse((self.parent/'.agent-bridge').exists())

if __name__=='__main__':unittest.main(verbosity=2)
