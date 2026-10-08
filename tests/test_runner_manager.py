import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_bridge.runner_manager import checked_config, inspect, start, stop
from agent_bridge.storage import Store, atomic_json, FileLock


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge runner ')
        self.root = Path(self.temp.name)
        (self.root / 'input.txt').write_text('test')
        self.path = self.root / 'bridge.json'
        self.raw = dict(project='.', include=['input.txt'], writable=['input.txt'], parallel=1,
                        timeout=15, endpoints={'mock': dict(adapter='command',
                            command=[sys.executable, '-c', 'import time; time.sleep(.5); print("mock reply")'])})
        atomic_json(self.path, self.raw)
        self.config = checked_config(self.path)
        self.run_id = None

    def tearDown(self):
        if self.run_id:
            stop(self.config, self.run_id, cancel=True, timeout=10)
        # A foreign-parent runner can report STOPPED just before Windows closes
        # its final log handle. Wait only for that bounded cleanup condition.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temp.cleanup()
                break
            except PermissionError as exc:
                if os.name != 'nt' or getattr(exc, 'winerror', None) != 32 or time.monotonic() >= deadline:
                    raise
                time.sleep(.05)

    def launch(self):
        result, code = start(self.config)
        self.run_id = result['run_id']
        self.assertEqual(code, 0, result)
        return result

    def until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.05)
        self.fail('Timed out waiting for mock runner')

    def test_preview_creates_nothing_and_old_lock_is_not_running(self):
        result = inspect(self.config)
        self.assertEqual(result['runner'], 'STOPPED')
        self.assertFalse(self.config['state'].exists())

    def test_unknown_database_and_unconfirmed_launch_refuse_start(self):
        self.config['state'].mkdir()
        database = self.config['state'] / 'queue.sqlite3'
        database.write_bytes(b'broken')
        self.assertIn('DATABASE_UNKNOWN', inspect(self.config)['problems'])
        with self.assertRaises(ValueError): start(self.config)
        database.unlink()
        atomic_json(self.config['state'] / 'runner-launch.json', dict(run_id='pending', outcome='PENDING'))
        self.assertIn('LAUNCH_UNCONFIRMED', inspect(self.config)['problems'])
        with self.assertRaises(ValueError): start(self.config)

    def test_spawn_failure_is_recorded_without_claim(self):
        store = Store(self.config['state'])
        with patch('agent_bridge.child.start_background', side_effect=OSError('spawn denied')):
            with self.assertRaises(OSError): start(self.config)
        receipt = json.loads((self.config['state'] / 'runner-launch.json').read_text())
        self.assertEqual(receipt['outcome'], 'FAILED')
        self.assertEqual(store.control()['calls_started'], 0)
        self.launch()

    def test_invalid_receipt_is_diagnostic_and_not_retried(self):
        self.config['state'].mkdir()
        atomic_json(self.config['state'] / 'runner-launch.json', [])
        self.assertIn('LAUNCH_UNKNOWN', inspect(self.config)['problems'])
        with self.assertRaises(ValueError): start(self.config)

    def test_start_identity_mismatch_never_claims_or_recovers(self):
        from agent_bridge.runner import run
        from agent_bridge.runner_manager import execution_identity
        store = Store(self.config['state'])
        store.submit('first', ['mock'], 'test', self.raw['endpoints'])
        expected = dict(run_id='invalid', identity=dict(execution_identity(self.config), python='other'))
        with self.assertRaises(ValueError): run(self.config, once=True, expected=expected)
        self.assertEqual(store.rows()[0]['state'], 'QUEUED')
        self.assertEqual(store.control()['calls_started'], 0)
        self.assertIsNone(store.lifecycle())

    def test_stale_future_and_mismatched_health_never_reuse(self):
        self.launch()
        path = self.config['state'] / 'health.json'
        original = json.loads(path.read_text())
        for age, expected in ((20, 'STALE'), (-20, 'FUTURE')):
            record = dict(original, heartbeat=time.time() - age, started=time.time() - 40)
            atomic_json(path, record)
            self.assertIn(expected, inspect(self.config)['problems'])
            with self.assertRaises(ValueError): start(self.config)
        atomic_json(path, original)

    def test_changed_config_rejected_before_spawn(self):
        self.raw['timeout'] = 13
        atomic_json(self.path, self.raw)
        with self.assertRaises(ValueError): start(self.config)
        self.assertFalse(self.config['state'].exists())

    def test_crash_then_new_run_preserves_interrupted_and_count(self):
        self.raw['endpoints']['mock']['command'][-1] = 'import time; time.sleep(10)'
        atomic_json(self.path, self.raw)
        self.config = checked_config(self.path)
        store = Store(self.config['state'])
        store.submit('crash', ['mock'], 'test', self.raw['endpoints'])
        self.launch()
        self.until(lambda: store.rows()[0]['state'] == 'RUNNING')
        from agent_bridge.runner_manager import _CHILDREN
        owned = _CHILDREN[self.run_id]
        owned.kill()
        owned.wait(timeout=10)
        previous = self.run_id
        self.run_id = None
        self.launch()
        self.assertNotEqual(self.run_id, previous)
        self.assertEqual(store.rows()[0]['state'], 'INTERRUPTED')
        self.assertEqual(store.control()['calls_started'], 1)
        with self.assertRaises(ValueError): stop(self.config, previous)

    def test_reuse_pause_limit_and_explicit_stop(self):
        store = Store(self.config['state'])
        store.pause()
        store.set_limit(0)
        self.launch()
        result, code = start(self.config)
        self.assertEqual((result['outcome'], code), ('REUSED', 0))
        status = inspect(self.config)
        self.assertIn('PAUSED', status['problems'])
        self.assertIn('LIMIT_REACHED', status['problems'])
        result, code = stop(self.config, self.run_id)
        self.assertEqual(code, 0, result)
        self.assertTrue(store.control()['paused'])
        self.assertEqual(store.control()['max_calls'], 0)

    def test_drain_keeps_queued_work_and_count(self):
        store = Store(self.config['state'])
        store.submit('first', ['mock'], 'test', self.raw['endpoints'])
        store.submit('second', ['mock'], 'test', self.raw['endpoints'])
        self.launch()
        self.until(lambda: store.rows('first')[0]['state'] == 'RUNNING')
        result, code = stop(self.config, self.run_id)
        self.assertEqual(code, 0, result)
        self.assertEqual([r['state'] for r in store.rows()], ['DONE', 'QUEUED'])
        self.assertEqual(store.control()['calls_started'], 1)

    def test_cancel_pending_then_observe_and_config_changed_stop(self):
        self.raw['endpoints']['mock']['command'][-1] = 'import time; time.sleep(10)'
        atomic_json(self.path, self.raw)
        self.config = checked_config(self.path)
        store = Store(self.config['state'])
        store.submit('first', ['mock'], 'test', self.raw['endpoints'])
        self.launch()
        self.until(lambda: store.rows()[0]['state'] == 'RUNNING')
        self.raw['timeout'] = 14
        atomic_json(self.path, self.raw)
        self.config = checked_config(self.path)
        with self.assertRaises(ValueError): start(self.config)
        result, code = stop(self.config, self.run_id, cancel=True, timeout=.001)
        self.assertIn(code, (0, 2))
        self.until(lambda: store.rows()[0]['state'] == 'CANCELLED')
        self.assertEqual(stop(self.config, self.run_id)[1], 0)
        self.assertEqual(store.control()['calls_started'], 1)

    def test_foreign_lock_and_old_id_do_not_stop(self):
        with FileLock(self.config['state'] / 'runner.lock'):
            with self.assertRaises(ValueError): start(self.config)
        self.launch()
        with self.assertRaises(ValueError): stop(self.config, 'old')
        self.assertTrue(inspect(self.config)['lock_held'])

    def test_parent_exit_and_concurrent_start_single_runner(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        argv = [sys.executable, '-m', 'agent_bridge', '--config', str(self.path), 'runner', 'start', '--apply']
        processes = [subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        results = [p.communicate(timeout=20) for p in processes]
        successful = [json.loads(out) for p, (out, err) in zip(processes, results) if p.returncode == 0]
        self.assertTrue(successful, results)
        self.run_id = successful[0]['run_id']
        self.assertEqual({r['run_id'] for r in successful}, {self.run_id})
        store = Store(self.config['state'])
        store.submit('after', ['mock'], 'test', self.raw['endpoints'])
        self.until(lambda: store.rows()[0]['state'] == 'DONE')
        self.assertEqual(store.control()['calls_started'], 1)

    def test_foreground_transition_preserves_confirmed_launch_receipt(self):
        from agent_bridge.runner import run
        self.launch()
        self.assertEqual(stop(self.config, self.run_id)[1], 0)
        self.run_id = None
        receipt = self.config['state'] / 'runner-launch.json'
        original = receipt.read_bytes()
        self.assertEqual(run(self.config, once=True), 0)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertNotIn('LAUNCH_UNCONFIRMED', inspect(self.config)['problems'])
        self.launch()

    def test_unconfirmed_receipt_blocks_foreground_claims(self):
        from agent_bridge.runner import run
        store = Store(self.config['state'])
        store.submit('pending', ['mock'], 'test', self.raw['endpoints'])
        atomic_json(self.config['state'] / 'runner-launch.json', dict(run_id='unconfirmed', outcome='PENDING'))
        with self.assertRaises(ValueError):
            run(self.config, once=True)
        self.assertEqual(store.control()['calls_started'], 0)
        self.assertEqual(store.rows()[0]['state'], 'QUEUED')
