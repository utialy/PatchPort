import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_bridge import health
from agent_bridge.runner import run
from agent_bridge.storage import FileLock, atomic_json
from agent_bridge.cli import main


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.record = dict(schema=1, pid=123, active=0, started=50,
                           heartbeat=100, code=health.code_identity())

    def write(self, record=None):
        atomic_json(self.state / "health.json", self.record if record is None else record)

    def test_fresh_stale_future_and_boundary(self):
        self.write()
        with FileLock(self.state / "runner.lock"):
            for now, expected in [(100, "FRESH"), (110, "FRESH"), (111, "STALE"), (99, "FUTURE")]:
                with self.subTest(now=now):
                    result = health.inspect(self.state, now)
                    self.assertEqual(result["heartbeat_status"], expected)
                    self.assertEqual(result["heartbeat_age_seconds"], now - 100)
                    self.assertEqual(result["runner"], "LOCKED")
                    self.assertIs(result["restart_required"], False)

    def test_missing_corrupt_and_non_object(self):
        for text in [None, "{", "[]", "null", '"text"', "{\"heartbeat\": NaN}"]:
            with self.subTest(text=text):
                if text is not None:
                    (self.state / "health.json").write_text(text, encoding="utf-8")
                result = health.inspect(self.state, 100)
                self.assertEqual(result["health"], "UNKNOWN")
                self.assertEqual(result["heartbeat_status"], "UNKNOWN")
                self.assertIsNone(result["restart_required"])

    def test_legacy_and_invalid_metadata(self):
        records = [dict(pid=123, heartbeat=100, active=0)]
        for key, value in [("schema", True), ("pid", False), ("active", -1),
                           ("started", 101), ("code", {}), ("heartbeat", "100"),
                           ("heartbeat", float("inf")), ("heartbeat", True)]:
            records.append(dict(self.record, **{key: value}))
        with FileLock(self.state / "runner.lock"):
            for record in records:
                with self.subTest(record=record):
                    self.write(record)
                    result = health.inspect(self.state, 100)
                    self.assertEqual(result["health"], "UNKNOWN")
                    self.assertIsNone(result["restart_required"])

    def test_stopped_retains_record_without_restart_advice(self):
        self.write()
        result = health.inspect(self.state, 100)
        self.assertEqual(result["runner"], "STOPPED")
        self.assertFalse(result["lock_held"])
        self.assertEqual(result["health"], self.record)
        self.assertIsNone(result["restart_required"])

    def test_large_integer_predicate_and_existing_values(self):
        for value in [10**400, -(10**400), True, False, None, "1", [], float("inf"), float("nan")]:
            with self.subTest(value_type=type(value).__name__):
                self.assertIs(health._number(value), False)
        for value in [0, 1, -1, 1.5, 10**20]:
            self.assertIs(health._number(value), True)

    def test_large_heartbeat_and_started_are_unknown(self):
        for field in ("heartbeat", "started"):
            with self.subTest(field=field):
                self.write(dict(self.record, **{field: 10**400}))
                result = health.inspect(self.state, 100)
                self.assertEqual(result["health"], "UNKNOWN")
                self.assertEqual(result["heartbeat_status"], "UNKNOWN" if field == "heartbeat" else "FRESH")

    def test_status_cli_handles_large_json_integer(self):
        self.write(dict(self.record, heartbeat=10**400))
        out = io.StringIO()
        with patch("agent_bridge.cli.load_config", return_value={"state": self.state}), contextlib.redirect_stdout(out):
            self.assertEqual(main(["status"]), 0)
        self.assertEqual(json.loads(out.getvalue())["heartbeat_status"], "UNKNOWN")

    def test_code_and_version_mismatch(self):
        self.write()
        with FileLock(self.state / "runner.lock"):
            for field, value in [("version", "new"), ("code_sha256", "0" * 64), ("code_sha256", None)]:
                with patch.object(health, "code_identity", return_value=dict(self.record["code"], **{field: value})):
                    result = health.inspect(self.state, 100)
                    self.assertIs(result["restart_required"], None if value is None else True)

    def test_fingerprint_tracks_names_bytes_and_additions(self):
        package = self.root / "package"
        package.mkdir()
        file = package / "a.py"
        file.write_text("first", encoding="utf-8")
        first = health.code_identity(package)
        file.write_text("second", encoding="utf-8")
        second = health.code_identity(package)
        self.assertNotEqual(first, second)
        file.rename(package / "b.py")
        third = health.code_identity(package)
        self.assertNotEqual(second, third)
        (package / "a.py").write_text("extra", encoding="utf-8")
        self.assertNotEqual(third, health.code_identity(package))
        with patch.object(Path, "read_bytes", side_effect=PermissionError):
            self.assertIsNone(health.code_identity(package)["code_sha256"])

    def test_runner_captures_identity_once_and_cli_reads_record(self):
        config = dict(state=self.state, parallel=1, endpoints={})
        with patch("agent_bridge.runner.code_identity", return_value=self.record["code"]) as identity, \
             patch("agent_bridge.runner.time.monotonic", side_effect=[3, 3, 6, 6]), \
             patch("agent_bridge.runner.time.sleep"), \
             patch("agent_bridge.runner.Store.rows", side_effect=[[], [{"state": "QUEUED"}], [], []]):
            self.assertEqual(run(config, once=True), 0)
            identity.assert_called_once_with()
        record = json.loads((self.state / "health.json").read_text())
        self.assertEqual(record["code"], self.record["code"])
        self.assertGreaterEqual(record["heartbeat"], record["started"])
        out = io.StringIO()
        with patch("agent_bridge.cli.load_config", return_value=config), contextlib.redirect_stdout(out):
            self.assertEqual(main(["status"]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["runner"], "STOPPED")
        self.assertEqual(result["heartbeat_status"], "FRESH")
        self.assertIn("control", result)
        self.assertEqual(result["counts"], {})


if __name__ == "__main__":
    unittest.main()
