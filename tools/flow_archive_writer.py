"""Create a verified flow archive without removing or replacing live artifacts."""
import contextlib
import hashlib
from pathlib import Path
import uuid

from agent_bridge.storage import FileLock, atomic_json, load_config, durable_write, sync_directory, identifier
import flow_archive
import flow_cleanup
from role_review import safe, object_json


def _materialize(staging, flow, records, entries):
    """Complete missing files; refuse to replace conflicting evidence."""
    raw_records = flow_cleanup.canonical(records)
    manifest = dict(schema=1, flow=records['flow'], entries=entries,
                    records_sha256=hashlib.sha256(raw_records).hexdigest())
    manifest['archive_digest'] = flow_archive.manifest_digest(manifest)
    expected = {'records.json': raw_records, 'manifest.json': flow_cleanup.canonical(manifest)}
    for entry in entries:
        source = _source(flow, entry, records)
        data = flow_archive._read(source.parent, source.name)
        if (len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']
                or source.stat().st_mtime_ns != entry['mtime_ns']):
            raise ValueError('SOURCE_CHANGED')
        path = staging / 'blobs' / entry['sha256']
        if path.exists():
            if flow_archive._read(staging, 'blobs/' + entry['sha256']) != data:
                raise ValueError('Existing blob digest mismatch')
        else:
            durable_write(path, data)
    for name, data in expected.items():
        path = staging / name
        if not path.exists():
            durable_write(path, data)
        if flow_archive._read(staging, name) != data:
            raise ValueError('Archive metadata changed before publication')
    flow_archive.validate_records(records, records['flow'], entries)
    flow_archive.verify_blobs(staging, manifest)
    sync_directory(staging / 'blobs')
    sync_directory(staging)
    return manifest


def _source(flow, entry, records):
    source, relative = entry['source'], entry['path']
    roots = {'original_input': 'original-input', 'review_input': 'review-input', 'flow_metadata': ''}
    for role, state in (('developer', 'developer-state'), ('reviewer', 'review-input/.agent-bridge')):
        record = records['roles'][role]
        if record is not None:
            roots[role + '_workspace'] = state + '/workspaces/' + record['task'] + '/project'
    return safe(flow, '/'.join(part for part in (roots[source], relative) if part))


def write_archive(project, flow_id, plan_hash, days=30, *, now=None):
    """Require an unchanged eligible plan; retain partial output on failure."""
    flow_archive._hash(plan_hash)
    project = Path(project).resolve()
    details = {}
    first = flow_cleanup.preview(project, flow_id, days, now=now, _details=details)
    if not first['eligible']:
        raise ValueError('FLOW_PROTECTED: ' + ', '.join(first['reasons']))
    if first['plan_hash'] != plan_hash:
        raise ValueError('PLAN_CHANGED')
    flow = safe(project, '.role-flows/' + flow_id)
    base = load_config(project / 'bridge.json')
    original_files = {item['path']: item for item in details['binding']['files']}
    original_dirs = {item['path']: item for item in details['binding']['directories']}
    created_locks, touched_dirs, held = set(), set(), set()
    lock_handles = {}
    journal = None

    def digest_file(path):
        if path not in lock_handles:
            return flow_cleanup.workspace.digest(path)
        stream = lock_handles[path].file
        stream.seek(0)
        return hashlib.file_digest(stream, 'sha256').hexdigest()

    def observed_inventory(root):
        files, directories = flow_cleanup.inventory(root, _digest=digest_file)
        if journal is not None:
            if object_json(safe(flow, 'cleanup.json')) != journal:
                raise ValueError('Archive journal changed')
            files = [item for item in files if item['path'] != 'cleanup.json']
        # FileLock may initialize missing locks; only its own additions are excluded.
        files = [item for item in files if item['path'] not in created_locks]
        directories = [dict(item, mtime_ns=original_dirs[item['path']]['mtime_ns'],
                            size=original_dirs[item['path']]['size'])
                       if item['path'] in touched_dirs else item for item in directories]
        return files, directories

    with contextlib.ExitStack() as stack:
        for lock in first['locks']:
            path = safe(project, lock['path'])
            if path in held:
                continue
            # An absent role state has no private runner. The flow and parent guards protect its creation.
            if not path.parent.exists():
                if path.parent in (flow / 'developer-state', flow / 'review-input/.agent-bridge'):
                    continue
                raise ValueError('Lock directory disappeared')
            if path.is_relative_to(flow) and not path.exists():
                relative = path.relative_to(flow).as_posix()
                if relative in original_files:
                    raise ValueError('PLAN_CHANGED')
                created_locks.add(relative)
                touched_dirs.add(path.parent.relative_to(flow).as_posix())
            lock_handles[path] = stack.enter_context(FileLock(path))
            held.add(path)
        locked_details = {}
        fresh = flow_cleanup.preview(project, flow_id, days, now=now, _held_locks=held,
                                     _inventory=observed_inventory, _details=locked_details)
        if not fresh['eligible'] or fresh['plan_hash'] != plan_hash:
            raise ValueError('PLAN_CHANGED')
        records = locked_details['records']
        operation = uuid.uuid4().hex
        staging = safe(project, (base['state'] / ('archive-staging-' + operation)).relative_to(project).as_posix())
        journal = dict(schema=1, flow=flow_id, operation=operation, mode='ARCHIVE_ONLY', state='PREPARING',
                       plan_hash=plan_hash, retention_days=days, staging=staging.relative_to(project).as_posix(),
                       originals_retained=True, deleted_files=0, resume_schema=1,
                       binding=details['binding'], records=records, entries=fresh['archive_entries'],
                       created_locks=sorted(created_locks))
        touched_dirs.add('.')
        atomic_json(flow / 'cleanup.json', journal)
        sync_directory(flow)
        try:
            staging.mkdir()
            (staging / 'blobs').mkdir()
            sync_directory(staging.parent)
            manifest = _materialize(staging, flow, records, fresh['archive_entries'])
            last = flow_cleanup.preview(project, flow_id, days, now=now, _held_locks=held,
                                        _inventory=observed_inventory, _archive_operation=operation)
            if not last['eligible'] or last['plan_hash'] != plan_hash:
                raise ValueError('PLAN_CHANGED')
            journal['archive_digest'] = manifest['archive_digest']
            atomic_json(flow / 'cleanup.json', journal)
            target = safe(flow, 'archive')
            if target.exists():
                raise ValueError('Archive already exists')
            staging.rename(target)
            sync_directory(flow)
            sync_directory(staging.parent)
            journal['state'] = 'ARCHIVE_READY'
            atomic_json(flow / 'cleanup.json', journal)
            sync_directory(flow)
            return dict(flow=flow_id, operation=operation, plan_hash=plan_hash,
                        **flow_archive.load_snapshot(project, flow_id))
        except BaseException:
            # Leave evidence and the operation state for explicit inspection; never retry or delete.
            if not (flow / 'archive').exists():
                journal['state'] = 'ARCHIVE_FAILED'
                atomic_json(flow / 'cleanup.json', journal)
            raise


