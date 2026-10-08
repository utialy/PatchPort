import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_bridge.storage import Store


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.store.register_runner(dict(protocol=1, run_id='first', mode='RUNNING'))
        self.store.submit('work', ['mock'], 'test', {'mock': {}})

    def test_stop_before_claim_and_no_anonymous_claim(self):
        self.assertFalse(self.store.claim('work--mock'))
        self.store.runner_transition('first', mode='DRAINING')
        self.assertFalse(self.store.claim('work--mock', 'first'))
        self.assertEqual(self.store.control()['calls_started'], 0)

    def test_claim_before_stop_preserves_count(self):
        self.assertTrue(self.store.claim('work--mock', 'first'))
        self.store.runner_transition('first', mode='DRAINING')
        self.assertEqual(self.store.control()['calls_started'], 1)
        self.assertEqual(self.store.rows()[0]['state'], 'RUNNING')

    def test_monotonic_stop_and_old_identity_rejected(self):
        self.store.runner_transition('first', mode='CANCELLING')
        self.store.runner_transition('first', mode='DRAINING')
        self.assertEqual(self.store.lifecycle()['mode'], 'CANCELLING')
        with self.assertRaises(ValueError): self.store.runner_transition('old', mode='DRAINING')
        self.store.runner_transition('first', mode='STOPPED')
        self.assertFalse(self.store.claim('work--mock', 'first'))

    def test_stop_claim_race_has_one_serial_outcome(self):
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            return self.store.claim('work--mock', 'first')
        def stop():
            barrier.wait()
            return self.store.runner_transition('first', mode='DRAINING')
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(claim), pool.submit(stop)
            claimed = a.result()
            b.result()
        self.assertEqual(self.store.control()['calls_started'], int(claimed))
        self.assertFalse(self.store.claim('work--mock', 'first'))
