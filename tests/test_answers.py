import contextlib
import io
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_context_plan as fixtures
from agent_bridge.answers import answer_for
from agent_bridge.cleanup import prune
from agent_bridge.runner import run
from agent_bridge.storage import Store

class AnswerTests(unittest.TestCase):
    setUp=fixtures.PlanTests.setUp
    save=fixtures.PlanTests.save
    write=fixtures.PlanTests.write
    call=fixtures.PlanTests.call
    def task(self, code="print('reply')"):
        self.c['endpoints']['one']['command'][-1]=code
        store=Store(self.c['state'])
        store.submit('answer',['one'],'request',self.c['endpoints'])
        with contextlib.redirect_stdout(io.StringIO()): run(self.c,True)
        return store
    def age(self,store):
        row=store.rows()[0]
        home=Path(json.loads(row['result'])['workspace'])
        old=time.time()-40*86400
        for path in sorted(home.rglob('*'),key=lambda p:len(p.parts),reverse=True): os.utime(path,(old,old))
        os.utime(home,(old,old))
        with store.connect() as db: db.execute('UPDATE tasks SET finished=?',(old,))
        return home
    def test_answer_survives_prune_and_brief_wait(self):
        store=self.task()
        expected=Path(json.loads(store.rows()[0]['result'])['workspace'],'answer.txt').read_text(encoding='utf-8')
        self.age(store)
        self.assertTrue(prune(self.c,30,['answer--one'],True)['tasks'][0]['deleted'])
        for action in ['result','wait']:
            code,out,_=self.call(action,'--id','answer','--brief')
            row=json.loads(out)[0]
            self.assertEqual(code,0)
            self.assertEqual(row['answer'],dict(text=expected,status='COMPLETE'))
            self.assertEqual(row['task_success'],'NOT_EVALUATED')
            self.assertNotIn('context_plan',row)
            self.assertNotIn('result',row)
    def test_error_answer_is_partial(self):
        store=self.task("print('partial'); raise SystemExit(1)")
        result=json.loads(store.rows()[0]['result'])
        expected=Path(result['workspace'],'answer.txt').read_text(encoding='utf-8')
        self.assertEqual(result['answer'],dict(text=expected,status='PARTIAL'))
        self.assertEqual(expected.strip(),'partial')
        code,out,_=self.call('result','--id','answer','--brief')
        self.assertEqual(code,2)
        self.assertEqual(json.loads(out)[0]['exit_code'],1)
    def test_legacy_body_archived_only_on_apply(self):
        store=self.task()
        with store.connect() as db:
            result=json.loads(db.execute('SELECT result FROM tasks').fetchone()[0])
            del result['answer']
            db.execute('UPDATE tasks SET result=?',(json.dumps(result),))
        self.age(store)
        expected=Path(json.loads(store.rows()[0]['result'])['workspace'],'answer.txt').read_text(encoding='utf-8')
        prune(self.c,30,['answer--one'],False)
        self.assertNotIn('answer',json.loads(store.rows()[0]['result']))
        before=store.rows()[0]['finished']
        prune(self.c,30,['answer--one'],True)
        self.assertEqual(answer_for(json.loads(store.rows()[0]['result']))['text'],expected)
        self.assertEqual(store.rows()[0]['finished'],before)
    def test_archive_failure_blocks_deletion(self):
        store=self.task()
        home=self.age(store)
        with patch('agent_bridge.cleanup.preserve_before_prune',side_effect=OSError('disk')):
            result=prune(self.c,30,['answer--one'],True)
        self.assertFalse(result['tasks'][0]['deleted'])
        self.assertTrue(home.exists())
    def test_legacy_and_empty_answers(self):
        self.assertEqual(answer_for({'provider_result':{'result':'legacy'}})['text'],'legacy')
        self.assertIsNone(answer_for({})['text'])
        self.assertEqual(answer_for({'answer':{'text':'','status':'COMPLETE'}})['text'],'')
    def test_mock_json_adapters_capture_common_answer(self):
        for adapter,event in [('claude',{'type':'result','result':'answer','is_error':False}),('codex',{'type':'turn.completed','usage':{}})]:
            endpoint=self.c['endpoints']['one']
            endpoint['adapter']=adapter
            program='import json; print('+repr(json.dumps(event))+')'
            if adapter=='codex':
                program += '; print('+repr(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'answer'}}))+')'
            endpoint['command'][-1]=program
            store=Store(self.c['state'])
            store.submit(adapter,['one'],'request',self.c['endpoints'])
            with contextlib.redirect_stdout(io.StringIO()): run(self.c,True)
            self.assertEqual(json.loads(store.rows(adapter)[0]['result'])['answer'],dict(text='answer',status='COMPLETE'))