def _inspect_partial(path, records, entries, *, complete=False):
    """Accept only expected, intact files; an interrupted write is not trusted."""
    raw = flow_cleanup.canonical(records)
    manifest = dict(schema=1, flow=records['flow'], entries=entries,
                    records_sha256=hashlib.sha256(raw).hexdigest())
    manifest['archive_digest'] = flow_archive.manifest_digest(manifest)
    expected = {'records.json': (len(raw), hashlib.sha256(raw).hexdigest())}
    raw_manifest = flow_cleanup.canonical(manifest)
    expected['manifest.json'] = (len(raw_manifest), hashlib.sha256(raw_manifest).hexdigest())
    expected.update({'blobs/' + item['sha256']: (item['size'], item['sha256']) for item in entries})
    if not path.exists():
        if complete:
            raise ValueError('Published archive missing')
        return sorted(expected), manifest
    files, directories = flow_cleanup.inventory(path)
    if any(item['path'] not in ('.', 'blobs') for item in directories):
        raise ValueError('Unexpected archive directory')
    for item in files:
        if expected.get(item['path']) != (item['size'], item['sha256']):
            raise ValueError('Unknown or corrupt archive evidence')
    missing = sorted(expected.keys() - {item['path'] for item in files})
    if complete and missing:
        raise ValueError('Published archive is incomplete')
    return missing, manifest


