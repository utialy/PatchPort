"""Read-only task selection and explicit startup project behavior."""
import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

from desktop.model import Projects, response_text, task_values


class ResultPresentationTests(unittest.TestCase):
    def test_empty_and_truncated_lists_explain_next_action(self):
        self.assertIn('No tasks', response_text(dict(ok=True, result=dict(tasks=[], more=False))))
        self.assertIn('Read by ID', response_text(dict(ok=True, result=dict(tasks=[{}], more=True))))
        for created in (None, 'bad', float('nan'), float('inf'), 10**100):
            row = dict(created=created, endpoint='claude', state='DONE', id='task--claude')
            self.assertEqual(task_values(row), ('Unknown', 'claude', 'DONE', 'task--claude'))


class FakeWorker:
    def __init__(self):
        self.busy = False
        self.calls = []
        self.events = queue.Queue()

    def submit(self, action, args):
        self.calls.append((action, args))
        self.busy = True

    def reconnect(self):
        self.busy = False

    def close(self):
        pass


@unittest.skipUnless(importlib.util.find_spec('tkinter'), 'Tkinter is unavailable')
class ResultWindowTests(unittest.TestCase):
    def setUp(self):
        from desktop import app
        self.module = app
        try:
            self.root = app.tk.Tk()
        except app.tk.TclError:
            self.skipTest('A Tk display is unavailable')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.project = self.directory/'project'
        self.project.mkdir()
        (self.project/'bridge.json').write_text('{}')
        self.worker = FakeWorker()
        self.app = app.Application(self.root, self.worker, Projects(self.directory/'projects.json'))
        self.app.select_project(self.project)

    def load_tasks(self):
        self.app.refresh_tasks()
        action, args = self.worker.calls[-1]
        self.assertEqual(action, 'tasks.list')
        self.worker.busy = False
        self.app.handle_reply(action, dict(ok=True, result=dict(tasks=[
            dict(id='saved--claude', endpoint='claude', state='DONE', created=0)], more=False)), None)
        self.app.update_controls()

    def test_selected_result_reads_exact_id_and_busy_blocks_reentry(self):
        self.load_tasks()
        self.assertEqual(self.app.task_tree.selection(), ('saved--claude',))
        self.app.read_selected_result()
        self.assertEqual(self.worker.calls[-1], ('result', dict(config=str(self.project/'bridge.json'), task_id='saved--claude')))
        count = len(self.worker.calls)
        self.app.read_selected_result()
        self.assertEqual(len(self.worker.calls), count)
        self.assertEqual(str(self.app.read_task_button.cget('state')), 'disabled')

    def test_project_change_clears_rows_and_rejects_late_response(self):
        self.load_tasks()
        self.app.read_selected_result()
        other = self.directory/'other'
        other.mkdir()
        (other/'bridge.json').write_text('{}')
        self.app.select_project(other)
        self.assertFalse(self.app.task_tree.get_children())
        self.worker.busy = False
        self.app.handle_reply('result', dict(ok=True, result=dict(answer=dict(text='OLD PROJECT ANSWER'))), None)
        self.assertNotIn('OLD PROJECT ANSWER', self.app.status_output.get('1.0', 'end'))
        with self.assertRaises(ValueError):
            self.app.read_selected_result()

    def test_refresh_or_connection_error_clears_saved_selection(self):
        self.load_tasks()
        self.app.refresh_tasks()
        self.assertFalse(self.app.task_tree.get_children())
        self.assertIsNone(self.app.task_config)
        self.worker.busy = False
        self.app.handle_reply('tasks.list', None, 'Connection closed')
        self.assertFalse(self.app.task_tree.get_children())
        self.assertIn('Connection unavailable', self.app.task_note.get())

    def test_startup_explicit_project_only_requests_readonly_refresh(self):
        for configured in (True, False):
            if not configured:
                (self.project/'bridge.json').unlink()
            with self.subTest(configured=configured), patch.object(self.module.tk, 'Tk') as root, \
                    patch.object(self.module, 'Application') as application, \
                    patch.object(self.module, 'Worker'), \
                    patch.object(sys, 'argv', ['PatchPort', '--core', sys.executable, '--registry',
                                             str(self.directory/'other-registry.json'), '--project', str(self.project)]):
                self.assertEqual(self.module.main(), 0)
                application.return_value.select_project.assert_called_once_with(self.project.resolve())
                self.assertEqual(application.return_value.refresh.call_count, int(configured))
                application.return_value.start.assert_not_called()
                root.return_value.mainloop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
