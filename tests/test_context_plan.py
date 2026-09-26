import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from agent_bridge import context, workspace
from agent_bridge.cli import main
from agent_bridge.runner import run
from agent_bridge.storage import Store, atomic_json, load_config

class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'src').mkdir()
        for name, data in [('AGENTS.md', b'rules'), ('HANDOFF.md', b'current'), ('src/a', '\uD55C\uAE00'.encode()), ('src/empty', b''), ('src/blob', bytes(range(256)))]:
            (self.root/name).write_bytes(data)
        self.raw = dict(project='.', include=['AGENTS.md','HANDOFF.md','src','src/a'], writable=['src'], context_required=['HANDOFF.md'], endpoints={'one':dict(adapter='command',command=[sys.executable,'-c',"print('ok')"])})
        self.path = self.root/'bridge.json'
        self.prompt = self.root/'prompt.txt'
        self.prompt.write_text('request', encoding='utf-8')
        self.plan_path = self.root/'plan.json'
        self.save()
        self.plan = dict(schema=1, files=[dict(path=i['path'], mode='omit' if i['path']=='src/blob' else 'full',reason='test') for i in context.preview(self.c)['files']])
        self.write()
    def save(self):
        atomic_json(self.path,self.raw)
        self.c=load_config(self.path)
    def write(self): atomic_json(self.plan_path,self.plan)
    def preview(self): return context.plan_preview(self.c,'request',self.plan_path)
    def call(self,*args):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=main(['--config',str(self.path),*args])
        return code,out.getvalue(),err.getvalue()
    def test_read_only_deterministic_selection(self):
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('agent_bridge.cli.Store') as store,patch('agent_bridge.runner.subprocess.Popen') as launch:
            code,out,_=self.call('context','--prompt-file',str(self.prompt),'--context-plan',str(self.plan_path))
            store.assert_not_called()
            launch.assert_not_called()
        report=json.loads(out)
        self.assertEqual(code,0)
        self.assertEqual(report['scope'],'preview_only')
        self.assertFalse(report['selection_applied_to_execution'])
        self.assertEqual(report['candidate_count'],5)
        self.assertEqual(report['summary']['file_bytes'],18)
        self.assertEqual(report['summary']['file_count'],4)
        self.assertEqual([i['path'] for i in report['required']],['AGENTS.md','HANDOFF.md'])
        self.assertEqual([i['path'] for i in report['omitted']],['src/blob'])
        self.assertEqual(before,{p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.plan['files'].reverse()
        self.write()
        self.assertEqual(report,self.preview())
    def test_budget_boundary_and_submit_still_uses_all_files(self):
        total=self.preview()['summary']['total_bytes']
        self.raw['context_budget']=dict(max_bytes=total,max_files=4)
        self.save()
        self.assertTrue(self.preview()['ok'])
        code,out,_=self.call('submit','--id','whole','--targets','one','--prompt-file',str(self.prompt))
        self.assertEqual(code,2)
        self.assertFalse(json.loads(out)['ok'])
        self.assertFalse(self.c['state'].exists())
        self.raw['context_budget']['max_bytes']-=1
        self.save()
        code,out,_=self.call('context','--prompt-file',str(self.prompt),'--context-plan',str(self.plan_path))
        self.assertEqual(code,2)
        self.assertEqual(json.loads(out)['summary']['total_bytes'],total)
    def test_invalid_schema_paths_and_coverage(self):
        original=copy.deepcopy(self.plan)
        cases=[[],{},dict(original,schema=True),dict(original,schema=2),dict(original,extra=1),dict(original,files={}),dict(original,files=original['files'][:-1]),dict(original,files=original['files']+[original['files'][0]])]
        for change in [dict(mode='summary'),dict(reason=' '),dict(path='../escape'),dict(path='src//a'),dict(path='src/./a'),dict(path='src/*'),dict(path='.venv/secret'),dict(path='outside'),dict(extra=1),dict(mode=None)]:
            altered=copy.deepcopy(original)
            altered['files'][-1].update(change)
            cases.append(altered)
        for case in cases:
            with self.subTest(case=case):
                self.plan=case
                self.write()
                with self.assertRaises(ValueError): self.preview()
    def test_duplicate_keys_and_nonfinite_json(self):
        for text in ['{"schema":1,"schema":1,"files":[]}','{"schema":NaN,"files":[]}','{"schema":1,"files":[{"path":"a","path":"b"}]}','{']:
            self.plan_path.write_text(text,encoding='utf-8')
            code,_,err=self.call('context','--prompt-file',str(self.prompt),'--context-plan',str(self.plan_path))
            self.assertEqual(code,1)
            self.assertTrue(err)
        self.assertFalse(self.c['state'].exists())
    def test_required_and_nested_rules_cannot_be_omitted(self):
        for name in ['AGENTS.md','HANDOFF.md']:
            entry=next(e for e in self.plan['files'] if e['path']==name)
            entry['mode']='omit'
            self.write()
            with self.assertRaisesRegex(ValueError,'Cannot omit required'): self.preview()
            entry['mode']='full'
        (self.root/'src/AGENTS.md').write_text('nested')
        self.plan['files'].append(dict(path='src/AGENTS.md',mode='omit',reason='test'))
        self.write()
        with self.assertRaisesRegex(ValueError,'Cannot omit required'): self.preview()
    def test_required_validation_and_submit_missing(self):
        for value in [None,{},['src'],['missing'],['../outside'],['src/*'],['HANDOFF.md','HANDOFF.md']]:
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.raw['context_required']=value
                self.save()
                context.preview(self.c)
        self.raw['context_required']=['HANDOFF.md']
        self.save()
        (self.root/'HANDOFF.md').unlink()
        code,_,_=self.call('submit','--id','missing','--targets','one','--prompt-file',str(self.prompt))
        self.assertEqual(code,1)
        self.assertFalse(self.c['state'].exists())
    def test_missing_include_and_changed_candidates(self):
        self.raw['include'].append('missing')
        self.save()
        self.assertEqual(context.preview(self.c)['missing'],['missing'])
        with self.assertRaisesRegex(ValueError,'missing include'): self.preview()
        self.raw['include'].pop()
        self.save()
        (self.root/'src/new').write_text('new')
        with self.assertRaisesRegex(ValueError,'every candidate'): self.preview()
    def test_aliases_and_links_rejected(self):
        os.link(self.root/'src/a',self.root/'src/alias')
        self.plan['files'].append(dict(path='src/alias',mode='full',reason='alias'))
        self.write()
        with self.assertRaisesRegex(ValueError,'same file'): self.preview()
        with patch.object(Path,'is_symlink',return_value=True):
            with self.assertRaisesRegex(ValueError,'Links'): self.preview()
    def test_required_rechecked_before_provider_and_after_copy(self):
        for after_copy in [False,True]:
            (self.root/'HANDOFF.md').write_text('current')
            batch='copy' if after_copy else 'source'
            Store(self.c['state']).submit(batch,['one'],'test',self.c['endpoints'])
            original=workspace.create
            def changed(config,id_):
                home=original(config,id_)
                (home/'project/HANDOFF.md').unlink()
                return home
            if not after_copy: (self.root/'HANDOFF.md').unlink()
            with patch('agent_bridge.runner.subprocess.Popen') as launch,patch('agent_bridge.runner.workspace.create',side_effect=changed if after_copy else original):
                run(self.c,True)
                launch.assert_not_called()
            row=Store(self.c['state']).rows(batch)[0]
            self.assertEqual(row['state'],'ERROR')
            self.assertIn('Required file',row['result'])
    def test_submit_accepts_and_stores_plan(self):
        code,out,_=self.call('submit','--id','planned','--targets','one','--prompt-file',str(self.prompt),'--context-plan',str(self.plan_path))
        self.assertEqual(code,0)
        result=json.loads(out)
        self.assertEqual(result['context']['scope'],'submitted_plan')
        self.assertTrue(result['context']['selection_applied_to_execution'])
        store=Store(self.c['state'])
        self.assertIsNotNone(store.context_plan('planned--one'))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks').fetchone()[0],'PLAN_QUEUED')
