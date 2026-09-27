"""Read-only role artifact inspection. Never applies, deletes, or starts work."""
import argparse
import contextlib
import json
from pathlib import Path
import re
import sqlite3
import stat

from agent_bridge import workspace
from agent_bridge.cleanup import TERMINAL, candidate, inspect_tree
from agent_bridge.storage import configure_output, identifier, load_config


def safe(root, relative):
    path = workspace.safe(root, relative)
    walk = path
    while walk != root:
        if walk.exists() or walk.is_symlink():
            info = walk.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Links/reparse points are not supported')
        walk = walk.parent
    return path


def object_json(path):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object: ' + path.name)
    return value


def flow_header(project, flow_id):
    project = Path(project).resolve()
    identifier(flow_id)
    base = load_config(safe(project, 'bridge.json'))
    if base['root'] != project:
        raise ValueError('Requires configured project root')
    if Path.cwd().resolve().is_relative_to(project / '.role-flows') or Path.cwd().resolve().is_relative_to(base['state'] / 'workspaces'):
        raise ValueError('Workers cannot inspect original role workflows')
    flow = safe(project, '.role-flows/' + flow_id)
    saved = object_json(safe(project, f'.role-flows/{flow_id}/result.json'))
    if saved.get('id') != flow_id:
        raise ValueError('Flow identity mismatch')
    return project, base, flow, saved


