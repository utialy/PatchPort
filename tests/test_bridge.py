import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from types import SimpleNamespace

from agent_bridge.storage import Store, FileLock, atomic_json, load_config
from agent_bridge.runner import run, argv_for
from agent_bridge import workspace
from agent_bridge.cli import main

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.root=Path(self.temp.name)
        (self.root/"src").mkdir()
        (self.root/"src/base[1].txt").write_text("before")
        self.mock=self.root/"mock.py"
        self.mock.write_text("import pathlib,sys,time; text=sys.stdin.read(); time.sleep(.3); pathlib.Path('src/result[1].txt').write_text('done'); print('answer '+text)")
        self.config_path=self.root/"bridge.json"
        self.raw=dict(project=".",include=["src"],writable=["src"],parallel=2,timeout=5,endpoints={k:dict(adapter="command",command=[sys.executable,str(self.mock)],parallel=1) for k in ("one","two")})
        self.save()
    def save(self):
        atomic_json(self.config_path,self.raw);self.c=load_config(self.config_path);self.store=Store(self.c["state"])
    def tearDown(self): self.temp.cleanup()
    def test_pause_persists_and_once_leaves_queue(self):
        self.store.submit("paused", ["one"], "test", self.c["endpoints"])
        self.store.pause()
        self.assertEqual(run(self.c, True), 2)
        reopened = Store(self.c["state"])
        self.assertTrue(reopened.control()["paused"])
        self.assertEqual(reopened.rows()[0]["state"], "QUEUED")
        self.assertFalse((self.c["state"] / "workspaces").exists())
        reopened.pause(False)
        self.assertEqual(run(self.c, True), 0)
        self.assertEqual(reopened.rows()[0]["state"], "DONE")

    def test_limit_survives_restart_and_resume(self):
        self.store.submit("limited", ["one", "two"], "test", self.c["endpoints"])
        self.store.set_limit(1)
        self.assertEqual(run(self.c, True), 2)
        self.assertEqual(sorted(r["state"] for r in self.store.rows()), ["DONE", "QUEUED"])
        reopened = Store(self.c["state"])
        reopened.pause(); reopened.pause(False)
        self.assertEqual(run(self.c, True), 2)
        self.assertEqual(reopened.control()["calls_started"], 1)
        reopened.set_limit(2)
        self.assertEqual(run(self.c, True), 0)
        self.assertEqual([r["state"] for r in reopened.rows()], ["DONE", "DONE"])

    def test_claim_limit_is_atomic_across_connections(self):
        ids = [self.store.submit("race" + str(i), ["one"], "test", self.c["endpoints"])[0] for i in range(10)]
        self.store.set_limit(3)
        with ThreadPoolExecutor(max_workers=10) as pool:
            claimed = list(pool.map(lambda id_: Store(self.c["state"]).claim(id_), ids))
        self.assertEqual(sum(claimed), 3)
        self.assertEqual(self.store.control()["calls_started"], 3)
        self.store.recover()
        self.assertEqual(self.store.control()["calls_started"], 3)
        self.assertEqual(self.store.control()["dispatch"], "LIMIT_REACHED")

    def test_duplicate_claim_and_failures_do_not_refund_limit(self):
        id_ = self.store.submit("count", ["one"], "test", self.c["endpoints"])[0]
        self.store.set_limit(2)
        self.assertFalse(self.store.claim("absent"))
        self.assertTrue(self.store.claim(id_))
        self.assertFalse(self.store.claim(id_))
        self.store.finish(id_, "ERROR", {"error":"before provider launch"})
        self.assertEqual(self.store.control()["calls_started"], 1)
        self.store.set_limit(0)
        self.assertEqual(self.store.control()["dispatch"], "LIMIT_REACHED")
        self.store.set_limit(None)
        self.assertEqual(self.store.control()["dispatch"], "READY")
        for value in (-1, True, 1.5, "2", 2**63):
            with self.assertRaises(ValueError): self.store.set_limit(value)

    def test_existing_database_migrates_started_count(self):
        id_ = self.store.submit("old", ["one"], "test", self.c["endpoints"])[0]
        self.store.claim(id_)
        with self.store.connect() as db:
            db.execute("DROP TABLE control")
        reopened = Store(self.c["state"])
        self.assertEqual(reopened.control()["calls_started"], 1)
        reopened.set_limit(1)
        self.assertEqual(Store(self.c["state"]).control()["dispatch"], "LIMIT_REACHED")

    def test_control_cli(self):
        def call(*args):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--config", str(self.config_path), *args])
            self.assertEqual(code, 0)
            return json.loads(out.getvalue())
        self.assertEqual(call("limit", "--max-calls", "0")["dispatch"], "LIMIT_REACHED")
        self.assertEqual(call("pause")["dispatch"], "PAUSED")
        self.assertEqual(call("resume")["dispatch"], "LIMIT_REACHED")
        self.assertEqual(call("limit", "--unlimited")["dispatch"], "READY")
        self.assertEqual(call("status")["control"]["calls_started"], 0)

    def test_pause_allows_already_claimed_work_to_finish(self):
        self.store.submit("active", ["one"], "test", self.c["endpoints"])
        self.store.submit("waiting", ["one"], "test", self.c["endpoints"])
        self.raw["parallel"] = 1
        self.save()
        from agent_bridge import runner
        original = runner.execute
        def pause_then_execute(*args):
            self.store.pause()
            return original(*args)
        with patch("agent_bridge.runner.execute", side_effect=pause_then_execute):
            self.assertEqual(run(self.c, True), 2)
        self.assertEqual(self.store.rows("active")[0]["state"], "DONE")
        self.assertEqual(self.store.rows("waiting")[0]["state"], "QUEUED")

    def test_doctor_reports_all_endpoints_without_creating_state(self):
        self.raw["state"] = "doctor-state"
        self.raw["endpoints"]["two"]["command"] = ["bridge-no-such-executable-924621"]
        atomic_json(self.config_path, self.raw)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.config_path), "doctor"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(report["checks"]["one"]["ok"])
        self.assertFalse(report["checks"]["two"]["ok"])
        self.assertIn("not found", report["checks"]["two"]["error"])
        self.assertFalse((self.root / "doctor-state").exists())

    def test_doctor_valid_commands_do_not_execute(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.config_path), "doctor"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertFalse((self.c["state"] / "workspaces").exists())
        self.assertEqual(self.store.rows(), [])

    def test_windows_shims_rejected_by_doctor_and_runner(self):
        for suffix in (".cmd", ".bat", ".ps1"):
            with self.subTest(suffix=suffix), patch("agent_bridge.runner.os", SimpleNamespace(name="nt")), patch("agent_bridge.runner.shutil.which", return_value=str(self.root / ("fake" + suffix))):
                with self.assertRaisesRegex(ValueError, "shell shims"):
                    argv_for(self.raw["endpoints"]["one"], self.root / "reply")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["--config", str(self.config_path), "doctor"])
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_resolved_executable_is_absolute(self):
        with patch("agent_bridge.runner.shutil.which", return_value="relative/python.exe"):
            argv = argv_for(self.raw["endpoints"]["one"], self.root / "reply")
        self.assertEqual(argv[0], str(Path("relative/python.exe").resolve()))

    def test_done_does_not_claim_task_success(self):
        events = [dict(type="item.completed", item=dict(type="agent_message", text="Unable to edit the file.")),
                  dict(type="turn.completed", usage={})]
        self.mock.write_text("\n".join("print(" + repr(json.dumps(event)) + ")" for event in events))
        self.raw["endpoints"] = {"one":dict(adapter="codex",command=[sys.executable,str(self.mock)])}
        self.save()
        self.store.submit("refusal", ["one"], "Edit the file", self.c["endpoints"])
        run(self.c, True)
        for action in ("result", "wait"):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--config",str(self.config_path),action,"--id","refusal"])
            row = json.loads(out.getvalue())[0]
            self.assertEqual(code, 0)
            self.assertEqual(row["state"], "DONE")
            self.assertEqual(row["task_success"], "NOT_EVALUATED")
            self.assertEqual(row["result"]["changes"], [])
    def test_fanout_and_isolation(self):
        ids=self.store.submit("batch",["one","two"],"test",self.c["endpoints"])
        run(self.c,True)
        rows=self.store.rows("batch")
        self.assertEqual([r["state"] for r in rows],["DONE","DONE"])
        self.assertLess(max(r["started"] for r in rows),min(r["finished"] for r in rows))
        self.assertFalse((self.root/"src/result[1].txt").exists())
        for id_ in ids:
            manifest=workspace.review(self.c,id_)
            self.assertEqual(manifest[0]["path"],"src/result[1].txt")
        applied=workspace.review(self.c,ids[0],["src/result[1].txt"])
        self.assertEqual((self.root/"src/result[1].txt").read_text(),"done")
        with self.assertRaises(ValueError): workspace.review(self.c,ids[1],["src/result[1].txt"])
        workspace.recover(self.c,ids[0],applied["backup"],True)
        self.assertFalse((self.root/"src/result[1].txt").exists())
    def test_duplicate_batch_atomic(self):
        self.store.submit("a",["one"],"test",self.c["endpoints"])
        with self.assertRaises(sqlite3.IntegrityError): self.store.submit("a",["two","one"],"test",self.c["endpoints"])
        self.assertEqual(len(self.store.rows()),1)
    def test_recover_claim_without_replay(self):
        id_=self.store.submit("a",["one"],"test",self.c["endpoints"])[0]
        self.assertTrue(self.store.claim(id_));self.store.recover()
        self.assertEqual(self.store.rows()[0]["state"],"INTERRUPTED")
        self.assertFalse(self.store.claim(id_))
    def test_lock(self):
        with FileLock(self.c["state"]/"runner.lock"):
            with self.assertRaises(RuntimeError):
                with FileLock(self.c["state"]/"runner.lock"): pass
    def test_timeout(self):
        self.mock.write_text("import time; time.sleep(30)")
        self.raw["timeout"]=1;self.save()
        self.store.submit("slow",["one"],"test",self.c["endpoints"])
        start=time.monotonic();run(self.c,True)
        self.assertEqual(self.store.rows()[0]["state"],"TIMEOUT")
        self.assertLess(time.monotonic()-start,10)
    def test_endpoint_limit(self):
        self.store.submit("a",["one"],"test",self.c["endpoints"])
        self.store.submit("b",["one"],"test",self.c["endpoints"])
        run(self.c,True)
        a,b=self.store.rows()
        self.assertLessEqual(a["finished"],b["started"])
    def test_failure_isolated(self):
        self.raw["endpoints"]["two"]["command"]=[sys.executable,"-c","raise SystemExit(3)"];self.save()
        self.store.submit("a",["one","two"],"test",self.c["endpoints"])
        run(self.c,True)
        self.assertEqual({r["endpoint"]:r["state"] for r in self.store.rows()},{"one":"DONE","two":"ERROR"})
    def test_schema_and_paths(self):
        with self.assertRaises(ValueError): self.store.submit("../bad",["one"],"x",self.c["endpoints"])
        with self.assertRaises(ValueError): workspace.safe(self.root,"../outside")
        self.raw["include"]=[".venv"];atomic_json(self.config_path,self.raw)
        with self.assertRaises(ValueError): load_config(self.config_path)
    def test_conflict_and_binary_recovery(self):
        home=workspace.create(self.c,"sample")
        rel="src/base[1].txt"
        (home/"project"/rel).write_bytes(b"\x00\xffmodified")
        workspace.changes(self.c,home)
        result=workspace.review(self.c,"sample",[rel])
        (self.root/rel).write_text("later edit")
        with self.assertRaises(ValueError): workspace.recover(self.c,"sample",result["backup"],True)
        (self.root/rel).write_bytes(b"\x00\xffmodified")
        workspace.recover(self.c,"sample",result["backup"],True)
        self.assertEqual((self.root/rel).read_text(),"before")
    def test_provider_event_contracts(self):
        for name,event in (("claude",{"type":"result","is_error":False,"result":"mock reply"}),("codex",{"type":"turn.completed","usage":{}})):
            self.mock.write_text("import json; print("+repr(json.dumps(event))+")" + ("; print(" + repr(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"mock reply"}})) + ")" if name == "codex" else ""))
            self.raw["endpoints"]={name:dict(adapter=name,command=[sys.executable,str(self.mock)],parallel=1)};self.save()
            self.store.submit(name,[name],"test",self.c["endpoints"]);run(self.c,True)
            self.assertEqual(self.store.rows(name)[0]["state"],"DONE")
    def test_timeout_cleans_descendant(self):
        self.mock.write_text("import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path('pid.txt').write_text(str(p.pid)); time.sleep(60)")
        self.raw["timeout"]=2;self.save()
        self.store.submit("tree",["one"],"test",self.c["endpoints"]);run(self.c,True)
        pid=int((self.c["state"]/"workspaces/tree--one/project/pid.txt").read_text())
        self.assertEqual(self.store.rows()[0]["state"],"TIMEOUT")
        if os.name == "nt":
            import ctypes
            k=ctypes.WinDLL("kernel32",use_last_error=True)
            k.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32];k.OpenProcess.restype=ctypes.c_void_p
            k.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
            k.CloseHandle.argtypes=[ctypes.c_void_p]
            h=k.OpenProcess(0x100000,False,pid)
            if h:
                try:self.assertEqual(k.WaitForSingleObject(h,5000),0)
                finally:k.CloseHandle(h)
        else:
            # A reaped descendant or an exited zombie is no longer executing.
            out=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True).stdout.strip()
            self.assertTrue(not out or out.startswith('Z'),out)
    def test_runner_crash_marks_interrupted(self):
        self.mock.write_text("import pathlib,time; pathlib.Path('started.txt').write_text('yes'); time.sleep(60)")
        self.store.submit("crash",["one"],"test",self.c["endpoints"])
        worker=subprocess.Popen([sys.executable,'-m','agent_bridge','--config',str(self.config_path),'run','--once'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            marker=self.c["state"]/'workspaces/crash--one/project/started.txt'
            deadline=time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.05)
            self.assertTrue(marker.exists())
            worker.kill();worker.wait(timeout=5)
            time.sleep(.3)
            run(self.c,True)
            self.assertEqual(self.store.rows()[0]["state"],"INTERRUPTED")
        finally:
            if worker.poll() is None:worker.kill();worker.wait()
    def test_missing_final_provider_event(self):
        self.mock.write_text("print('{}')")
        self.raw["endpoints"]={"one":dict(adapter="claude",command=[sys.executable,str(self.mock)])};self.save()
        self.store.submit("bad",["one"],"test",self.c["endpoints"]);run(self.c,True)
        self.assertEqual(self.store.rows()[0]["state"],"ERROR")

if __name__ == "__main__": unittest.main()
