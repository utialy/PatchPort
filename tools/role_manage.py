"""Explicit role promotion, recovery and artifact cleanup; never submits AI work."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from agent_bridge import workspace
from agent_bridge.answers import preserve_before_prune
from agent_bridge.cleanup import candidate, copy_guard, delete_artifacts, inspect_tree
from agent_bridge.storage import FileLock, Store, checked_local_path, configure_output, identifier, probe_lock, readonly_database
from role_review import inspect, resolve, safe, object_json
import flow_lifecycle

FINISHED_PHASES = {'REVIEW_DONE', 'ERROR', 'DEVELOPER_FAILED', 'TESTS_FAILED',
                   'REVIEWER_FAILED', 'REVIEWER_MODIFIED_FILES', 'DEVELOPMENT_CHANGED_AFTER_REVIEW_SNAPSHOT'}


@contextlib.contextmanager
def locked(project, flow, role, task):
    identifier(task)
    first = resolve(project, flow, role)
    if first['task'] != task:
        raise ValueError('Explicit task ID does not match role')
    # Always serialize live-root changes with the parent, including legacy flows.
    states = [first['base']['state']]
    for relative in ('developer-state', 'review-input/.agent-bridge'):
        state = safe(first['project'], '.role-flows/' + flow + '/' + relative)
        if state not in states and (state.exists() or state == first['config']['state']):
            states.append(state)
    with flow_lifecycle.lock(first['project'], first['base'], flow), contextlib.ExitStack() as stack:
        flow_home = safe(first['project'], '.role-flows/' + flow)
        if (flow_home / 'cleanup.json').exists() or (flow_home / 'archive').exists():
            from flow_archive import load_snapshot
            try:
                load_snapshot(first['project'], flow)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ValueError('FLOW_ARCHIVE_PENDING: inspect the archive operation first') from exc
        for state in states:
            for name in ('runner.lock', 'promotion.lock'):
                path = safe(first['project'], (state / name).relative_to(first['project']).as_posix())
                if state == first['config']['state'] and name == 'promotion.lock':
                    # workspace.review/recover acquire this themselves.
                    continue
                stack.enter_context(FileLock(path))
        current = resolve(project, flow, role)
        if any(current[k] != first[k] for k in ('task', 'queue', 'config', 'base', 'shared')):
            raise ValueError('Role identity/config changed while locking')
        if current['saved'].get('state') not in FINISHED_PHASES or current['active_roles']:
            raise ValueError('Flow is active or incomplete; inspect status first')
        for state in states[1:]:
            db_path = safe(first['project'], (state / 'queue.sqlite3').relative_to(first['project']).as_posix())
            if db_path.exists():
                with contextlib.closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
                    active = db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('QUEUED','RUNNING','ROLE_QUEUED','ROLE_RUNNING','PLAN_QUEUED','PLAN_RUNNING','SUMMARY_QUEUED','SUMMARY_RUNNING')").fetchone()[0]
                if active:
                    raise ValueError('Flow has active private tasks')
        yield current


def allowed(config, base, rel):
    return all(any(rel == p or rel.startswith(p.rstrip('/') + '/') for p in c['writable'])
               for c in (config, base))


def promote(project, flow, role, task, paths, apply=False):
    if role != 'developer':
        raise ValueError('Only developer proposals can be promoted')
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('Select unique reviewed paths')
    with locked(project, flow, role, task) as record:
        view = inspect(project, flow, role)
        if record['row']['state'] != 'DONE' or not view['manifest_matches']:
            raise ValueError('Requires DONE and unchanged proposal manifest')
        stored = json.loads((record['home'] / 'changes.json').read_text(encoding='utf-8'))
        if stored != record['result'].get('changes'):
            raise ValueError('Manifest differs from terminal task record')
        selected = []
        for path in paths:
            matches = [c for c in view['changes'] if c['path'] == path]
            if len(matches) != 1 or not matches[0]['allowed'] or not matches[0]['original_matches']:
                raise ValueError('Path is unreviewed, not writable, or original changed: ' + path)
            selected.append(matches[0])
        result = dict(flow=flow, role=role, task=task, apply=apply, changes=selected, review_is_approval=False)
        if apply:
            result.update(workspace.review(record['config'], task, paths))
        return result


def recover(project, flow, role, task, backup, apply=False):
    if role != 'developer':
        raise ValueError('Only developer recovery is supported')
    identifier(backup)
    if not backup.startswith('backup-'):
        raise ValueError('Invalid backup ID')
    with locked(project, flow, role, task) as record:
        home = record['home']
        inspect_tree(home)
        journal = object_json(safe(home, backup + '/journal.json'))
        if journal.get('state') not in {'APPLYING', 'APPLIED', 'RECOVERING', 'ROLLED_BACK'}:
            raise ValueError('Invalid recovery state')
        changes = journal.get('changes')
        if not isinstance(changes, list) or not changes:
            raise ValueError('Invalid recovery changes')
        seen = set()
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError('Invalid recovery change')
            rel = change.get('path')
            safe(record['project'], rel)
            if rel in seen or not allowed(record['config'], record['base'], rel):
                raise ValueError('Duplicate or non-writable recovery path')
            seen.add(rel)
            for key in ('before', 'after'):
                value = change[key]
                if value is not None and (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)):
                    raise ValueError('Invalid recovery hash')
        return dict(flow=flow, role=role, task=task, backup=backup, apply=apply,
                    journal=workspace.recover(record['config'], task, backup, apply))


def role_copy_guard(record, flow):
    archive = safe(record['project'], '.role-flows/' + flow + '/archive')
    journal = archive.parent / 'cleanup.json'
    data = dict(copy=copy_guard(record['config'], record['row']),
                saved=record['saved'], config=record['config'], base=record['base'],
                queue=record['queue'], shared=record['shared'],
                archive=workspace.digest(archive / 'manifest.json'), journal=workspace.digest(journal))
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


def preview_prune(project, flow, role, task, days=30, *, freeze=True):
    """Observe role cleanup without creating locks, databases, or archive files."""
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('days must be 1..3650')
    record = resolve(project, flow, role, database_reader=readonly_database)
    if record['task'] != identifier(task):
        raise ValueError('Explicit task ID does not match role')
    cutoff = time.time() - days * 86400
    artifact = candidate(record['config'], record['row'], cutoff)
    archive = 'ABSENT'
    if record['saved'].get('state') not in FINISHED_PHASES or record['active_roles']:
        artifact.update(eligible=False, reason='FLOW_ACTIVE_OR_INCOMPLETE')
    flow_home = safe(record['project'], '.role-flows/' + flow)
    if (flow_home / 'archive').exists() or (flow_home / 'cleanup.json').exists():
        try:
            from flow_archive import load_snapshot
            load_snapshot(record['project'], flow)
            archive = 'ARCHIVE_READY'
        except (OSError, ValueError, TypeError, KeyError):
            archive = 'UNCONFIRMED'
            artifact.update(eligible=False, reason='FLOW_ARCHIVE_PENDING')
    states = {record['base']['state'], record['config']['state']}
    for relative in ('developer-state', 'review-input/.agent-bridge'):
        state = safe(record['project'], '.role-flows/' + flow + '/' + relative)
        if state.exists():
            states.add(state)
    locks = [flow_lifecycle.lock_path(record['project'], record['base'], flow)]
    locks.extend(state / name for state in sorted(states) for name in ('runner.lock', 'promotion.lock'))
    if any(probe_lock(checked_local_path(lock)) == 'LOCKED' for lock in locks):
        artifact.update(eligible=False, reason='RUNNER_OR_MANAGEMENT_ACTIVE')
    for state in states - {record['base']['state']}:
        path = checked_local_path(state / 'queue.sqlite3')
        if path.exists():
            with contextlib.closing(readonly_database(path)) as db:
                if db.execute("SELECT 1 FROM tasks WHERE state IN ('QUEUED','RUNNING','ROLE_QUEUED','ROLE_RUNNING','PLAN_QUEUED','PLAN_RUNNING','SUMMARY_QUEUED','SUMMARY_RUNNING') LIMIT 1").fetchone():
                    artifact.update(eligible=False, reason='FLOW_ACTIVE_OR_INCOMPLETE')
    queue_path = checked_local_path(record['queue'] / 'queue.sqlite3')
    with contextlib.closing(readonly_database(queue_path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_cleanup'").fetchone():
            saved = db.execute('SELECT state FROM artifact_cleanup WHERE id=?', (task,)).fetchone()
            artifact['cleanup_state'] = saved[0] if saved else None
    if artifact['reason'] == 'ARTIFACTS_MISSING' and artifact.get('cleanup_state') == 'DELETED':
        artifact['reason'] = 'ALREADY_DELETED'
    report = dict(flow=flow, role=role, task=task, days=days, cutoff=cutoff, artifact=artifact,
                  workspace=str(record['home']), queue_mode='SHARED_PARENT' if record['shared'] else 'PRIVATE',
                  archive=archive, apply=False, read_only=True)
    if freeze and artifact['eligible']:
        report['guard'] = role_copy_guard(record, flow)
    artifact.pop('error', None)
    return report


def prune(project, flow, role, task, days=30, apply=False, *, expected=None, cutoff=None):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('days must be 1..3650')
    with locked(project, flow, role, task) as record:
        lock = safe(record['project'], (record['config']['state'] / 'promotion.lock').relative_to(record['project']).as_posix())
        with FileLock(lock):
            cutoff = time.time() - days * 86400 if cutoff is None else cutoff
            item = candidate(record['config'], record['row'], cutoff)
            if expected is not None and (not apply or not item['eligible'] or role_copy_guard(record, flow) != expected):
                raise ValueError('Role artifacts changed after cleanup preview')
            if apply and item['eligible']:
                # Re-read identity and eligibility immediately before deletion.
                fresh = resolve(project, flow, role)
                if any(fresh[k] != record[k] for k in ('task', 'queue', 'config', 'base', 'shared')):
                    raise ValueError('Role changed before deletion')
                if fresh['active_roles'] or fresh['saved'].get('state') not in FINISHED_PHASES:
                    raise ValueError('Flow became active before deletion')
                item = candidate(fresh['config'], fresh['row'], cutoff)
                if expected is not None and role_copy_guard(fresh, flow) != expected:
                    raise ValueError('Role artifacts changed before deletion')
                if item['eligible']:
                    home = Path(item['path'])
                    if home != record['home'] or not home.is_relative_to(record['project'] / '.role-flows' / flow):
                        raise ValueError('Unexpected deletion path')
                    # Only the actual owning queue is opened for writes.
                    store = Store(record['queue'])
                    delete_artifacts(store, fresh['row'], home, item, preserve=preserve_before_prune)
            return dict(flow=flow, role=role, task=task, apply=apply, days=days, artifact=item)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('action', choices=('promote', 'recover', 'prune', 'prune-flow', 'archive-flow'))
    parser.add_argument('--id', required=True)
    parser.add_argument('--role', choices=('developer', 'reviewer'))
    parser.add_argument('--task')
    parser.add_argument('--paths', nargs='+')
    parser.add_argument('--backup')
    parser.add_argument('--days', type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-hash')
    parser.add_argument('--continue', dest='continue_operation')
    parser.add_argument('--evidence-source')
    parser.add_argument('--evidence-path')
    args = parser.parse_args()
    try:
        if args.action == 'archive-flow':
            if args.role or args.task or args.paths or args.backup:
                raise ValueError('archive-flow does not accept role/task or promotion arguments')
            from flow_archive_writer import write_archive, continue_archive
            from flow_archive import load_snapshot, read_evidence
            from flow_cleanup import preview
            if args.continue_operation:
                if args.days is not None or args.evidence_source or args.evidence_path:
                    raise ValueError('Continuation uses the stored period and does not accept evidence queries')
                result = continue_archive(args.project, args.id, args.continue_operation,
                                          args.plan_hash, apply=args.apply)
            elif args.apply:
                if not args.plan_hash or args.evidence_source or args.evidence_path:
                    raise ValueError('Archive creation requires --plan-hash and no evidence query')
                result = write_archive(args.project, args.id, args.plan_hash, 30 if args.days is None else args.days)
            elif args.plan_hash:
                raise ValueError('--plan-hash requires --apply')
            else:
                identifier(args.id)
                home = safe(args.project.resolve(), '.role-flows/' + args.id)
                if (home / 'cleanup.json').exists() or (home / 'archive').exists():
                    if args.days is not None:
                        raise ValueError('Stored snapshot queries do not accept --days')
                    result = load_snapshot(args.project, args.id)
                    if bool(args.evidence_source) != bool(args.evidence_path):
                        raise ValueError('Specify both evidence source and path')
                    if args.evidence_path:
                        result = read_evidence(args.project, args.id, args.evidence_source, args.evidence_path,
                                               expected_digest=result['archive_digest'])
                else:
                    if args.evidence_source or args.evidence_path:
                        raise ValueError('No completed archive snapshot')
                    result = preview(args.project, args.id, 30 if args.days is None else args.days)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.plan_hash or args.evidence_source or args.evidence_path or args.continue_operation:
            raise ValueError('Archive arguments require archive-flow')
        if args.action == 'prune-flow':
            if args.apply or args.role or args.task or args.paths or args.backup:
                raise ValueError('prune-flow is preview-only and accepts only --id and --days')
            from flow_cleanup import preview
            result = preview(args.project, args.id, 30 if args.days is None else args.days)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if not args.role or not args.task:
            raise ValueError('Role management requires --role and --task')
        common = (args.project, args.id, args.role, args.task)
        if args.action == 'promote':
            if args.backup or args.days is not None: raise ValueError('Unexpected promote arguments')
            result = promote(*common, args.paths, args.apply)
        elif args.action == 'recover':
            if args.paths or args.days is not None: raise ValueError('Unexpected recover arguments')
            result = recover(*common, args.backup, args.apply)
        else:
            if args.paths or args.backup: raise ValueError('Unexpected prune arguments')
            result = prune(*common, 30 if args.days is None else args.days, args.apply)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if args.action == 'prune' and args.apply and not result['artifact']['deleted'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
