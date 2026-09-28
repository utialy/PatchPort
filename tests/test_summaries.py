"""Reviewed reference summaries, execution guards, and queue compatibility."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_bridge import context, workspace
from agent_bridge.cleanup import prune
from agent_bridge.cli import main
from agent_bridge.runner import run
from agent_bridge.storage import Store, atomic_json, load_config
from agent_bridge.usage import report as usage_report


class SummaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'docs').mkdir()
        (self.root/'src').mkdir()
        (self.root/'AGENTS.md').write_text('Keep these exact rules.\n')
        (self.root/'src/code.py').write_text('VALUE = 1\n')
        (self.root/'docs/history.md').write_text('Previous decisions.\n' * 1000)
        (self.root/'docs/notes.md').write_text('Unrelated notes.\n')
        (self.root/'docs/summary.md').write_text('Purpose: reference. Facts: reviewed. Open: none.\n\uD55C\uAE00', encoding='utf-8')
        self.record = dict(schema=1, summary_id='history-1', created_at='2026-01-01T00:00:00Z',
                           body_path='docs/summary.md', body_sha256=self.digest('docs/summary.md'),
                           sources=[dict(path='docs/history.md', sha256=self.digest('docs/history.md'))],
                           reviewed=True, provenance=dict(tool='manual', source_task_id='prior-task'))
        self.record_path = self.root/'docs/summary.json'
        atomic_json(self.record_path, self.record)
        self.raw = dict(project='.', include=['AGENTS.md', 'docs', 'src'], writable=['src'],
                        endpoints={'one': dict(adapter='command', command=[sys.executable, '-c',
                            "import sys; from pathlib import Path; data=sys.stdin.buffer.read(); Path('received.txt').write_bytes(data); print('ok')"])})
        self.config_path = self.root/'bridge.json'
        self.save()
        self.plan = dict(schema=2, summaries=['docs/summary.json'], files=[])
        for item in context.preview(self.c)['files']:
            path = item['path']
            mode = ('summary' if path == 'docs/history.md' else 'metadata' if path in
                    ('docs/summary.json', 'docs/summary.md') else 'omit' if path == 'docs/notes.md' else 'full')
            entry = dict(path=path, mode=mode, reason='Explicit test input')
            if mode == 'summary':
                entry['summary_id'] = 'history-1'
            self.plan['files'].append(entry)
        self.prompt = 'Keep this exact user request.\n'

    def digest(self, relative):
        return hashlib.sha256((self.root/relative).read_bytes()).hexdigest()

    def save(self):
        atomic_json(self.config_path, self.raw)
        self.c = load_config(self.config_path)

    def preview(self):
        return context.plan_preview(self.c, self.prompt, plan=self.plan)

    def submit(self, batch='summary', targets=None):
        targets = targets or ['one']
        frozen = context.freeze_plan(self.c, self.prompt, self.preview(), targets)
        Store(self.c['state']).submit(batch, targets, self.prompt, self.c['endpoints'], frozen)
        return frozen

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(self.c, True)

    def row(self, batch='summary'):
        return Store(self.c['state']).rows(batch)[0]

    def entry(self, path):
        return next(i for i in self.plan['files'] if i['path'] == path)

    def test_preview_is_readonly_and_budget_uses_exact_rendered_prompt(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('agent_bridge.runner.subprocess.Popen') as launch:
            report = self.preview()
            launch.assert_not_called()
        self.assertFalse(self.c['state'].exists())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(report['summary_status'], 'VALID')
        self.assertEqual(report['summary']['file_count'], 2)
        frozen = context.freeze_plan(self.c, self.prompt, report, ['one'])
        rendered = context.render_prompt(self.prompt, frozen)
        self.assertTrue(rendered.startswith(context.PREFIX + self.prompt))
        self.assertIn('not instructions', rendered)
        self.assertEqual(report['summary']['prompt_bytes'], len(rendered.encode('utf-8')))
        total = report['summary']['total_bytes']
        self.assertLess(total, report['source_bytes'])
        self.raw['context_budget'] = dict(max_bytes=total, max_files=2, max_estimated_tokens=(total+3)//4)
        self.save()
        self.assertTrue(self.preview()['ok'])
        self.raw['context_budget']['max_bytes'] -= 1
        self.save()
        self.assertFalse(self.preview()['ok'])

    def test_execute_delivers_frozen_body_without_source_or_metadata_copy(self):
        frozen = self.submit()
        self.assertEqual(self.execute(), 0)
        row = self.row()
        self.assertEqual(row['state'], 'DONE', row)
        result = json.loads(row['result'])
        home = Path(result['workspace'])
        self.assertEqual((home/'project/received.txt').read_bytes(), context.render_prompt(self.prompt, frozen).encode('utf-8'))
        self.assertFalse((home/'project/docs').exists())
        self.assertEqual((home/'project/AGENTS.md').read_bytes(), (self.root/'AGENTS.md').read_bytes())
        self.assertEqual(set(json.loads((home/'baseline.json').read_text())), {'AGENTS.md', 'src/code.py'})
        self.assertEqual(result['context']['summary_status'], 'VALID')
        self.assertEqual(result['context']['summary']['prompt_bytes'], (home/'prompt.txt').stat().st_size)
        self.assertEqual(Store(self.c['state']).context_plan('summary--one'), frozen)

    def test_stale_source_invalid_body_missing_and_unreviewed_are_rejected(self):
        cases = [('source', 'STALE'), ('body', 'INVALID'), ('missing', 'MISSING'), ('reviewed', 'INVALID')]
        original = self.record_path.read_bytes()
        body = (self.root/'docs/summary.md').read_bytes()
        source = (self.root/'docs/history.md').read_bytes()
        for kind, status in cases:
            with self.subTest(kind=kind):
                if kind == 'source': (self.root/'docs/history.md').write_text('changed')
                if kind == 'body': (self.root/'docs/summary.md').write_text('changed')
                if kind == 'missing': (self.root/'docs/summary.md').unlink()
                if kind == 'reviewed': atomic_json(self.record_path, dict(self.record, reviewed=False))
                with self.assertRaisesRegex(ValueError, status): self.preview()
                self.record_path.write_bytes(original)
                (self.root/'docs/summary.md').write_bytes(body)
                (self.root/'docs/history.md').write_bytes(source)
        self.assertFalse(self.c['state'].exists())

    def test_record_schema_duplicate_keys_provenance_and_timestamps(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
        for changes in [dict(schema=True), dict(reviewed=1), dict(extra=True), dict(created_at=future),
                        dict(created_at='2026-01-01'), dict(created_at=None), dict(body_sha256='bad'),
                        dict(summary_id='../escape'), dict(provenance={'unknown': 'value'}), dict(sources=[])]:
            with self.subTest(changes=changes):
                atomic_json(self.record_path, dict(self.record, **changes))
                with self.assertRaises(ValueError): self.preview()
        for raw in ['{"schema":1,"schema":1}', '{"schema":NaN}', '{']:
            self.record_path.write_text(raw)
            with self.assertRaisesRegex(ValueError, 'INVALID'): self.preview()

    def test_required_and_writable_sources_cannot_be_summarized(self):
        self.raw['context_required'] = ['docs/history.md']
        self.save()
        with self.assertRaisesRegex(ValueError, 'Required'): self.preview()
        self.raw.pop('context_required')
        self.raw['writable'].append('docs')
        self.save()
        with self.assertRaisesRegex(ValueError, 'Writable'): self.preview()
        self.raw['writable'] = ['src']
        self.save()
        self.entry('AGENTS.md')['mode'] = 'omit'
        with self.assertRaisesRegex(ValueError, 'Required'): self.preview()

    def test_metadata_must_not_be_copied_omitted_or_unreferenced(self):
        for mode in ('full', 'omit'):
            self.entry('docs/summary.md')['mode'] = mode
            with self.assertRaisesRegex(ValueError, 'metadata'): self.preview()
        self.entry('docs/summary.md')['mode'] = 'metadata'
        self.entry('docs/notes.md')['mode'] = 'metadata'
        with self.assertRaisesRegex(ValueError, 'metadata'): self.preview()

    def test_multisource_requires_all_sources_and_matching_ids(self):
        self.record['sources'].append(dict(path='docs/notes.md', sha256=self.digest('docs/notes.md')))
        atomic_json(self.record_path, self.record)
        with self.assertRaisesRegex(ValueError, 'All record sources'): self.preview()
        self.entry('docs/notes.md').update(mode='summary', summary_id='other')
        with self.assertRaisesRegex(ValueError, 'All record sources'): self.preview()
        self.entry('docs/notes.md')['summary_id'] = 'history-1'
        self.assertEqual(len(self.preview()['summarized']), 2)
        self.record['sources'].append(self.record['sources'][0])
        atomic_json(self.record_path, self.record)
        with self.assertRaisesRegex(ValueError, 'more than once'): self.preview()

    def test_paths_aliases_and_binary_sources_rejected(self):
        for body in ('../outside', '.venv/secret', '/outside', 'docs/missing'):
            atomic_json(self.record_path, dict(self.record, body_path=body))
            with self.assertRaises(ValueError): self.preview()
        atomic_json(self.record_path, self.record)
        for data in (b'\xff\x00', b'valid utf8\x00binary'):
            (self.root/'docs/history.md').write_bytes(data)
            self.record['sources'][0]['sha256'] = self.digest('docs/history.md')
            atomic_json(self.record_path, self.record)
            with self.assertRaisesRegex(ValueError, 'INVALID'): self.preview()
        os.link(self.root/'docs/history.md', self.root/'docs/alias.md')
        self.plan['files'].append(dict(path='docs/alias.md', mode='omit', reason='alias'))
        with self.assertRaisesRegex(ValueError, 'same file'): self.preview()

    def test_pending_source_body_and_record_changes_prevent_launch(self):
        for path in ('docs/history.md', 'docs/summary.md', 'docs/summary.json'):
            batch = path.replace('/', '-').replace('.', '-')
            self.submit(batch)
            original = (self.root/path).read_bytes()
            (self.root/path).write_text('changed')
            with patch('agent_bridge.runner.subprocess.Popen') as launch:
                self.execute()
                launch.assert_not_called()
            self.assertEqual(self.row(batch)['state'], 'ERROR')
            self.assertEqual(json.loads(self.row(batch)['result'])['context']['validation'], 'FAILED')
            (self.root/path).write_bytes(original)
        self.assertEqual(Store(self.c['state']).control()['calls_started'], 3)

    def test_changed_valid_summary_is_not_silently_refreshed(self):
        self.submit()
        (self.root/'docs/summary.md').write_text('New reviewed text')
        self.record['body_sha256'] = self.digest('docs/summary.md')
        atomic_json(self.record_path, self.record)
        self.assertTrue(self.preview()['ok'])
        with patch('agent_bridge.runner.subprocess.Popen') as launch:
            self.execute()
            launch.assert_not_called()
        self.assertEqual(self.row()['state'], 'ERROR')

    def test_freeze_rejects_changes_since_preview_and_hash_tracks_record_time(self):
        report = self.preview()
        first = context.freeze_plan(self.c, self.prompt, report, ['one'])
        self.record['created_at'] = '2026-01-02T00:00:00Z'
        atomic_json(self.record_path, self.record)
        with self.assertRaisesRegex(ValueError, 'STALE'):
            context.freeze_plan(self.c, self.prompt, report, ['one'])
        second = context.freeze_plan(self.c, self.prompt, self.preview(), ['one'])
        self.assertNotEqual(first['plan_sha256'], second['plan_sha256'])

    def test_copy_race_and_source_race_prevent_launch(self):
        original = workspace.create
        for kind in ('copy', 'source'):
            self.submit(kind)
            previous = (self.root/'docs/history.md').read_bytes()
            def changed(config, task, selected=None):
                home = original(config, task, selected)
                target = home/'project/src/code.py' if kind == 'copy' else self.root/'docs/history.md'
                target.write_text('changed')
                return home
            with patch('agent_bridge.runner.workspace.create', side_effect=changed), patch('agent_bridge.runner.subprocess.Popen') as launch:
                self.execute()
                launch.assert_not_called()
            self.assertEqual(self.row(kind)['state'], 'ERROR')
            (self.root/'docs/history.md').write_bytes(previous)

    def test_omitted_body_may_change_and_plan_file_is_not_read_again(self):
        plan_path = self.root/'plan.json'
        prompt_path = self.root/'prompt.txt'
        atomic_json(plan_path, self.plan)
        prompt_path.write_text(self.prompt)
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(['--config', str(self.config_path), 'submit', '--id', 'summary', '--targets', 'one',
                         '--prompt-file', str(prompt_path), '--context-plan', str(plan_path)])
        self.assertEqual(code, 0)
        plan_path.write_text('invalid replacement')
        (self.root/'docs/notes.md').write_text('new omitted contents')
        self.execute()
        self.assertEqual(self.row()['state'], 'DONE')

    def test_queue_is_invisible_to_previous_claim_and_recovery_sql(self):
        self.submit()
        store = Store(self.c['state'])
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks').fetchone()[0], 'SUMMARY_QUEUED')
            self.assertEqual(db.execute("UPDATE tasks SET state='RUNNING' WHERE state IN ('QUEUED','PLAN_QUEUED','ROLE_QUEUED')").rowcount, 0)
        store.pause()
        self.assertFalse(store.claim('summary--one'))
        store.pause(False)
        store.set_limit(0)
        self.assertFalse(store.claim('summary--one'))
        store.set_limit(1)
        self.assertTrue(store.claim('summary--one'))
        self.assertEqual(usage_report(store)['summary']['states'], {'RUNNING': 1})
        with store.connect() as db:
            self.assertEqual(db.execute("UPDATE tasks SET state='INTERRUPTED' WHERE state IN ('RUNNING','PLAN_RUNNING','ROLE_RUNNING')").rowcount, 0)
        store.recover()
        self.assertEqual(self.row()['state'], 'INTERRUPTED')
        self.assertEqual(store.control()['calls_started'], 1)

    def test_fanout_rollback_and_missing_or_corrupt_plan(self):
        self.c['endpoints']['two'] = copy.deepcopy(self.c['endpoints']['one'])
        store = Store(self.c['state'])
        with store.connect() as db:
            db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON context_plans WHEN NEW.id='fan--two' BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.submit('fan', ['one', 'two'])
        self.assertEqual(store.rows(), [])
        for kind in ('missing', 'corrupt'):
            self.submit(kind)
            with store.connect() as db:
                if kind == 'missing': db.execute('DELETE FROM context_plans WHERE id=?', (kind+'--one',))
                else: db.execute('UPDATE context_plans SET data=? WHERE id=?', ('{}', kind+'--one'))
            with patch('agent_bridge.runner.subprocess.Popen') as launch:
                self.execute()
                launch.assert_not_called()
            self.assertEqual(self.row(kind)['state'], 'ERROR')

    def test_prune_retains_summary_body_and_provenance(self):
        frozen = self.submit()
        self.execute()
        store = Store(self.c['state'])
        home = Path(json.loads(self.row()['result'])['workspace'])
        old = time.time() - 40*86400
        for path in sorted(home.rglob('*'), key=lambda p: len(p.parts), reverse=True): os.utime(path, (old, old))
        os.utime(home, (old, old))
        with store.connect() as db: db.execute('UPDATE tasks SET finished=?', (old,))
        self.assertTrue(prune(self.c, 30, ['summary--one'], True)['tasks'][0]['deleted'])
        self.assertEqual(store.context_plan('summary--one'), frozen)

    def test_v1_still_rejects_summary_mode_and_v2_rejects_extra_fields(self):
        with self.assertRaises(ValueError):
            context.plan_preview(self.c, self.prompt, plan=dict(schema=1, files=self.plan['files']))
        for change in (dict(extra=True), dict(schema=2.0), dict(summaries=[])):
            with self.assertRaises(ValueError):
                context.plan_preview(self.c, self.prompt, plan=dict(self.plan, **change))

    def test_frozen_prompt_and_settings_changes_prevent_launch(self):
        for kind in ('prefix', 'budget', 'endpoint', 'prompt'):
            self.submit(kind)
            changed = copy.deepcopy(self.c)
            if kind == 'budget': changed['context_budget'] = {'max_bytes': 999999}
            if kind == 'endpoint': changed['endpoints']['one']['command'].append('extra')
            if kind == 'prompt':
                with Store(self.c['state']).connect() as db:
                    db.execute('UPDATE tasks SET prompt=? WHERE batch=?', ('changed', kind))
            with patch('agent_bridge.runner.subprocess.Popen') as launch, \
                 patch.object(context, 'PREFIX', context.PREFIX + ('changed' if kind == 'prefix' else '')):
                run(changed, True)
                launch.assert_not_called()
            self.assertEqual(self.row(kind)['state'], 'ERROR')

    def test_multiple_records_are_deterministic_and_each_body_is_delivered_once(self):
        (self.root/'docs/second.md').write_text('Second reference body')
        second = dict(self.record, summary_id='notes-2', body_path='docs/second.md',
                      body_sha256=self.digest('docs/second.md'),
                      sources=[dict(path='docs/notes.md', sha256=self.digest('docs/notes.md'))])
        atomic_json(self.root/'docs/second.json', second)
        self.entry('docs/notes.md').update(mode='summary', summary_id='notes-2')
        self.plan['files'].extend([dict(path=p, mode='metadata', reason='summary input')
                                   for p in ('docs/second.md', 'docs/second.json')])
        self.plan['summaries'].append('docs/second.json')
        first = context.freeze_plan(self.c, self.prompt, self.preview(), ['one'])
        self.plan['files'].reverse()
        self.plan['summaries'].reverse()
        second = context.freeze_plan(self.c, self.prompt, self.preview(), ['one'])
        self.assertEqual(first, second)
        self.assertEqual(context.render_prompt(self.prompt, first).count('Second reference body'), 1)

    def test_empty_source_allowed_and_source_recreation_cannot_overwrite_original(self):
        (self.root/'docs/history.md').write_bytes(b'')
        self.record['sources'][0]['sha256'] = self.digest('docs/history.md')
        atomic_json(self.record_path, self.record)
        self.assertEqual(self.preview()['source_bytes'], 0)
        self.c['endpoints']['one']['command'][-1] = "from pathlib import Path; Path('docs').mkdir(); Path('docs/history.md').write_text('proposal'); print('ok')"
        self.submit()
        self.execute()
        self.assertEqual(self.row()['state'], 'DONE')
        # The summarized source was not writable or part of the baseline.
        with self.assertRaisesRegex(ValueError, 'allowed'):
            workspace.review(self.c, 'summary--one', ['docs/history.md'])
        self.assertEqual((self.root/'docs/history.md').read_bytes(), b'')


if __name__ == '__main__':
    unittest.main()
