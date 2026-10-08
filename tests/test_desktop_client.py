import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest

from desktop.client import ConnectionLost, CoreClient, Worker, bundled_core
from desktop.model import Projects, response_text, status_text

ROOT = Path(__file__).resolve().parents[1]


class DesktopClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project spaces \ud55c\uae00'
        self.project.mkdir()
        (self.project / 'input.txt').write_text('Original', encoding='utf-8')
        self.config = self.project / 'bridge.json'
        self.config.write_text(json.dumps(dict(project='.', state='.agent-bridge', include=['input.txt'],
            writable=['input.txt'], parallel=1, timeout=10,
            endpoints={'mock': dict(adapter='command', command=[sys.executable], parallel=1)})), encoding='utf-8')

    def client(self, **kwargs):
        client = CoreClient(sys.executable, source=ROOT / 'src', **kwargs)
        def cleanup():
            client.close()
            client.process.wait(timeout=10)
        self.addCleanup(cleanup)
        return client

    def test_real_pipe_preview_apply_controls_and_eof(self):
        client = self.client()
        preview = client.request('setup.preview', dict(project=str(self.project)))
        self.assertTrue(preview['ok'], preview)
        self.assertFalse((self.project / '.agent-bridge').exists())
        result = client.request('setup.apply', dict(plan_id=preview['result']['plan_id'], apply=True))
        self.assertTrue(result['ok'], result)
        status = client.request('runner.status', dict(config=str(self.config)))
        self.assertTrue(status['ok'], status)
        self.assertFalse((self.project / '.agent-bridge').exists())
        for action, extra in [('pause', {}), ('limit', {'max_calls': 0}), ('resume', {})]:
            self.assertTrue(client.request(action, dict(config=str(self.config), apply=True, **extra))['ok'])
        status = client.request('runner.status', dict(config=str(self.config)))['result']
        self.assertIn('LIMIT_REACHED', status_text(status))
        self.assertIn('NOT CHECKED', status_text(status))
        client.close()
        self.assertEqual(client.process.wait(timeout=10), 0)
        with self.assertRaises(ConnectionLost):
            client.request('runner.status', dict(config=str(self.config)))

    def test_runner_survives_window_transport_close(self):
        client = self.client()
        client.request('limit', dict(config=str(self.config), max_calls=0, apply=True))
        started = client.request('runner.start', dict(config=str(self.config), apply=True))
        self.assertTrue(started['ok'], started)
        run_id = started['result']['run_id']
        other = self.client()
        try:
            client.close()
            client.process.wait(timeout=10)
            status = other.request('runner.status', dict(config=str(self.config)))['result']
            self.assertTrue(status['lock_held'])
            self.assertEqual(status['management']['run_id'], run_id)
            stopped = other.request('runner.stop', dict(config=str(self.config), run_id=run_id, apply=True))
            self.assertTrue(stopped['ok'], stopped)
        finally:
            other.request('runner.stop', dict(config=str(self.config), run_id=run_id, apply=True, cancel_active=True))

    def test_response_timeout_never_retries(self):
        from unittest.mock import patch
        client = self.client()
        with patch.object(client.replies, 'get', side_effect=queue.Empty):
            with self.assertRaises(ConnectionLost):
                client.request('pause', dict(config=str(self.config), apply=True))
        self.assertTrue(client.closed)
        client.process.wait(timeout=10)

    def test_mismatched_response_invalidates_session(self):
        client = self.client()
        client.replies.put(dict(protocol=1, id='different-session', ok=True, result={}, problem=None))
        with self.assertRaises(ConnectionLost):
            client.request('runner.status', dict(config=str(self.config)))
        self.assertTrue(client.closed)
        self.assertFalse((self.project / '.agent-bridge').exists())

    def test_oversized_request_is_rejected_before_write(self):
        client = self.client()
        with self.assertRaises(ValueError):
            client.request('pause', dict(config=str(self.config), apply=True, extra='x' * 65536))
        self.assertFalse((self.project / '.agent-bridge').exists())
        self.assertFalse(client.closed)

    def test_partial_connection_shows_backup_and_next_action(self):
        text = response_text(dict(protocol=1, id='transport-only', ok=False,
            problem=dict(code='OPERATION_INCOMPLETE', next_action='Inspect backup before applying'),
            result=dict(project='project', connection='PARTIAL', actions=[dict(action='create', path='bridge.py')],
                        include=['input.txt'], writable=['input.txt'], backup='saved-backup', error='Changed file')))
        for expected in ('PARTIAL', 'saved-backup', 'CREATE', 'bridge.py', 'Changed file', 'Inspect backup'):
            self.assertIn(expected, text)
        self.assertNotIn('transport-only', text)

    def test_registry_is_explicit_and_preserves_invalid_file(self):
        path = self.root / 'settings/projects.json'
        projects = Projects(path)
        self.assertFalse(path.exists())
        projects.add(self.project)
        projects.add(self.project)
        self.assertEqual(Projects(path).items, [str(self.project.resolve())])
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            Projects(path)
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_manifest_rejects_escape(self):
        (self.root / 'package.json').write_text(json.dumps(dict(schema=1, core_python='../python.exe')))
        with self.assertRaises(ValueError):
            bundled_core(self.root)
        (self.root / 'core').mkdir()
        core = self.root / 'core/python.exe'
        core.write_bytes(b'test')
        (self.root / 'package.json').write_text(json.dumps(dict(schema=1, core_python='core/python.exe')))
        self.assertEqual(bundled_core(self.root), core.resolve())

    def test_worker_serializes(self):
        worker = Worker(lambda: self.client())
        worker.submit('runner.status', dict(config=str(self.config)))
        with self.assertRaises(RuntimeError):
            worker.submit('pause', {})
        action, reply, error = worker.events.get(timeout=15)
        self.assertEqual(action, 'runner.status')
        self.assertIsNone(error)
        self.assertTrue(reply['ok'])
        worker.busy = False
        worker.close()
        with self.assertRaises(RuntimeError):
            worker.submit('runner.status', {})

    def test_unknown_and_missing_cli_are_not_ready(self):
        text = status_text(dict(lock_held=True, heartbeat_status='STALE', problems=['IDENTITY_MISMATCH'],
                               diagnostics={'commands': {'codex': {'status': 'MISSING_OR_UNSUPPORTED'}}}))
        for expected in ('UNKNOWN', 'STALE', 'IDENTITY_MISMATCH', 'missing or unsupported', 'NOT CHECKED'):
            self.assertIn(expected, text)


if __name__ == '__main__':
    unittest.main()
