"""Read-only project and role observations; never dispatches or promotes work."""
import argparse
import contextlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from agent_bridge.storage import load_config
from agent_bridge import workspace
from agent_bridge.usage import report, totals
from flow_archive import is_archived, load_view


class Snapshot:
    def __init__(self, db):
        self.db = db

    @contextlib.contextmanager
    def connect(self):
        yield self.db
        self.db.rollback()


def observe(state):
    """Copy an existing DB into memory; schema compatibility never writes to disk."""
    path = workspace.safe(state, 'queue.sqlite3')
    if not path.is_file():
        return dict(observation='MISSING', control=None, tasks=[], usage=None)
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            deadline = time.monotonic() + 5
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError('Database snapshot timed out; no retry or dispatch performed')
            source.backup(db, pages=128, progress=progress, sleep=0.05)
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS usage_snapshots (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.commit()
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            control = None
            if 'control' in tables:
                row = db.execute('SELECT paused,max_calls,calls_started FROM control WHERE id=1').fetchone()
                if row:
                    control = dict(row)
                    control['paused'] = bool(control['paused'])
                    control['remaining'] = None if control['max_calls'] is None else max(0, control['max_calls'] - control['calls_started'])
                    control['dispatch'] = 'PAUSED' if control['paused'] else 'LIMIT_REACHED' if control['remaining'] == 0 else 'READY'
            tasks = []
            roles = {r['id']: dict(flow=r['flow'], role=r['role']) for r in db.execute('SELECT id,flow,role FROM role_tasks')} if 'role_tasks' in tables else {}
            for row in db.execute('SELECT id,state,result FROM tasks ORDER BY created,id'):
                result = json.loads(row['result']) if row['result'] else {}
                if not isinstance(result, dict):
                    raise ValueError('Expected task result object')
                tasks.append(dict(id=row['id'], state=row['state'].removeprefix('PLAN_').removeprefix('ROLE_'),
                                  role_identity=roles.get(row['id']),
                                  answer=result.get('answer'), changes=result.get('changes'),
                                  task_success='NOT_EVALUATED', execution_liveness='UNKNOWN'))
            usage = report(Snapshot(db))
            for row in usage['tasks']:
                row['role_identity'] = roles.get(row['id'])
            return dict(observation='READ', control=control, tasks=tasks, usage=usage)


def overview(project):
    project = Path(project).resolve()
    config = load_config(project / 'bridge.json')
    if config['root'] != project:
        raise ValueError('Overview requires the configured project root')
    if Path.cwd().resolve().is_relative_to(config['state'] / 'workspaces') or Path.cwd().resolve().is_relative_to(project / '.role-flows'):
        raise ValueError('Workers cannot inspect original project workflows')
    errors, records, seen = [], [], set()

    def read(state, scope, flow_id=None, role=None):
        key = str(Path(state).resolve()).casefold() if sys.platform == 'win32' else str(Path(state).resolve())
        if key in seen:
            errors.append(dict(scope=scope, flow_id=flow_id, role=role, error='Duplicate state excluded'))
            return dict(observation='DUPLICATE', control=None, tasks=[], usage=None)
        seen.add(key)
        try:
            value = observe(state)
            for task in value['tasks']:
                identity = task.get('role_identity')
                task['identity'] = ['role', identity['flow'], identity['role'], task['id']] if scope == 'project' and identity else [scope, flow_id, role, task['id']]
            if value['usage'] is not None:
                for row in value['usage']['tasks']:
                    identity = row.get('role_identity')
                    records.append(dict(row, identity=['role', identity['flow'], identity['role'], row['id']] if scope == 'project' and identity else [scope, flow_id, role, row['id']]))
            return value
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            errors.append(dict(scope=scope, flow_id=flow_id, role=role, error=str(exc)))
            return dict(observation='ERROR', control=None, tasks=[], usage=None)

    parent = read(config['state'], 'project')
    flows = []
    root = workspace.safe(project, '.role-flows')
    entries = {p.name: p for p in root.iterdir()} if root.exists() else {}
    for task in parent['tasks']:
        if task.get('role_identity'):
            flow_id = task['role_identity']['flow']
            entries.setdefault(flow_id, root / flow_id)
    for entry in sorted(entries.values()):
        shared = any(t.get('role_identity', {}).get('flow') == entry.name for t in parent['tasks'] if t.get('role_identity'))
        archive_view = None
        archived = False
        try:
            flow = workspace.safe(project, '.role-flows/' + entry.name)
            if not flow.is_dir() and not shared:
                continue
            saved = json.loads(workspace.safe(flow, 'result.json').read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise ValueError('Expected role result object')
            shared = shared or saved.get('queue_mode') == 'SHARED_PARENT'
            archived = is_archived(saved)
            if archived:
                archive_view = load_view(project, entry.name)
                saved = archive_view['records']
                if (saved['queue_mode'] == 'SHARED_PARENT') != shared:
                    raise ValueError('Archive queue mode disagrees with parent role identity')
            phase = saved['original_phase'] if archived else saved.get('state', 'UNKNOWN')
            item = dict(id=entry.name, saved_phase=phase, execution_liveness='UNKNOWN',
                        review_is_approval=False, task_success='NOT_EVALUATED',
                        user_action='REVIEW_RESULTS' if phase == 'REVIEW_DONE' else 'INSPECT_STATUS_OR_FAILURE',
                        checks=saved.get('checks'), error=saved.get('error'),
                        original_changed_since_snapshot=saved.get('original_changed_since_snapshot'))
            if archived:
                item.update(artifact_source='ARCHIVE', artifact_state=archive_view['artifact_state'],
                            evidence_status='AVAILABLE', current_files_checked=False,
                            archived_roles=saved['roles'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            archive_view = None
            errors.append(dict(flow_id=entry.name, error=str(exc)))
            # An unreadable manifest must not silently omit existing role usage.
            try:
                flow = workspace.safe(project, '.role-flows/' + entry.name)
            except ValueError:
                continue
            item = dict(id=entry.name, saved_phase='UNKNOWN', execution_liveness='UNKNOWN',
                        review_is_approval=False, user_action='INSPECT_STATUS_OR_FAILURE')
            if archived:
                item.update(artifact_source='ARCHIVE', artifact_state='UNKNOWN',
                            evidence_status='MISSING' if isinstance(exc, FileNotFoundError) else 'CORRUPT',
                            current_files_checked=False)
        item['roles'] = {}
        for role, relative in (('developer', 'developer-state'), ('reviewer', 'review-input/.agent-bridge')):
            if shared:
                tasks = [t for t in parent['tasks'] if t.get('role_identity') == dict(flow=entry.name, role=role)]
                usage = [r for r in records if r['identity'][:3] == ['role', entry.name, role]]
                item['roles'][role] = dict(observation='READ' if tasks else 'NOT_SUBMITTED',
                                          control=parent['control'], tasks=tasks,
                                          usage=dict(summary=totals(usage), tasks=usage), queue_mode='SHARED_PARENT')
                continue
            try:
                state = workspace.safe(project, '.role-flows/' + entry.name + '/' + relative)
                item['roles'][role] = read(state, 'role', entry.name, role)
            except ValueError as exc:
                errors.append(dict(flow_id=entry.name, role=role, error=str(exc)))
        if archive_view is not None:
            for role, expected in archive_view['records']['roles'].items():
                observed = item['roles'].get(role, {})
                tasks = observed.get('tasks', [])
                if expected is None:
                    if observed.get('observation') == 'MISSING':
                        observed['observation'] = 'NOT_SUBMITTED'
                    if tasks:
                        errors.append(dict(flow_id=entry.name, role=role, error='Archived unsubmitted role has queue tasks'))
                elif observed.get('observation') in ('READ', 'NOT_SUBMITTED'):
                    if len(tasks) != 1 or (tasks[0]['id'], tasks[0]['state']) != (expected['task'], expected['state']):
                        errors.append(dict(flow_id=entry.name, role=role, error='Archive role identity/state disagrees with queue'))
        flows.append(item)
    missing = parent['observation'] != 'READ' or any(
        role['observation'] not in ('READ', 'NOT_SUBMITTED') for flow in flows for role in flow['roles'].values())
    return dict(schema=1, observed_at=time.time(), consistency='PER_DATABASE_SNAPSHOT',
                project=parent, flows=flows, errors=errors,
                usage=dict(summary=totals(records), tasks=records, coverage_complete=not errors and not missing,
                           cost_basis='provider-reported USD, not subscription bill or remaining quota'),
                control_scope='SHARED_PARENT for new roles; historical private states remain separate')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = overview(args.project)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
