"""Read project data summaries without creating databases, locks, or artifacts."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from . import runner_manager
from .storage import checked_local_path, probe_lock, readonly_database

SCHEMA = 1
MAX_ENTRIES = 20000
MAX_DATABASE_BYTES = 64 * 1024 * 1024


def key(path):
    return os.path.normcase(str(Path(path).absolute()))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_info(path):
    path = checked_local_path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return dict(path=str(path), status='absent', bytes=0, revision=None)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Expected a regular file')
    return dict(path=str(path), status='present', bytes=info.st_size,
                revision=fingerprint([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]))


def tree_info(path):
    """Bound filesystem work and report incomplete totals as unknown."""
    result = dict(path=str(path), status='unknown', files=None, directories=None, bytes=None)
    try:
        root = checked_local_path(path)
        if not root.exists():
            return dict(result, status='absent', files=0, directories=0, bytes=0, revision=None)
        pending, entries, files, directories, size = [root], [], 0, 0, 0
        device = root.stat().st_dev
        while pending:
            item = checked_local_path(pending.pop())
            if item != root and not item.is_relative_to(root):
                raise ValueError('Tree boundary changed')
            info = item.stat()
            if info.st_dev != device:
                raise ValueError('Mount boundary')
            entries.append((str(item.relative_to(root)), info.st_ino, info.st_size, info.st_mtime_ns))
            if len(entries) + len(pending) > MAX_ENTRIES:
                return dict(result, reason='SCAN_LIMIT', scanned=len(entries))
            if stat.S_ISDIR(info.st_mode):
                directories += 1
                with os.scandir(item) as children:
                    for child in children:
                        pending.append(Path(child.path))
                        if len(entries) + len(pending) > MAX_ENTRIES:
                            return dict(result, reason='SCAN_LIMIT', scanned=len(entries))
            elif stat.S_ISREG(info.st_mode):
                files += 1
                size += info.st_size
            else:
                raise ValueError('Unsupported file type')
        return dict(result, status='present', files=files, directories=directories, bytes=size,
                    revision=fingerprint(sorted(entries)))
    except (OSError, ValueError):
        return dict(result, reason='UNSAFE_OR_UNREADABLE')


def database_info(path, project, *, batch=False):
    result = dict(path=str(path), status='unknown', bytes=None, batch=None, interactive=None)
    try:
        info = file_info(path)
        result.update(info)
        if info['status'] == 'absent':
            return result
        if info['bytes'] > MAX_DATABASE_BYTES:
            return dict(result, status='unknown', reason='DATABASE_SIZE_LIMIT')
        db = readonly_database(path)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if batch:
                if 'tasks' not in tables:
                    result.update(status='unknown', reason='BATCH_SCHEMA_UNKNOWN')
                else:
                    counts = dict(db.execute('SELECT state,COUNT(*) FROM tasks GROUP BY state'))
                    if any(not isinstance(state, str) for state in counts):
                        raise ValueError('Invalid task state')
                    active = sum(count for state, count in counts.items() if state.endswith(('QUEUED', 'RUNNING')))
                    result['batch'] = dict(tasks=sum(counts.values()), active=active,
                                           usage=db.execute('SELECT COUNT(*) FROM usage_snapshots').fetchone()[0] if 'usage_snapshots' in tables else None)
            if 'interactive_sessions' in tables:
                required = {'interactive_meta', 'interactive_events', 'interactive_usage', 'interactive_deliveries'}
                if not required <= tables or tuple(db.execute('SELECT version FROM interactive_meta WHERE id=1').fetchone() or ()) not in ((1,), (2,)):
                    return dict(result, status='unknown', reason='INTERACTIVE_SCHEMA_UNKNOWN')
                from . import interactive_guard
                if interactive_guard.version(db) == 2:
                    interactive_guard.validate_schema(db)
                collate = ' COLLATE NOCASE' if os.name == 'nt' else ''
                where = 's.project=?' + collate
                sessions = db.execute('SELECT COUNT(*),SUM(finished IS NULL) FROM interactive_sessions s WHERE ' + where, (str(project),)).fetchone()
                events = db.execute('SELECT COUNT(*) FROM interactive_events e JOIN interactive_sessions s ON e.session=s.id WHERE ' + where, (str(project),)).fetchone()[0]
                usage = db.execute('SELECT COUNT(*) FROM interactive_usage u JOIN interactive_sessions s ON u.session=s.id WHERE ' + where, (str(project),)).fetchone()[0]
                deliveries, incomplete = 0, False
                for index, row in enumerate(db.execute('SELECT data FROM interactive_deliveries')):
                    if index >= MAX_ENTRIES:
                        incomplete = True
                        break
                    data = json.loads(row[0])
                    if not isinstance(data, dict) or not isinstance(data.get('project'), str):
                        incomplete = True
                        continue
                    if key(data['project']) == key(project):
                        deliveries += 1
                total = db.execute('SELECT COUNT(*) FROM interactive_sessions').fetchone()[0]
                result['interactive'] = dict(sessions=sessions[0], unfinished=sessions[1] or 0,
                                             events=events, usage=usage, deliveries=None if incomplete else deliveries,
                                             other_project_sessions=total - sessions[0])
                if incomplete:
                    result.update(status='unknown', reason='DELIVERY_SCOPE_UNKNOWN')
            elif not batch:
                result.update(status='unknown', reason='INTERACTIVE_SCHEMA_UNKNOWN')
        finally:
            db.close()
        if file_info(path) != info:
            return dict(result, status='unknown', batch=None, interactive=None, reason='DATABASE_CHANGED')
        return result
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return dict(result, status='unknown', batch=None, interactive=None, reason='DATABASE_UNREADABLE')


def inspect(project, config=None, databases=None, detached=False):
    project = checked_local_path(project)
    if not project.is_dir():
        raise ValueError('Expected a project directory')
    selected = Path(config) if config else project / 'bridge.json'
    report = dict(schema=SCHEMA, project=str(project), scope='selected_config_and_explicit_ledgers',
                  detached=detached, config=None, databases=[], artifacts=[], blockers=[],
                  exclusions=['other_workspaces', 'other_configurations', 'provider_history', 'authentication', 'global_profiles'],
                  byte_basis='logical_file_bytes_not_reclaimable_space', mutation_supported=False)
    state = None
    try:
        report['config'] = file_info(selected)
        if report['config']['status'] == 'present' and not detached:
            loaded = runner_manager.checked_config(selected)
            if loaded['root'] != project:
                raise ValueError('Selected configuration belongs to another project')
            state = loaded['state']
            report['config'].update(sha256=loaded['_source_sha256'], state=str(state))
    except (OSError, ValueError, TypeError, KeyError):
        report['config'] = dict(path=str(selected), status='unknown', bytes=None, reason='CONFIG_UNREADABLE_OR_MISMATCH')
        report['blockers'].append('CONFIG_UNKNOWN')
    candidates = [state / 'queue.sqlite3'] if state else []
    candidates.extend(Path(value) for value in (databases or []))
    seen = set()
    for path in candidates:
        if key(path) in seen:
            continue
        seen.add(key(path))
        batch = state is not None and key(path) == key(state / 'queue.sqlite3')
        item = database_info(path, project, batch=batch)
        report['databases'].append(item)
        if item['status'] == 'unknown':
            report['blockers'].append('DATABASE_UNKNOWN')
        if (item.get('batch') or {}).get('active'):
            report['blockers'].append('BATCH_PENDING_OR_RUNNING')
        if (item.get('interactive') or {}).get('unfinished'):
            report['blockers'].append('INTERACTIVE_UNFINISHED')
    paths = [('role_flows', project / '.role-flows'), ('connection_backups', project / '.bridge-integration-backups')]
    if state:
        paths.extend([('task_copies', state / 'workspaces'), ('role_jobs', state / 'p1-jobs')])
        for name in ('runner.lock', 'runner-management.lock', 'promotion.lock'):
            try:
                if probe_lock(checked_local_path(state / name)) == 'LOCKED':
                    report['blockers'].append('RUNNER_OR_MANAGEMENT_ACTIVE')
            except (OSError, ValueError):
                report['blockers'].append('LOCK_UNKNOWN')
        jobs = tree_info(state / 'p1-jobs')
        if jobs['status'] == 'unknown':
            report['blockers'].append('ROLE_JOBS_UNKNOWN')
        elif jobs['status'] == 'present':
            for receipt in (state / 'p1-jobs').glob('*.json'):
                try:
                    item = file_info(receipt)
                    if item['bytes'] > 1024 * 1024:
                        raise ValueError('Receipt too large')
                    ticket = json.loads(receipt.read_bytes())
                    if ticket.get('state') not in ('FINISHED', 'FAILED') or not ticket.get('finished'):
                        report['blockers'].append('ROLE_JOB_UNCONFIRMED')
                except (OSError, ValueError, TypeError, AttributeError):
                    report['blockers'].append('ROLE_JOBS_UNKNOWN')
    for kind, path in paths:
        report['artifacts'].append(dict(kind=kind, **tree_info(path)))
    report['blockers'] = sorted(set(report['blockers']))
    report['revision'] = fingerprint(report)
    return report