def resolve(project, flow_id, role, *, database_reader=None):
    if role not in ('developer', 'reviewer'):
        raise ValueError('Unknown role')
    project, base, flow, saved = flow_header(project, flow_id)
    if saved.get('state') == 'ARCHIVED' or 'artifact_layout' in saved:
        raise ValueError('FLOW_ARCHIVED: promotion/recovery requires live artifacts')
    metadata = saved.get(role)
    if not isinstance(metadata, dict):
        raise ValueError('Role metadata missing')
    task = identifier(metadata.get('task'))
    root = project if role == 'developer' else safe(project, f'.role-flows/{flow_id}/review-input')
    state = safe(project, f'.role-flows/{flow_id}/' + ('developer-state' if role == 'developer' else 'review-input/.agent-bridge'))
    config_path = safe(project, f'.role-flows/{flow_id}/{role}-config.json')
    if metadata.get('config') != str(config_path):
        raise ValueError('Unexpected recorded config path')
    config = load_config(config_path)
    if config['root'] != root or config['state'] != state:
        raise ValueError('Role config escapes expected root/state')
    if saved.get('queue_mode') not in (None, 'SHARED_PARENT'):
        raise ValueError('Unknown queue mode')
    shared = saved.get('queue_mode') == 'SHARED_PARENT'
    if not shared and metadata.get('queue_state'):
        raise ValueError('Shared queue metadata requires SHARED_PARENT mode')
    queue = base['state'] if shared else state
    if shared and metadata.get('queue_state') != str(queue):
        raise ValueError('Unexpected queue path')
    db_path = safe(project, (queue / 'queue.sqlite3').relative_to(project).as_posix())
    with contextlib.closing(database_reader(db_path) if database_reader else
                           sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        if row is None or row['endpoint'] != role:
            raise ValueError('Role task missing or mismatched')
        row = dict(row)
        active_roles = 0
        if shared:
            active_roles = db.execute("SELECT COUNT(*) FROM tasks t JOIN role_tasks r ON t.id=r.id WHERE r.flow=? AND t.state IN ('ROLE_QUEUED','ROLE_RUNNING','QUEUED','RUNNING')", (flow_id,)).fetchone()[0]
            record = db.execute('SELECT flow,role,data FROM role_tasks WHERE id=?', (task,)).fetchone()
            if record is None or record['flow'] != flow_id or record['role'] != role:
                raise ValueError('Parent role identity mismatch')
            frozen = json.loads(record['data'])
            if not isinstance(frozen, dict) or frozen.get('flow') != flow_id or frozen.get('role') != role or frozen.get('config') != object_json(config_path):
                raise ValueError('Role config differs from submitted config')
    row['state'] = row['state'].removeprefix('ROLE_').removeprefix('PLAN_')
    if row['state'] not in TERMINAL:
        raise ValueError('Role task is not terminal; inspect status first')
    home = safe(project, (state / 'workspaces' / task).relative_to(project).as_posix())
    result = json.loads(row['result'] or '{}')
    if not isinstance(result, dict) or result.get('workspace') != str(home):
        raise ValueError('Unexpected recorded workspace')
    return dict(project=project, base=base, config=config, queue=queue, home=home,
                row=row, saved=saved, shared=shared, task=task, result=result, active_roles=active_roles)


def inspect(project, flow_id, role, *, evidence_path=None, evidence_source=None):
    if role not in ('developer', 'reviewer'):
        raise ValueError('Unknown role')
    project, _, _, saved = flow_header(project, flow_id)
    if saved.get('state') == 'ARCHIVED' or 'artifact_layout' in saved:
        from flow_archive import load_view, role_view, read_evidence
        view = load_view(project, flow_id)
        result = role_view(view, flow_id, role)
        if evidence_source is not None and evidence_path is None:
            raise ValueError('--evidence-source requires --evidence-path')
        if evidence_path is not None:
            result['evidence'] = read_evidence(project, flow_id, evidence_source or role + '_workspace',
                                             evidence_path, expected_digest=view['archive_digest'])
        return result
    if evidence_path is not None or evidence_source is not None:
        raise ValueError('Evidence file options require an archived flow')
    resolved = resolve(project, flow_id, role)
    project, base, config, home, row, task, shared = (
        resolved[k] for k in ('project', 'base', 'config', 'home', 'row', 'task', 'shared'))
    root = config['root']
    inspect_tree(home)
    before = object_json(home / 'baseline.json')
    for rel, digest in before.items():
        safe(project, rel)
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid baseline hash')
    after = workspace.files(home / 'project')
    recorded = json.loads((home / 'changes.json').read_text(encoding='utf-8'))
    if not isinstance(recorded, list) or any(not isinstance(c, dict) for c in recorded):
        raise ValueError('Invalid change manifest')
    changes = []
    for rel in sorted(before.keys() | after.keys()):
        if before.get(rel) == after.get(rel):
            continue
        original = workspace.digest(safe(project, rel)) if role == 'developer' else workspace.digest(safe(root, rel))
        allowed = all(any(rel == p or rel.startswith(p.rstrip('/') + '/') for p in policy)
                      for policy in (config['writable'], base['writable']))
        matches = [c for c in recorded if c.get('path') == rel]
        matches_record = len(matches) == 1 and matches[0].get('before') == before.get(rel) and matches[0].get('after') == after.get(rel)
        changes.append(dict(path=rel, before=before.get(rel), after=after.get(rel), original=original,
                            allowed=allowed, original_matches=original == before.get(rel), proposal_matches_record=matches_record))
    manifest_matches = len(recorded) == len(changes) and all(c['proposal_matches_record'] for c in changes)
    return dict(schema=1, flow=flow_id, role=role, task=task, state=row['state'],
                queue_mode='SHARED_PARENT' if shared else 'PRIVATE', workspace=str(home),
                read_only=True, review_is_approval=False, consistency='OBSERVATION_ONLY',
                changes=changes, manifest_matches=manifest_matches,
                promotion_supported=False, cleanup_supported=False,
                recovery_protection=candidate(config, row, float('inf')))


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--role', choices=('developer', 'reviewer'), required=True)
    parser.add_argument('--evidence-path', help='Read one archived logical evidence path')
    parser.add_argument('--evidence-source', choices=('original_input', 'review_input', 'flow_metadata',
                                                     'developer_workspace', 'reviewer_workspace'))
    args = parser.parse_args()
    try:
        result = inspect(args.project, args.id, args.role,
                         evidence_path=args.evidence_path, evidence_source=args.evidence_source)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
