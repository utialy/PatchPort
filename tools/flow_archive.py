"""Read-only evidence access for the proposed flow archive schema; no cleanup."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

from agent_bridge.storage import identifier
from role_review import safe
from agent_bridge.cleanup import TERMINAL

SOURCES = {'original_input', 'review_input', 'developer_workspace',
           'reviewer_workspace', 'flow_metadata'}
PHASES = {'REVIEW_DONE', 'ERROR', 'DEVELOPER_FAILED', 'TESTS_FAILED',
          'REVIEWER_FAILED', 'REVIEWER_MODIFIED_FILES', 'DEVELOPMENT_CHANGED_AFTER_REVIEW_SNAPSHOT'}
ARTIFACT_STATES = {'ARCHIVE_READY', 'DELETING', 'DELETE_FAILED', 'COMPACTED'}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite JSON value: ' + value)


def _json(data):
    value = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object')
    return value


def manifest_digest(manifest):
    payload = {key: value for key, value in manifest.items() if key != 'archive_digest'}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid SHA256')
    return value


def _path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value) or value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/'))
            or PurePosixPath(value).is_absolute()):
        raise ValueError('Invalid logical evidence path')
    return value


def _read(root, relative):
    path = safe(root, relative)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Evidence must be a regular file without hardlinks')
    return path.read_bytes()


def load_archive(project, flow):
    """Validate metadata/records only. Blob integrity is checked on evidence read."""
    identifier(flow)
    project = Path(project).resolve()
    archive = safe(project, '.role-flows/' + flow + '/archive')
    manifest = _json(_read(archive, 'manifest.json'))
    if (set(manifest) != {'schema', 'flow', 'entries', 'records_sha256', 'archive_digest'}
            or type(manifest['schema']) is not int or manifest['schema'] != 1
            or manifest['flow'] != flow or not isinstance(manifest['entries'], list)):
        raise ValueError('Unsupported archive manifest')
    if _hash(manifest['archive_digest']) != manifest_digest(manifest):
        raise ValueError('Archive manifest digest mismatch')
    seen = set()
    for entry in manifest['entries']:
        if not isinstance(entry, dict) or set(entry) != {'source', 'path', 'kind', 'sha256', 'size', 'mtime_ns'}:
            raise ValueError('Invalid archive entry')
        if not isinstance(entry['source'], str) or entry['source'] not in SOURCES or entry['kind'] != 'file':
            raise ValueError('Unsupported evidence source or kind')
        identity = (entry['source'], _path(entry['path']))
        if identity in seen:
            raise ValueError('Duplicate evidence identity')
        seen.add(identity)
        _hash(entry['sha256'])
        if any(type(entry[key]) is not int or entry[key] < 0 for key in ('size', 'mtime_ns')):
            raise ValueError('Invalid evidence size or timestamp')
    records = _read(archive, 'records.json')
    if hashlib.sha256(records).hexdigest() != _hash(manifest['records_sha256']):
        raise ValueError('Records digest mismatch')
    return archive, manifest, _json(records)


def read_evidence(project, flow, source, path, *, expected_digest=None):
    """Return one verified file as UTF-8 text or base64; never restores files."""
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError('Unknown evidence source')
    _path(path)
    archive, manifest, _ = load_archive(project, flow)
    if expected_digest is not None and manifest['archive_digest'] != expected_digest:
        raise ValueError('Archive changed during evidence query')
    matches = [e for e in manifest['entries'] if (e['source'], e['path']) == (source, path)]
    if not matches:
        raise ValueError('Evidence not found')
    entry = matches[0]
    data = _read(archive, 'blobs/' + entry['sha256'])
    if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('Evidence digest or size mismatch')
    try:
        content, encoding = data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(data).decode('ascii'), 'base64'
    return dict(flow=flow, source=source, path=path, sha256=entry['sha256'],
                encoding=encoding, content=content, read_only=True,
                artifact_source='ARCHIVE', current_files_checked=False)


def is_archived(saved):
    return isinstance(saved, dict) and (saved.get('state') == 'ARCHIVED' or 'artifact_layout' in saved)


def validate_records(records, flow, entries):
    required = {'schema', 'flow', 'queue_mode', 'original_phase', 'roles', 'checks'}
    if (not required <= records.keys() or records.keys() - required - {'error', 'original_changed_since_snapshot'}
            or type(records['schema']) is not int or records['schema'] != 1
            or records['flow'] != flow or records['queue_mode'] not in ('PRIVATE', 'SHARED_PARENT')
            or not isinstance(records['original_phase'], str) or records['original_phase'] not in PHASES):
        raise ValueError('Invalid archive records schema or identity')
    roles = records['roles']
    if not isinstance(roles, dict) or set(roles) != {'developer', 'reviewer'}:
        raise ValueError('Archive must describe both roles, including unsubmitted roles')
    for role in roles.values():
        if role is None:
            continue
        if not isinstance(role, dict) or set(role) != {'task', 'state', 'answer', 'changes'}:
            raise ValueError('Invalid archived role')
        identifier(role['task'])
        if not isinstance(role['state'], str) or role['state'] not in TERMINAL:
            raise ValueError('Archived role must be terminal')
        answer = role['answer']
        if (not isinstance(answer, dict) or set(answer) != {'text', 'status'}
                or answer['status'] not in ('COMPLETE', 'PARTIAL', 'LEGACY_REPORTED', 'UNAVAILABLE')
                or (answer['text'] is not None and not isinstance(answer['text'], str))
                or (answer['status'] == 'UNAVAILABLE') != (answer['text'] is None)):
            raise ValueError('Invalid archived answer')
        if role['changes'] is None:
            continue
        if not isinstance(role['changes'], list):
            raise ValueError('Invalid archived changes')
        seen = set()
        for change in role['changes']:
            if not isinstance(change, dict) or set(change) != {'path', 'before', 'after', 'allowed'}:
                raise ValueError('Invalid archived change')
            path = _path(change['path'])
            if path in seen or type(change['allowed']) is not bool:
                raise ValueError('Duplicate change or invalid permission observation')
            seen.add(path)
            for key in ('before', 'after'):
                if change[key] is not None:
                    _hash(change[key])
    if records['original_phase'] == 'REVIEW_DONE' and any(r is None or r['state'] != 'DONE' for r in roles.values()):
        raise ValueError('REVIEW_DONE requires two completed roles')
    if (records['queue_mode'] == 'SHARED_PARENT' and all(roles.values())
            and roles['developer']['task'] == roles['reviewer']['task']):
        raise ValueError('Shared archive roles must have distinct task IDs')
    if not isinstance(records['checks'], list):
        raise ValueError('Invalid archived checks')
    available = {(entry['source'], entry['path']) for entry in entries}
    for check in records['checks']:
        if (not isinstance(check, dict) or set(check) != {'argv', 'exit_code', 'log'}
                or not isinstance(check['argv'], list) or not check['argv']
                or any(not isinstance(a, str) or not a for a in check['argv'])
                or type(check['exit_code']) is not int):
            raise ValueError('Invalid archived check')
        if ('flow_metadata', _path(check['log'])) not in available:
            raise ValueError('Archived check log is not in the evidence manifest')
    if records.get('error') is not None and not isinstance(records['error'], str):
        raise ValueError('Invalid archived error')
    changed = records.get('original_changed_since_snapshot')
    if changed is not None and type(changed) is not bool:
        raise ValueError('Invalid original-change observation')


def load_view(project, flow):
    """Validate the archive marker, records, journal and every referenced blob."""
    identifier(flow)
    project = Path(project).resolve()
    home = safe(project, '.role-flows/' + flow)
    saved = _json(_read(home, 'result.json'))
    if (saved.get('id') != flow or saved.get('state') != 'ARCHIVED'
            or saved.get('artifact_layout') != 'FLOW_ARCHIVE_V1'):
        raise ValueError('Unsupported archive marker')
    archive, manifest, records = load_archive(project, flow)
    if _hash(saved.get('archive_digest')) != manifest['archive_digest']:
        raise ValueError('Archive marker digest mismatch')
    validate_records(records, flow, manifest['entries'])
    if any(saved.get(key) != records[key] for key in ('original_phase', 'queue_mode')):
        raise ValueError('Archive marker and records disagree')
    operation = identifier(saved.get('cleanup_operation'))
    journal = _json(_read(home, 'cleanup.json'))
    if (type(journal.get('schema')) is not int or journal['schema'] != 1
            or journal.get('flow') != flow or journal.get('operation') != operation
            or journal.get('archive_digest') != manifest['archive_digest']
            or not isinstance(journal.get('state'), str) or journal['state'] not in ARTIFACT_STATES):
        raise ValueError('Invalid archive cleanup journal')
    verified = {}
    for entry in manifest['entries']:
        digest = entry['sha256']
        if digest not in verified:
            path = safe(archive, 'blobs/' + digest)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Evidence must be a regular file without hardlinks')
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                size = stream.tell()
            if actual != digest:
                raise ValueError('Evidence digest mismatch')
            verified[digest] = size
        if verified[digest] != entry['size']:
            raise ValueError('Evidence size mismatch')
    return dict(records=records, entries=manifest['entries'], archive_digest=manifest['archive_digest'],
                artifact_state=journal['state'], evidence_status='AVAILABLE',
                artifact_source='ARCHIVE', current_files_checked=False)


def role_view(view, flow, role):
    if role not in ('developer', 'reviewer'):
        raise ValueError('Unknown role')
    record = view['records']['roles'][role]
    if record is None:
        raise ValueError('Role was not submitted')
    changes = None if record['changes'] is None else [dict(c, recorded_allowed=c['allowed'], allowed=None,
        original=None, original_matches=None, proposal_matches_record=None) for c in record['changes']]
    return dict(schema=1, flow=flow, role=role, task=record['task'], state=record['state'],
                queue_mode=view['records']['queue_mode'], answer=record['answer'], changes=changes,
                checks=view['records']['checks'], workspace=None, manifest_matches=None,
                read_only=True, review_is_approval=False, task_success='NOT_EVALUATED',
                consistency='OBSERVATION_ONLY', current_files_checked=False,
                promotion_supported=False, cleanup_supported=False,
                recovery_protection=dict(eligible=False, reason='FLOW_ARCHIVED'),
                artifact_state=view['artifact_state'], evidence_status=view['evidence_status'],
                artifact_source='ARCHIVE', archive_digest=view['archive_digest'])


def result_view(view, flow):
    records = view['records']
    result = dict(id=flow, state=records['original_phase'], checks=records['checks'], error=records.get('error'),
                original_changed_since_snapshot=records.get('original_changed_since_snapshot'),
                automatic_promotion=False, review_is_approval=False, task_success='NOT_EVALUATED',
                artifact_state=view['artifact_state'], evidence_status=view['evidence_status'],
                artifact_source='ARCHIVE', current_files_checked=False)
    for role, record in records['roles'].items():
        if record is not None:
            result[role] = dict(record, config=None, usage=None, usage_source='QUERY_OWNING_QUEUE')
    return result
