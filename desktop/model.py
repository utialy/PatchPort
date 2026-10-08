"""Presentation state and an explicit project registry; no disk discovery."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile


class Projects:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.items = []
        self._check()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(data, list) or len(data) > 1000
                    or any(not isinstance(p, str) or not Path(p).is_absolute() for p in data)):
                raise ValueError('Invalid project registry; preserve it and select another registry path')
            self.items = list(dict.fromkeys(data))

    def _check(self):
        for path in (self.path, *self.path.parents):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Linked registry paths are not supported')

    def add(self, project):
        project = str(Path(project).resolve())
        if project in self.items:
            return
        if len(self.items) >= 1000:
            raise ValueError('Project registry is full')
        self._check()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        values = self.items + [project]
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.projects-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(values, stream, ensure_ascii=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            self._check()
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.items = values


def status_text(status):
    control = status.get('control') or {}
    counts = status.get('counts') or {}
    record = status.get('management') or {}
    diagnostics = status.get('diagnostics') or {}
    lines = ['Backend: ' + status.get('backend', 'manual'),
             'Runner: ' + (record.get('mode', 'UNKNOWN') if status.get('lock_held') else 'STOPPED'),
             'Heartbeat: ' + status.get('heartbeat_status', 'UNKNOWN'),
             'Dispatch: ' + control.get('dispatch', 'Not initialized'),
             'Calls started: {} / {}'.format(control.get('calls_started', 0),
                 'Unlimited' if control.get('max_calls') is None else control['max_calls']),
             'Queued: {}    Running: {}'.format(
                 sum(n for s, n in counts.items() if s.endswith('QUEUED')),
                 sum(n for s, n in counts.items() if s.endswith('RUNNING'))),
             'Authentication: NOT CHECKED \u2014 sign in through the official CLI if needed.']
    for name, value in diagnostics.get('commands', {}).items():
        if 'shell' in value:
            lines.append(name + ': shell found; profile command NOT CHECKED (' + value['shell'] + ')')
        else:
            lines.append(name + ': ' + ('CLI found' if value['status'] == 'FOUND' else 'CLI missing or unsupported'))
    if status.get('problems'):
        lines.append('Needs attention: ' + ', '.join(status['problems']))
        lines.append('Inspect the details and project CLI diagnostics before another change.')
    return '\n'.join(lines)


def task_values(task):
    """Format a task row; damaged or absent timestamps must not break the window."""
    try:
        created = datetime.fromtimestamp(task['created']).strftime('%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        created = 'Unknown'
    return created, task['endpoint'], task['state'], task['id']


def response_text(reply):
    """Show user decisions and outcomes without transport implementation fields."""
    if not reply:
        return ''
    result = reply.get('result') or {}
    lines = []
    if not reply.get('ok'):
        problem = reply.get('problem') or {}
        lines.extend([problem.get('code', 'UNKNOWN'), problem.get('next_action', ''), ''])
    report = result.get('report', result)
    if 'actions' in report:
        lines.extend(['Project: ' + report.get('project', ''),
                      'Connection: ' + report.get('connection', 'UNKNOWN'),
                      'Inputs: ' + ', '.join(report.get('include', [])),
                      'Writable in original: ' + ', '.join(report.get('writable', [])),
                      '', 'Connection file changes:'])
        lines.extend('  {}  {}'.format(item['action'].upper(), item['path']) for item in report['actions'])
        for name, check in report.get('diagnostics', {}).get('commands', {}).items():
            lines.append(name + ': ' + check['status'])
            if 'shell' in check:
                lines.append('  Shell: ' + check['shell'] + '; command: ' + repr(check['command'])
                             + '; profile: ' + check['profile'] + ' (not loaded; command not checked)')
        lines.extend(['', 'Authentication has not been checked. Setup makes no AI calls.',
                      report.get('note', ''), report.get('next_action', '')])
        if report.get('backup'):
            lines.append('Backup: ' + str(report['backup']))
        if report.get('error'):
            lines.append('Error: ' + str(report['error']))
    elif 'tasks' in result:
        lines.append('Select a task and read its saved result.' if result['tasks'] else 'No tasks in this project yet.')
        if result.get('more'):
            lines.append('Only the most recent tasks are shown. Use Read by ID for an older task.')
    elif 'candidates' in result:
        lines.append('Discovered CLI candidates (not invoked):')
        for candidate in result['candidates']:
            lines.append(json.dumps(candidate, ensure_ascii=False) if not isinstance(candidate, str) else candidate)
        if not result['candidates']:
            lines.append('None found. Install the official CLI or select its executable.')
    elif 'heartbeat_status' in result:
        record = result.get('management') or {}
        lines.extend(['Run ID: ' + record.get('run_id', 'Not started'), result.get('next_action', '')])
        beat = result.get('health')
        if isinstance(beat, dict):
            for name in ('last_error', 'error'):
                if beat.get(name):
                    lines.append('Last error: ' + str(beat[name]))
        if result.get('database_error'):
            lines.append('Database: ' + result['database_error'])
    elif 'answer' in result:
        answer = result['answer'] or {}
        lines.extend(['Task: ' + result.get('id', ''), 'State: ' + result.get('state', 'UNKNOWN'),
                      'Task success is not evaluated automatically.', '', answer.get('text', ''), '',
                      'Stored details:', json.dumps(result, ensure_ascii=False, indent=2)])
    elif result:
        lines.extend('{}: {}'.format(key.replace('_', ' ').capitalize(), value)
                     for key, value in result.items() if key not in ('ok',))
    return '\n'.join(lines)
