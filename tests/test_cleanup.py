import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agent_bridge.cleanup import prune
from agent_bridge.cli import main
from agent_bridge.storage import Store, FileLock, atomic_json, load_config
from agent_bridge import workspace


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-cleanup-")
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src/base[1].txt").write_text("before")
        self.config_path = self.root / "bridge.json"
        atomic_json(self.config_path, dict(project=".", include=["src"], writable=["src"],
                    endpoints={"one":dict(adapter="command",command=[sys.executable])}))
        self.c = load_config(self.config_path)
        self.store = Store(self.c["state"])
        self.old = time.time() - 60 * 86400

    def tearDown(self):
        self.temp.cleanup()

    def task(self, batch="sample", state="DONE"):
        id_ = self.store.submit(batch, ["one"], "test", self.c["endpoints"])[0]
        home = workspace.create(self.c, id_)
        (home / "answer.txt").write_text("answer")
        (home / "events.jsonl").write_text("{}\n")
        if state != "QUEUED":
            self.store.claim(id_)
        if state not in ("QUEUED", "RUNNING"):
            self.store.finish(id_, state, {"workspace":str(home)})
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET finished=? WHERE id=? AND finished IS NOT NULL", (self.old, id_))
        self.age(home)
        return id_, home

    def age(self, home):
        for path in [*home.rglob("*"), home]:
            os.utime(path, (self.old, self.old))

    def rolled_back(self, id_, home):
        (home / "project/src/base[1].txt").write_text("after")
        workspace.changes(self.c, home)
        applied = workspace.review(self.c, id_, ["src/base[1].txt"])
        workspace.recover(self.c, id_, applied["backup"], True)
        return home / applied["backup"] / "journal.json"

    def test_preview_delete_and_preserve_queue_and_budget(self):
        id_, home = self.task()
        self.store.set_limit(1)
        rows = self.store.rows()
        controls = self.store.control()
        report = prune(self.c)
        self.assertTrue(report["tasks"][0]["eligible"])
        self.assertGreater(report["eligible_bytes"], 0)
        self.assertTrue(home.exists())
        self.assertEqual(self.store.rows(), rows)
        deleted = prune(self.c, ids=[id_], apply=True)
        self.assertTrue(deleted["tasks"][0]["deleted"])
        self.assertFalse(home.exists())
        expected_result = json.loads(rows[0]["result"])
        expected_result["answer"] = {"text": "answer", "status": "COMPLETE"}
        rows[0]["result"] = json.dumps(expected_result, ensure_ascii=False)
        self.assertEqual(self.store.rows(), rows)
        self.assertEqual(self.store.control(), controls)
        self.assertEqual(self.store.artifact_status(id_)["state"], "DELETED")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.submit("sample", ["one"], "again", self.c["endpoints"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config",str(self.config_path),"result","--id","sample"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())[0]["artifact_cleanup"]["state"], "DELETED")

    def test_nonterminal_recent_and_unknown_state_protected(self):
        for index, state in enumerate(("QUEUED", "RUNNING", "UNKNOWN")):
            self.task("protected" + str(index), state)
        _, recent = self.task("recent")
        (recent / "answer.txt").write_text("new answer")
        rows = prune(self.c)["tasks"]
        self.assertEqual([r["reason"] for r in rows[:3]], ["TASK_NOT_TERMINAL"] * 3)
        self.assertEqual(rows[3]["reason"], "RETENTION_PERIOD")

    def test_requires_explicit_valid_selection_before_deleting(self):
        id_, home = self.task()
        for kwargs in ({"apply":True}, {"ids":[id_, "missing"], "apply":True},
                       {"ids":[id_, id_]}, {"ids":["../bad"]}, {"days":0}):
            with self.assertRaises(ValueError): prune(self.c, **kwargs)
        self.assertTrue(home.exists())

    def test_runner_and_promotion_lock_block_cleanup(self):
        id_, home = self.task()
        for lock in ("runner.lock", "promotion.lock"):
            with FileLock(self.c["state"] / lock):
                with self.assertRaises(RuntimeError): prune(self.c, ids=[id_], apply=True)
        self.assertTrue(home.exists())

    def test_backups_protected_until_rolled_back_and_aged(self):
        id_, home = self.task()
        (home / "project/src/base[1].txt").write_text("after")
        workspace.changes(self.c, home)
        applied = workspace.review(self.c, id_, ["src/base[1].txt"])
        self.age(home)
        self.assertEqual(prune(self.c)["tasks"][0]["reason"], "RECOVERY_DATA_PROTECTED")
        workspace.recover(self.c, id_, applied["backup"], True)
        journal_path = home / applied["backup"] / "journal.json"
        self.age(home)
        self.assertEqual(prune(self.c)["tasks"][0]["reason"], "RETENTION_PERIOD")
        journal = json.loads(journal_path.read_text())
        journal["updated"] = self.old
        atomic_json(journal_path, journal)
        self.age(home)
        self.assertTrue(prune(self.c, ids=[id_], apply=True)["tasks"][0]["deleted"])
        self.assertEqual((self.root / "src/base[1].txt").read_text(), "before")

    def test_missing_malformed_and_pending_journals_protected(self):
        id_, home = self.task()
        backup = home / "backup-incomplete"
        backup.mkdir()
        journal = backup / "journal.json"
        samples = [None, "{", "[]", '{"state":"APPLYING"}', '{"state":"RECOVERING"}',
                   '{"state":"ROLLED_BACK","changes":[]}']
        for sample in samples:
            if sample is not None: journal.write_text(sample)
            self.age(home)
            report = prune(self.c, ids=[id_], apply=True)
            self.assertFalse(report["tasks"][0]["deleted"])
            self.assertTrue(home.exists())

    def test_damaged_rolled_back_backup_protected(self):
        id_, home = self.task()
        journal_path = self.rolled_back(id_, home)
        (journal_path.parent / "src/base[1].txt").write_text("damaged")
        self.age(home)
        self.assertEqual(prune(self.c)["tasks"][0]["reason"], "UNSAFE_OR_INCOMPLETE")

    def test_links_even_in_excluded_directories_are_rejected(self):
        id_, home = self.task()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        cache = home / "project/__pycache__"
        cache.mkdir()
        link = cache / "outside"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Symlink creation unavailable")
            command = "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''") + "' -Target '" + str(outside).replace("'", "''") + "' -ErrorAction Stop | Out-Null"
            created = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, timeout=15)
            if created.returncode:
                self.skipTest("Symlink and junction creation unavailable")
        try:
            self.assertFalse(prune(self.c, ids=[id_], apply=True)["tasks"][0]["eligible"])
            self.assertEqual((outside / "keep.txt").read_text(), "keep")
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                os.rmdir(link)

    def test_recent_change_between_preview_and_deletion_is_rechecked(self):
        id_, home = self.task()
        from agent_bridge import cleanup
        original = cleanup.candidate
        def inspect_and_edit(*args):
            result = original(*args)
            (home / "answer.txt").write_text("edited during preview")
            return result
        with patch("agent_bridge.cleanup.candidate", side_effect=inspect_and_edit):
            report = prune(self.c, ids=[id_], apply=True)
        self.assertFalse(report["tasks"][0]["deleted"])
        self.assertEqual(report["tasks"][0]["reason"], "RETENTION_PERIOD")
        self.assertTrue(home.exists())

    def test_reparse_ancestor_detected_without_is_junction(self):
        id_, home = self.task()
        original = Path.lstat
        class ReparseInfo:
            st_file_attributes = 0x400
            def __init__(self, info): self.info = info
            def __getattr__(self, name): return getattr(self.info, name)
        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return ReparseInfo(info) if path == home.parent else info
        with patch.object(Path, "lstat", lstat):
            report = prune(self.c, ids=[id_], apply=True)
        self.assertEqual(report["tasks"][0]["reason"], "UNSAFE_OR_INCOMPLETE")
        self.assertTrue(home.exists())

    def test_delete_failure_recorded_and_other_artifacts_untouched(self):
        id_, home = self.task()
        _, other = self.task("other")
        with patch("agent_bridge.cleanup.shutil.rmtree", side_effect=PermissionError("locked")):
            report = prune(self.c, ids=[id_], apply=True)
        self.assertEqual(report["tasks"][0]["reason"], "DELETE_FAILED")
        self.assertEqual(self.store.artifact_status(id_)["state"], "DELETE_FAILED")
        self.assertTrue(home.exists())
        self.assertTrue(other.exists())

    def test_cli_skipped_deletion_returns_two(self):
        id_, home = self.task(state="RUNNING")
        with redirect_stdout(io.StringIO()):
            code = main(["--config",str(self.config_path),"prune","--ids",id_,"--apply"])
        self.assertEqual(code, 2)
        self.assertTrue(home.exists())


if __name__ == "__main__":
    unittest.main()
