import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from agent_bridge.cli import main
from agent_bridge.cleanup import prune
from agent_bridge.runner import run
from agent_bridge.storage import Store, atomic_json, load_config
from agent_bridge import workspace
from agent_bridge.usage import snapshot, report


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-usage-")
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        self.config_path = self.root / "bridge.json"
        self.raw = dict(project=".", include=["src"], writable=["src"], timeout=5,
                        endpoints={"one":dict(adapter="command", command=[sys.executable])})
        self.save()

    def save(self):
        atomic_json(self.config_path, self.raw)
        self.c = load_config(self.config_path)
        self.store = Store(self.c["state"])

    def tearDown(self):
        self.temp.cleanup()

    def task(self, batch="sample", state="DONE", result=None):
        id_ = self.store.submit(batch, ["one"], "test", self.c["endpoints"])[0]
        self.store.claim(id_)
        if state != "RUNNING": self.store.finish(id_, state, result or {})
        return id_

    def test_codex_cache_is_subset_and_unknown_cost(self):
        value = snapshot("codex", dict(usage=dict(input_tokens=100, cached_input_tokens=80, output_tokens=5)), "alias")
        self.assertEqual(value["input_tokens"], 100)
        self.assertEqual(value["cache_read_tokens"], 80)
        self.assertIsNone(value["cost_usd"])
        self.assertIsNone(value["cache_write_tokens"])
        self.assertEqual(value["reported_model"], "UNKNOWN")
        self.assertEqual(value["requested_model"], "alias")

    def test_claude_normalizes_all_input_categories(self):
        value = snapshot("claude", dict(usage=dict(input_tokens=10, output_tokens=5,
                         cache_read_input_tokens=20, cache_creation_input_tokens=30), total_cost_usd=.25))
        self.assertEqual(value["input_tokens"], 60)
        self.assertEqual(value["cost_usd"], .25)
        self.assertIsNone(snapshot("claude", dict(usage=dict(input_tokens=10)))["input_tokens"])

    def test_invalid_values_and_crash_zeroes_are_unknown(self):
        for invalid in (True, -1, "5", float("nan"), float("inf"), 2**10000, 1.5):
            value = snapshot("codex", dict(usage=dict(input_tokens=invalid)))
            self.assertIsNone(value["input_tokens"])
            json.dumps(value, allow_nan=False)
        self.assertIsNone(snapshot("claude", dict(total_cost_usd=float("inf")))["cost_usd"])
        self.assertIsNone(snapshot("claude", dict(subtype="error_during_execution", total_cost_usd=0))["cost_usd"])
        self.assertEqual(snapshot("command", dict(usage=dict(output_tokens=5)))["source"], "UNKNOWN")

    def test_snapshot_upsert_and_recovery_preserve_usage(self):
        id_ = self.task(state="RUNNING")
        value = snapshot("codex", dict(usage=dict(input_tokens=10, output_tokens=2)))
        self.store.record_usage(id_, value)
        self.store.record_usage(id_, value)
        self.store.recover()
        result = report(Store(self.c["state"]))
        self.assertEqual(result["summary"]["tasks"], 1)
        self.assertEqual(result["summary"]["metrics"]["input_tokens"]["known_sum"], 10)
        self.assertEqual(result["summary"]["states"], {"INTERRUPTED":1})

    def test_legacy_result_and_unknown_failures(self):
        self.task("legacy", result=dict(provider_result=dict(type="turn.completed", usage=dict(input_tokens=4, output_tokens=2))))
        self.task("failed", "ERROR")
        self.store.submit("queued", ["one"], "test", self.c["endpoints"])
        result = report(self.store)
        self.assertEqual(result["summary"]["tasks"], 2)
        self.assertEqual(result["summary"]["metrics"]["input_tokens"], dict(known_sum=4, unknown_tasks=1, complete=False))
        self.assertEqual(result["summary"]["states"], {"DONE":1, "ERROR":1})
        self.assertEqual(list(report(self.store, group_by="model")["groups"]), ["UNKNOWN"])

    def test_model_groups_use_actual_models_and_model_totals(self):
        id_ = self.task()
        models = {name:dict(inputTokens=10, outputTokens=2, cacheReadInputTokens=20,
                           cacheCreationInputTokens=30, costUSD=.1) for name in ("model-a", "model-b")}
        value = snapshot("claude", dict(modelUsage=models, usage=dict(output_tokens=1), total_cost_usd=.2))
        self.store.record_usage(id_, value)
        result = report(self.store, group_by="model")
        self.assertEqual(result["summary"]["tasks"], 1)
        self.assertEqual(result["summary"]["metrics"]["input_tokens"]["known_sum"], 120)
        self.assertEqual(result["summary"]["metrics"]["output_tokens"]["known_sum"], 4)
        self.assertEqual(set(result["groups"]), set(models))
        for group in result["groups"].values():
            self.assertEqual(group["metrics"]["cost_usd"]["known_sum"], .1)
            self.assertIsNone(group["metrics"]["elapsed_seconds"]["known_sum"])

    def test_cli_filters_period_and_validation(self):
        old = self.task("old")
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET started=?,finished=? WHERE id=?", (time.time()-10*86400, time.time()-10*86400+2, old))
        self.task("new")
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.config_path), "usage", "--days", "7", "--endpoint", "one", "--batch", "new", "--group-by", "day"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["summary"]["tasks"], 1)
        self.assertEqual(report(self.store, endpoint="absent")["summary"]["tasks"], 0)
        self.assertEqual(set(report(self.store, group_by="batch")["groups"]), {"old", "new"})
        for days in (0, -1, True, 3651):
            with self.assertRaises(ValueError): report(self.store, days=days)
        with self.assertRaises(ValueError): report(self.store, group_by="invalid")

    def test_prune_keeps_durable_usage(self):
        id_ = self.task()
        home = workspace.create(self.c, id_)
        self.store.record_usage(id_, snapshot("codex", dict(usage=dict(input_tokens=8))))
        old = time.time()-60*86400
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET started=?,finished=? WHERE id=?", (old-1, old, id_))
        for path in [*home.rglob("*"), home]: os.utime(path, (old, old))
        before = report(self.store)
        prune(self.c, days=30, ids=[id_], apply=True)
        self.assertFalse(home.exists())
        self.assertEqual(report(Store(self.c["state"])), before)

    def test_runner_duplicate_terminal_events_and_crash_result(self):
        good = dict(type="result", subtype="success", result="ok", usage=dict(input_tokens=10,
                    output_tokens=2, cache_read_input_tokens=0, cache_creation_input_tokens=0), total_cost_usd=.1)
        crash = dict(type="result", subtype="error_during_execution", is_error=True, total_cost_usd=0)
        self.run_events("claude", [good, good, crash])
        result = report(self.store)
        self.assertEqual(result["summary"]["states"], {"ERROR":1})
        self.assertEqual(result["summary"]["metrics"]["input_tokens"]["known_sum"], 10)
        self.assertEqual(result["summary"]["metrics"]["cost_usd"]["known_sum"], .1)

    def test_runner_codex_terminal_usage(self):
        final = dict(type="turn.completed", usage=dict(input_tokens=12, cached_input_tokens=8, output_tokens=3))
        self.run_events("codex", [[], dict(type="item.completed", item=dict(type="agent_message", text="ok")), final, final])
        result = report(self.store)
        self.assertEqual(result["summary"]["states"], {"DONE":1})
        self.assertEqual(result["summary"]["metrics"]["input_tokens"]["known_sum"], 12)

    def run_events(self, adapter, events):
        mock = self.root / "provider.py"
        mock.write_text("import sys\nsys.stdin.read()\n" + "\n".join("print(" + repr(json.dumps(event)) + ")" for event in events), encoding="utf-8")
        self.raw["endpoints"]["one"] = dict(adapter=adapter, command=[sys.executable, str(mock)])
        self.save()
        self.store.submit("runner", ["one"], "test", self.c["endpoints"])
        with redirect_stdout(io.StringIO()): self.assertEqual(run(self.c, True), 0)


if __name__ == "__main__":
    unittest.main()
