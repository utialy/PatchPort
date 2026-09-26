import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent_bridge import context, workspace
from agent_bridge.cli import main
from agent_bridge.runner import run
from agent_bridge.storage import Store, atomic_json, load_config


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src/a.txt").write_text("\ud55c\uae00", encoding="utf-8")
        self.path = self.root / "bridge.json"
        self.raw = dict(project=".", include=["src", "src/a.txt", "missing"], writable=["src"],
                        endpoints={"one":dict(adapter="command", command=[sys.executable, "-c", "print('ok')"])})
        self.save()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("\uc9c0\uc2dc", encoding="utf-8")

    def tearDown(self): self.temp.cleanup()

    def save(self):
        atomic_json(self.path, self.raw)
        self.c = load_config(self.path)

    def call(self, *args):
        output = io.StringIO()
        with redirect_stdout(output): code = main(["--config", str(self.path), *args])
        return code, json.loads(output.getvalue())

    def test_preview_deduplicates_and_measures_utf8_without_state(self):
        code, report = self.call("context", "--prompt-file", str(self.prompt))
        self.assertEqual(code, 0)
        self.assertEqual(report["summary"]["file_count"], 1)
        self.assertEqual(report["summary"]["file_bytes"], 6)
        self.assertEqual(report["missing"], ["missing"])
        self.assertEqual(report["summary"]["prompt_bytes"], len((context.PREFIX+"\uc9c0\uc2dc").encode()))
        self.assertFalse(self.c["state"].exists())

    def test_budget_boundary_and_submit_rejection(self):
        total = context.preview(self.c, "\uc9c0\uc2dc")["summary"]["total_bytes"]
        self.raw["context_budget"] = dict(max_files=1, max_bytes=total, max_estimated_tokens=(total+3)//4)
        self.save()
        self.assertTrue(context.preview(self.c, "\uc9c0\uc2dc")["ok"])
        self.raw["context_budget"]["max_bytes"] -= 1
        self.save()
        code, report = self.call("submit", "--id", "test", "--targets", "one", "--prompt-file", str(self.prompt))
        self.assertEqual(code, 2)
        self.assertEqual(report["exceeded"][0]["limit"], "max_bytes")
        self.assertFalse(self.c["state"].exists())

    def test_invalid_budget_config(self):
        for value in (None, [], {"unknown":1}, {"max_bytes":True}, {"max_files":-1}, {"max_estimated_tokens":1.5}):
            self.raw["context_budget"] = value
            with self.assertRaises(ValueError): self.save()

    def test_runner_rechecks_growth_without_provider(self):
        self.raw["context_budget"] = dict(max_files=1)
        self.save()
        self.call("submit", "--id", "grow", "--targets", "one", "--prompt-file", str(self.prompt))
        (self.root / "src/b.txt").write_text("new")
        with patch("agent_bridge.runner.subprocess.Popen") as launch, redirect_stdout(io.StringIO()):
            self.assertEqual(run(self.c, True), 0)
            launch.assert_not_called()
        row = Store(self.c["state"]).rows()[0]
        self.assertEqual(row["state"], "ERROR")
        self.assertIn("Context budget exceeded", row["result"])

    def test_copied_inventory_persisted(self):
        self.call("submit", "--id", "run", "--targets", "one", "--prompt-file", str(self.prompt))
        with redirect_stdout(io.StringIO()): run(self.c, True)
        row = Store(self.c["state"]).rows()[0]
        self.assertEqual(row["state"], "DONE")
        result = json.loads(row["result"])
        report = json.loads((Path(result["workspace"]) / "context.json").read_text())
        self.assertEqual(report, result["context"])
        self.assertEqual(report["summary"]["file_bytes"], 6)
        delivered = (Path(result["workspace"]) / "prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(delivered, context.PREFIX + "\uc9c0\uc2dc")
        self.assertEqual(report["summary"]["prompt_bytes"], len(delivered.encode("utf-8")))

    def test_growth_during_copy_is_blocked(self):
        self.raw["context_budget"] = dict(max_files=1)
        self.save()
        self.call("submit", "--id", "race", "--targets", "one", "--prompt-file", str(self.prompt))
        original = workspace.create
        def changed(config, id_):
            home = original(config, id_)
            (home / "project/src/new.txt").write_text("new")
            return home
        with patch("agent_bridge.runner.workspace.create", side_effect=changed), patch("agent_bridge.runner.subprocess.Popen") as launch, redirect_stdout(io.StringIO()):
            run(self.c, True)
            launch.assert_not_called()
        row = Store(self.c["state"]).rows()[0]
        self.assertEqual(row["state"], "ERROR")
        self.assertFalse(json.loads(row["result"])["context"]["ok"])
