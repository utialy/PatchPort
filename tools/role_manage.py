"""Explicit role promotion, recovery and artifact cleanup; never submits AI work."""
import argparse
import contextlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from agent_bridge import workspace
from agent_bridge.answers import preserve_before_prune
from agent_bridge.cleanup import candidate, inspect_tree
from agent_bridge.storage import FileLock, Store, configure_output, identifier
from role_review import inspect, resolve, safe, object_json

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
    if first['config']['state'] not in states:
        states.append(first['config']['state'])
    with contextlib.ExitStack() as stack:
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


def prune(project, flow, role, task, days=30, apply=False):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('days must be 1..3650')
    with locked(project, flow, role, task) as record:
        lock = safe(record['project'], (record['config']['state'] / 'promotion.lock').relative_to(record['project']).as_posix())
        with FileLock(lock):
            cutoff = time.time() - days * 86400
            item = candidate(record['config'], record['row'], cutoff)
            if apply and item['eligible']:
                # Re-read identity and eligibility immediately before deletion.
                fresh = resolve(project, flow, role)
                if any(fresh[k] != record[k] for k in ('task', 'queue', 'config', 'base', 'shared')):
                    raise ValueError('Role changed before deletion')
                if fresh['active_roles'] or fresh['saved'].get('state') not in FINISHED_PHASES:
                    raise ValueError('Flow became active before deletion')
                item = candidate(fresh['config'], fresh['row'], cutoff)
                if item['eligible']:
                    home = Path(item['path'])
                    if home != record['home'] or not home.is_relative_to(record['project'] / '.role-flows' / flow):
                        raise ValueError('Unexpected deletion path')
                    # Only the actual owning queue is opened for writes.
                    store = Store(record['queue'])
                    store.mark_artifacts(task, 'DELETING')
                    try:
                        preserve_before_prune(store, fresh['row'], home)
                        shutil.rmtree(home)
                    except (OSError, ValueError, TypeError) as exc:
                        store.mark_artifacts(task, 'DELETE_FAILED')
                        item.update(reason='DELETE_FAILED', error=str(exc))
                    else:
                        store.mark_artifacts(task, 'DELETED')
                        item.update(reason='DELETED', deleted=True)
            return dict(flow=flow, role=role, task=task, apply=apply, days=days, artifact=item)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('action', choices=('promote', 'recover', 'prune'))
    parser.add_argument('--id', required=True)
    parser.add_argument('--role', required=True, choices=('developer', 'reviewer'))
    parser.add_argument('--task', required=True)
    parser.add_argument('--paths', nargs='+')
    parser.add_argument('--backup')
    parser.add_argument('--days', type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
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
