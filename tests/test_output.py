import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_context_plan as fixtures
from agent_bridge.runner import run
from agent_bridge.storage import Store

class OutputTests(unittest.TestCase):
    setUp=fixtures.PlanTests.setUp
    save=fixtures.PlanTests.save
    write=fixtures.PlanTests.write
    def submit(self):
        self.c['endpoints']['one']['command'][-1]="import sys; sys.stdout.buffer.write('\\u2705\\n'.encode('utf-8'))"
        store=Store(self.c['state'])
        store.submit('unicode',['one'],'test',self.c['endpoints'])
        return store
    def test_runner_and_result_support_legacy_console(self):
        store=self.submit()
        output=io.BytesIO()
        stream=io.TextIOWrapper(output,encoding='cp949')
        with contextlib.redirect_stdout(stream):run(self.c,True)
        stream.flush()
        self.assertIn('\u2705',output.getvalue().decode('utf-8'))
        self.assertEqual(store.rows()[0]['state'],'DONE')
        source_root=str(Path(__file__).resolve().parents[1]/'src')
        # Run the same imported package (source or wheel) as this test process.
        import agent_bridge
        package_parent=str(Path(agent_bridge.__file__).parent.parent)
        env=dict(os.environ,PYTHONPATH=package_parent,PYTHONUTF8='0',PYTHONIOENCODING='cp949')
        proc=subprocess.run([sys.executable,'-X','utf8=0','-m','agent_bridge','--config',str(self.path),'result','--id','unicode','--brief'],env=env,capture_output=True,timeout=15)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertIn('\u2705',json.loads(proc.stdout.decode('utf-8'))[0]['answer']['text'])
        stream.detach()
    def test_failed_console_does_not_fail_task(self):
        class BrokenConsole:
            def write(self,value):raise OSError('closed output sink')
            def flush(self):pass
        store=self.submit()
        with contextlib.redirect_stdout(BrokenConsole()):run(self.c,True)
        row=store.rows()[0]
        self.assertEqual(row['state'],'DONE')
        self.assertIn('\u2705',json.loads(row['result'])['answer']['text'])
if __name__=='__main__':unittest.main(verbosity=2)