def continue_archive(project, flow_id, operation, plan_hash=None, *, apply=False, now=None):
    """Inspect or explicitly complete one persisted archive operation; never deletes."""
    identifier(flow_id)
    identifier(operation)
    project = Path(project).resolve()
    flow = safe(project, '.role-flows/' + flow_id)
    journal_path = safe(flow, 'cleanup.json')
    journal = flow_archive._json(flow_archive._read(flow, 'cleanup.json'))
    if (journal.get('mode') != 'ARCHIVE_ONLY' or journal.get('flow') != flow_id
            or journal.get('operation') != operation or type(journal.get('resume_schema')) is not int
            or journal['resume_schema'] != 1
            or journal.get('state') not in ('PREPARING', 'ARCHIVE_FAILED', 'ARCHIVE_READY')):
        raise ValueError('Unsupported or mismatched archive operation; legacy journals cannot resume')
    original_hash = flow_archive._hash(journal.get('plan_hash'))
    if plan_hash != original_hash and (apply or plan_hash is not None):
        raise ValueError('Explicit original plan hash required')
    binding = journal['binding']
    if (hashlib.sha256(flow_cleanup.canonical(binding)).hexdigest() != original_hash
            or binding.get('id') != flow_id or binding.get('project') != str(project)
            or binding.get('days') != journal.get('retention_days')):
        raise ValueError('Invalid persisted plan')
    if journal['state'] == 'ARCHIVE_READY':
        return dict(flow=flow_id, operation=operation, plan_hash=original_hash,
                    apply=apply, already_complete=True, **flow_archive.load_snapshot(project, flow_id))
    base = load_config(safe(project, 'bridge.json'))
    staging = safe(project, (base['state'] / ('archive-staging-' + operation)).relative_to(project).as_posix())
    if journal.get('staging') != staging.relative_to(project).as_posix():
        raise ValueError('Staging identity mismatch')
    archive = safe(flow, 'archive')
    if staging.exists() and archive.exists():
        raise ValueError('Ambiguous staged and published archive')
    records, entries = journal['records'], journal['entries']
    flow_archive.validate_records(records, flow_id, entries)
    missing, manifest = _inspect_partial(archive if archive.exists() else staging, records, entries,
                                         complete=archive.exists())
    if journal.get('archive_digest') not in (None, manifest['archive_digest']):
        raise ValueError('Archive digest differs from the persisted operation')
    original_dirs = {item['path']: item for item in binding['directories']}
    created = journal['created_locks']
    permitted = {state + '/' + name for state in ('developer-state', 'review-input/.agent-bridge')
                 for name in ('runner.lock', 'promotion.lock')}
    if (not isinstance(created, list) or any(not isinstance(p, str) or p not in permitted for p in created)
            or set(created) & {item['path'] for item in binding['files']}):
        raise ValueError('Invalid generated lock inventory')
    touched = {'.'} | {str(Path(p).parent).replace('\\', '/') for p in created}
    handles = {}

    def digest_file(path):
        if path not in handles:
            return flow_cleanup.workspace.digest(path)
        stream = handles[path].file
        stream.seek(0)
        return hashlib.file_digest(stream, 'sha256').hexdigest()

    def observed_inventory(root):
        if object_json(journal_path) != journal:
            raise ValueError('Archive journal changed')
        files, directories = flow_cleanup.inventory(root, _digest=digest_file)
        files = [item for item in files if item['path'] not in set(created) | {'cleanup.json'}
                 and not item['path'].startswith('archive/')]
        directories = [dict(item, mtime_ns=original_dirs[item['path']]['mtime_ns'],
                            size=original_dirs[item['path']]['size'])
                       if item['path'] in touched else item for item in directories
                       if item['path'] != 'archive' and not item['path'].startswith('archive/')]
        return files, directories

    def check():
        details = {}
        report = flow_cleanup.preview(project, flow_id, journal['retention_days'], now=now,
            _held_locks=handles, _inventory=observed_inventory, _details=details, _archive_operation=operation)
        if not report['eligible']:
            raise ValueError('FLOW_PROTECTED: ' + ', '.join(report['reasons']))
        if (report['plan_hash'] != original_hash or details['records'] != records
                or report['archive_entries'] != entries):
            raise ValueError('PLAN_CHANGED')
        return report

    first = check()
    result = dict(flow=flow_id, operation=operation, plan_hash=original_hash, apply=apply,
                  eligible=True, state=journal['state'], missing_files=missing,
                  published=archive.exists(), originals_retained=True, deleted_files=0)
    if not apply:
        return result
    with contextlib.ExitStack() as stack:
        for item in first['locks']:
            path = safe(project, item['path'])
            if path in handles:
                continue
            if not path.parent.exists():
                if path.parent in (flow/'developer-state', flow/'review-input/.agent-bridge'):
                    continue
                raise ValueError('Lock directory disappeared')
            handles[path] = stack.enter_context(FileLock(path))
        check()
        _inspect_partial(archive if archive.exists() else staging, records, entries, complete=archive.exists())
        journal['state'] = 'PREPARING'
        atomic_json(journal_path, journal)
        sync_directory(flow)
        if not archive.exists():
            staging.mkdir(exist_ok=True)
            (staging/'blobs').mkdir(exist_ok=True)
            sync_directory(staging.parent)
            manifest = _materialize(staging, flow, records, entries)
            check()
            _inspect_partial(staging, records, entries, complete=True)
            journal['archive_digest'] = manifest['archive_digest']
            atomic_json(journal_path, journal)
            if archive.exists():
                raise ValueError('Archive appeared during continuation')
            staging.rename(archive)
            sync_directory(flow)
            sync_directory(staging.parent)
        else:
            check()
        journal['archive_digest'] = manifest['archive_digest']
        journal['state'] = 'ARCHIVE_READY'
        atomic_json(journal_path, journal)
        sync_directory(flow)
        return dict(flow=flow_id, operation=operation, plan_hash=original_hash, apply=True,
                    **flow_archive.load_snapshot(project, flow_id))
