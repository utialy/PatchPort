"""Bounded, body-free observations of task and flow references; never mutate data."""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat

from .storage import checked_local_path, identifier, load_config, path_identity, probe_lock, readonly_database

SCHEMA = 1
MAX_ENTRIES = 20000
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_READ_BYTES = 128 * 1024 * 1024
ACTIONS = {
    'records.inventory': ({'project'}, {'config', 'kind', 'cursor', 'limit', 'known_configs'}),
    'records.inspect': ({'project', 'kind', 'record_id'}, {'config', 'cursor', 'limit', 'known_configs'}),
}
TASK_STATES = {'QUEUED', 'RUNNING', 'PLAN_QUEUED', 'PLAN_RUNNING', 'SUMMARY_QUEUED', 'SUMMARY_RUNNING', 'ROLE_QUEUED', 'ROLE_RUNNING', 'DONE', 'ERROR', 'TIMEOUT', 'CANCELLED', 'INTERRUPTED'}
FLOW_STATES = {'PREPARING', 'DEVELOPING', 'TESTING', 'REVIEWING', 'REVIEW_DONE', 'ERROR', 'DEVELOPER_FAILED', 'TESTS_FAILED', 'REVIEWER_FAILED', 'REVIEWER_MODIFIED_FILES', 'DEVELOPMENT_CHANGED_AFTER_REVIEW_SNAPSHOT', 'ARCHIVED'}
TERMINAL = {'DONE', 'ERROR', 'TIMEOUT', 'CANCELLED', 'INTERRUPTED'}
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.aws', '.ssh', '.codex'}
SHA = re.compile(r'^[0-9a-f]{64}$')


class ObservationError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def object_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(value, dict):
        raise ValueError('Expected object')
    return value


def relative(value):
    if (not isinstance(value, str) or not value or len(value) > 2048 or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value) or PurePosixPath(value).is_absolute()
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Invalid relative path')
    return value


def same(a, b):
    return isinstance(a, (str, Path)) and path_identity(a) == path_identity(b)


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def page_args(kind, cursor, limit, detail=False):
    if kind not in ('task', 'flow') or type(limit) is not int or not 1 <= limit <= 100 or not isinstance(cursor, str):
        raise ValueError('Invalid record page')
    if cursor:
        if detail:
            if not re.fullmatch(r'[0-9a-f]{64}', cursor):
                raise ValueError('Invalid reference cursor')
        else:
            identifier(cursor)


class Observer:
    def __init__(self, project):
        self.project = checked_local_path(project)
        if not self.project.is_dir():
            raise ValueError('Expected project directory')
        self.refs = {}
        self.observed = {}
        self.images = {}
        self.blockers = set()
        self.work = 0
        self.read_bytes = 0
        self.config = None
        self.config_path = None

    def budget(self, count=1):
        self.work += count
        if self.work > MAX_ENTRIES:
            self.blockers.add('INVENTORY_LIMIT')
            raise ObservationError('INVENTORY_LIMIT')

    def stamp(self, path):
        path = checked_local_path(path)
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ObservationError('UNSAFE_PATH')
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink]

    def observe(self, path):
        path = checked_local_path(path)
        key = path_identity(path)
        current = self.stamp(path)
        if key in self.observed and self.observed[key][1] != current:
            raise ObservationError('DATA_CHANGED')
        if key not in self.observed:
            self.budget()
            self.observed[key] = (path, current)
        return current

    def add(self, location, category, kind, *, status='present', size=None, reason=None, physical=False, link=None):
        key = digest([path_identity(location) if physical else location, physical])
        value = self.refs.get(key)
        if value:
            if value['category'] != category:
                value['category'] = 'unknown'; self.blockers.add('REFERENCE_SCOPE_UNKNOWN')
            if link and link not in value['links']:
                self.budget(); value['links'].append(link)
            return value
        self.budget()
        value = dict(id=key, location=str(location), category=category, kind=kind, status=status, bytes=size,
                     physical=physical, links=[link] if link else [])
        if reason:
            value['reason'] = reason
        self.refs[key] = value
        if status == 'unknown':
            self.blockers.add(reason or 'REFERENCE_SCOPE_UNKNOWN')
        return value

    def file(self, path, category, kind, *, link=None):
        try:
            current = self.observe(path)
            if current and not stat.S_ISREG(current[4]):
                raise ObservationError('UNSAFE_PATH')
            return self.add(str(path), category, kind, status='present' if current else 'absent', size=current[2] if current else 0, physical=True, link=link)
        except (OSError, ValueError) as error:
            if isinstance(error, ObservationError) and str(error) == 'INVENTORY_LIMIT':
                raise
            return self.add(str(path), category, kind, status='unknown', reason='UNSAFE_OR_UNREADABLE', physical=True, link=link)

    def read(self, path, maximum=MAX_JSON_BYTES):
        before = self.observe(path)
        if before is None:
            return None
        if not stat.S_ISREG(before[4]) or before[2] > maximum:
            self.blockers.add('METADATA_LIMIT')
            raise ObservationError('METADATA_LIMIT')
        self.read_bytes += before[2]
        if self.read_bytes > MAX_READ_BYTES:
            self.blockers.add('READ_LIMIT')
            raise ObservationError('READ_LIMIT')
        with checked_local_path(path).open('rb') as stream:
            data = stream.read(maximum+1)
        if len(data) != before[2] or self.observe(path) != before:
            raise ObservationError('DATA_CHANGED')
        return data

    def json(self, path, category='body', kind='metadata', *, required=False):
        ref = self.file(path, category, kind)
        try:
            raw = self.read(path)
            if raw is None:
                if required:
                    raise ObservationError('METADATA_MISSING')
                return None
            value = object_json(raw.decode('utf-8-sig'))
            ref['sha256'] = hashlib.sha256(raw).hexdigest()
            return value
        except (OSError, ValueError, RecursionError) as error:
            code = str(error) if isinstance(error, ObservationError) else 'METADATA_UNKNOWN'
            ref.update(status='unknown', bytes=None, reason=code); self.blockers.add(code)
            return None

    def entries(self, root):
        info = self.observe(root)
        if info is None:
            return []
        if not stat.S_ISDIR(info[4]):
            raise ObservationError('UNSAFE_PATH')
        values = []
        with os.scandir(root) as stream:
            for entry in stream:
                self.budget(); values.append(Path(entry.path))
        return sorted(values, key=lambda p: p.name)

    def tree(self, root, classifier):
        pending = [root]
        device = self.project.stat().st_dev
        while pending:
            path = pending.pop()
            try:
                info = self.observe(path)
                if info is None:
                    self.add(str(root), 'body', 'artifact_tree', status='absent', size=0)
                    continue
                if info[0] != device or any(part in EXCLUDED for part in path.relative_to(root).parts):
                    raise ObservationError('UNEXPECTED_EXCLUDED_DATA')
                if stat.S_ISDIR(info[4]):
                    pending.extend(reversed(self.entries(path)))
                else:
                    category, kind = classifier(path.relative_to(root).as_posix())
                    self.file(path, category, kind)
            except (OSError, ValueError) as error:
                if isinstance(error, ObservationError) and str(error) == 'INVENTORY_LIMIT':
                    raise
                self.add(str(path), 'unknown', 'artifact', status='unknown', reason=str(error) if isinstance(error, ObservationError) else 'UNSAFE_OR_UNREADABLE')

    def database(self, path):
        key = path_identity(path)
        if key in self.images:
            return self.images[key]
        ref = self.file(path, 'control', 'ledger_container')
        if ref['status'] == 'absent':
            self.images[key] = None; return None
        try:
            info = self.observe(path)
            if info is None or info[2] > MAX_DATABASE_BYTES:
                raise ObservationError('DATABASE_SIZE_LIMIT')
            self.read_bytes += info[2]
            if self.read_bytes > MAX_READ_BYTES:
                raise ObservationError('READ_LIMIT')
            db = readonly_database(checked_local_path(path))
            self.images[key] = db
            self.observe(path)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'tasks' not in tables:
                raise ObservationError('BATCH_SCHEMA_UNKNOWN')
            for table in ('tasks', 'role_tasks', 'context_plans', 'usage_snapshots'):
                if table in tables:
                    count = db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                    self.budget(count)
            return db
        except (OSError, ValueError, sqlite3.Error) as error:
            db = self.images.pop(key, None)
            if db is not None:
                db.close()
            code = str(error) if isinstance(error, ObservationError) else 'DATABASE_UNREADABLE'
            self.blockers.add(code); ref.update(status='unknown', bytes=None, reason=code)
            self.images[key] = None
            return None

    def table(self, db, name):
        return db is not None and bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def locks(self, state, flow=None):
        names = ['runner.lock', 'runner-management.lock', 'promotion.lock']
        if flow:
            names += ['flow-locks/'+flow+'.lock', 'p1-jobs/start.lock']
        for name in names:
            target = state / name
            item = self.file(target, 'control', 'lock')
            try:
                value = probe_lock(checked_local_path(target))
                item['lock'] = value
                if value == 'LOCKED':
                    self.blockers.add('WRITER_ACTIVE')
            except (OSError, ValueError):
                item.update(status='unknown', reason='LOCK_UNKNOWN'); self.blockers.add('LOCK_UNKNOWN')

    def setup(self, config, known_configs):
        self.config_path = Path(config) if config else self.project / 'bridge.json'
        raw = self.json(self.config_path, 'external', 'selected_config')
        if raw is None:
            self.blockers.add('CONFIG_UNKNOWN'); return
        try:
            loaded = load_config(self.config_path, data=raw)
            checked_local_path(loaded['root']); checked_local_path(loaded['root'] / raw.get('state', '.agent-bridge'))
            if not same(loaded['root'], self.project):
                raise ValueError('Project mismatch')
            self.config = loaded
        except (OSError, ValueError, KeyError, TypeError):
            self.blockers.add('CONFIG_UNKNOWN'); return
        for candidate in known_configs:
            if same(candidate, self.config_path):
                continue
            info = self.json(Path(candidate), 'external', 'known_config')
            if info is None:
                if self.stamp(candidate) is not None:
                    self.blockers.add('KNOWN_CONFIG_UNKNOWN')
                continue
            try:
                other = load_config(candidate, data=info)
                checked_local_path(other['root'] / info.get('state', '.agent-bridge'))
                a, b = path_identity(loaded['state']), path_identity(other['state'])
                if a == b or Path(a).is_relative_to(b) or Path(b).is_relative_to(a):
                    self.blockers.add('SHARED_STATE_UNCONFIRMED')
            except (OSError, ValueError, KeyError, TypeError):
                self.blockers.add('KNOWN_CONFIG_UNKNOWN')

    def payload(self, path, table, task, raw, category='body'):
        size = len(raw.encode('utf-8')) if isinstance(raw, str) else 0 if raw is None else None
        self.add(str(path)+'#'+table+'/'+task, category, table, size=size)

    def external(self, value, link, input_root=None):
        try:
            target = (input_root or self.project) / relative(value)
            internal = input_root is not None and not same(input_root, self.project)
            self.file(target, 'body' if internal else 'external', 'review_input' if internal else 'original_input', link=link)
        except (ValueError, TypeError):
            self.blockers.add('REFERENCE_SCOPE_UNKNOWN')

    def task(self, dbpath, task_id, home=None, *, role=None, input_root=None):
        db = self.database(dbpath)
        if db is None:
            return None
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            self.blockers.add('RECORD_MISSING'); return None
        value = dict(row)
        from .records_guard import decorate
        decorate(db,value)
        state = value.get('state') if value.get('state') in TASK_STATES else 'UNKNOWN'
        if state not in TERMINAL:
            self.blockers.add('TASK_NOT_TERMINAL')
        for field in ('prompt', 'result'):
            self.payload(dbpath, 'tasks.'+field, task_id, value.get(field))
        self.payload(dbpath, 'tasks.identity', task_id, None, 'control')
        for table, category in [('usage_snapshots', 'control'), ('context_plans', 'body'), ('role_tasks', 'body'), ('artifact_cleanup', 'control')]:
            if not self.table(db, table):
                if table == 'usage_snapshots':
                    self.add(str(dbpath)+'#'+table+'/'+task_id, 'control', table, status='unknown', reason='LEGACY_SCHEMA')
                continue
            saved = db.execute('SELECT * FROM '+table+' WHERE id=?', (task_id,)).fetchone()
            if saved is None:
                continue
            data = dict(saved).get('data')
            self.payload(dbpath, table, task_id, data, category)
            if table == 'context_plans' and isinstance(data, str) and value.get('body_state') not in ('PURGING','DELETED'):
                try:
                    if len(data.encode()) > MAX_JSON_BYTES:
                        raise ObservationError('METADATA_LIMIT')
                    payload = object_json(data)['payload']
                    for source in payload.get('selected', []):
                        self.budget(); self.external(source['path'], task_id, input_root)
                    for summary in payload.get('summaries', []):
                        self.budget(); self.external(summary['record_path'], task_id, input_root)
                        record = summary['record']; self.external(record['body_path'], task_id, input_root)
                        for source in record['sources']:
                            self.budget(); self.external(source['path'], task_id, input_root)
                except (KeyError, TypeError, ValueError, RecursionError):
                    self.blockers.add('CONTEXT_SCOPE_UNKNOWN')
        try:
            raw = value.get('result')
            if raw and len(raw.encode()) > MAX_JSON_BYTES:
                raise ValueError('Result limit')
            result = object_json(raw) if raw else {}
            if home is not None and result.get('workspace') is not None and not same(result['workspace'], home):
                self.blockers.add('WORKSPACE_SCOPE_UNKNOWN')
        except (ValueError, TypeError, RecursionError):
            self.blockers.add('RESULT_UNKNOWN')
        return dict(id=task_id, state=state, role=role, body_state=value.get('body_state'))

    def controls(self, path):
        db = self.database(path)
        for table in ('control', 'runner_lifecycle'):
            self.add(str(path)+'#'+table, 'control', table,
                     status='present' if self.table(db, table) else 'unknown', reason=None if self.table(db, table) else 'CONTROL_UNKNOWN')

    def consumers(self, path, selected):
        db = self.database(path)
        if not self.table(db, 'context_plans'):
            return
        for row in db.execute('SELECT id,data FROM context_plans'):
            try:
                consumer = identifier(row['id'])
                if consumer in selected:
                    continue
                if len(row['data'].encode()) > MAX_JSON_BYTES:
                    raise ValueError('Context size')
                saved = object_json(row['data'])
                if saved.get('body_state') in ('PURGING','DELETED'):
                    continue
                payload = saved['payload']
                for summary in payload.get('summaries', []):
                    self.budget()
                    source = summary['record'].get('provenance', {}).get('source_task_id')
                    if source in selected:
                        ref = self.add(str(path)+'#context_plans/'+consumer, 'external', 'downstream_task_context', size=len(row['data'].encode()), link=source)
                        ref['related_task_id'] = consumer
                        self.blockers.add('RELATED_TASK_REFERENCES')
            except (KeyError, ValueError, TypeError, RecursionError):
                self.blockers.add('CONTEXT_SCOPE_UNKNOWN')

    def verify(self):
        for path, before in self.observed.values():
            try:
                if self.stamp(path) != before:
                    self.blockers.add('DATA_CHANGED')
            except (OSError, ValueError):
                self.blockers.add('DATA_CHANGED')

    def close(self):
        for db in self.images.values():
            if db is not None:
                db.close()


def classify_task(name):
    first = name.split('/')[0]
    if first.startswith('backup-'):
        return 'recovery', 'promotion_backup'
    if first == 'project':
        return 'body', 'working_copy'
    if first in {'prompt.txt', 'context.json', 'process.json', 'events.jsonl', 'stderr.log', 'answer.txt', 'provider-reply.txt', 'baseline.json', 'changes.json', 'exit-code.txt'}:
        return 'body', 'task_artifact'
    return 'unknown', 'unrecognized_artifact'


def classify_flow(name):
    parts = name.split('/')
    if any(p.startswith('backup-') for p in parts):
        return 'recovery', 'promotion_backup'
    if parts[-1] in {'queue.sqlite3', 'runner.lock', 'runner-management.lock', 'promotion.lock'}:
        return 'control', 'ledger_container' if parts[-1] == 'queue.sqlite3' else 'lock'
    if parts[0] == 'archive':
        return 'body', 'archive_blob' if len(parts) == 3 and parts[1] == 'blobs' else 'archive_metadata'
    if parts[0] in {'original-input', 'review-input', 'developer-state'}:
        return 'body', 'role_copy_or_input'
    if len(parts) == 1 and (parts[0] in {'result.json', 'cleanup.json'} or re.fullmatch(r'(developer|reviewer)(-config\.json|-submitted\.json|\.log)|test-\d+\.log', parts[0])):
        return 'body', 'flow_metadata_or_log'
    return 'unknown', 'unrecognized_artifact'


def archive(obs, flow):
    manifest = obs.json(flow / 'archive/manifest.json', 'body', 'archive_metadata')
    records = obs.json(flow / 'archive/records.json', 'body', 'archive_metadata')
    if manifest is None and records is None:
        return
    try:
        if not manifest or not records or set(manifest) != {'schema','flow','entries','records_sha256','archive_digest'} or manifest.get('schema') != 1 or records.get('schema') != 1 or manifest.get('flow') != flow.name or records.get('flow') != flow.name:
            raise ValueError('Archive identity')
        raw = obs.read(flow / 'archive/records.json')
        if hashlib.sha256(raw).hexdigest() != manifest['records_sha256']:
            raise ValueError('Archive records')
        claimed = manifest['archive_digest']
        body = {k:v for k,v in manifest.items() if k != 'archive_digest'}
        canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
        if hashlib.sha256(canonical).hexdigest() != claimed:
            raise ValueError('Archive manifest')
        seen, blobs = set(), set()
        for entry in manifest['entries']:
            obs.budget()
            source = entry['source']; rel = relative(entry['path']); sha = entry['sha256']
            if source not in {'original_input', 'review_input', 'developer_workspace', 'reviewer_workspace', 'flow_metadata'} or not isinstance(sha, str) or not SHA.fullmatch(sha) or type(entry['size']) is not int or entry['size'] < 0 or (source,rel) in seen:
                raise ValueError('Archive entry')
            seen.add((source,rel)); blobs.add(sha)
            target = flow / 'archive/blobs' / sha
            ref = obs.file(target, 'body', 'archive_blob', link=source+':'+rel)
            if ref['status'] != 'present' or ref['bytes'] != entry['size']:
                raise ValueError('Missing blob')
            if 'sha256' not in ref:
                data = obs.read(target, MAX_DATABASE_BYTES)
                ref['sha256'] = hashlib.sha256(data).hexdigest()
            if ref['sha256'] != sha:
                raise ValueError('Damaged blob')
            obs.add(str(flow / 'archive/manifest.json')+'#'+source+'/'+rel, 'body', 'archive_reference', size=0, link=sha)
        actual = {p.name for p in obs.entries(flow / 'archive/blobs')}
        if actual != blobs:
            raise ValueError('Unreferenced blob')
        if {p.name for p in obs.entries(flow / 'archive')} != {'blobs','records.json','manifest.json'}:
            raise ValueError('Unrecognized archive item')
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        obs.blockers.add('ARCHIVE_UNKNOWN')


def project_refs(obs):
    state = obs.config['state']
    obs.locks(state)
    for name in ('runner-launch.json', 'health.json', 'service.json'):
        ref = obs.file(state / name, 'control', 'execution_receipt')
        if name == 'service.json' and ref['status'] != 'absent':
            obs.blockers.add('SERVICE_REGISTRATION_PRESENT')
    obs.file(obs.project / 'role_workflow.json', 'external', 'user_policy')
    obs.file(obs.project / 'tasks/request.md', 'external', 'user_request')
    manifest = obs.json(obs.project / '.bridge-integration.json', 'control', 'connection_manifest')
    if manifest is not None:
        try:
            if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
                raise ValueError('Manifest')
            for name, expected in manifest['files'].items():
                obs.budget(); target = obs.project / relative(name)
                ref = obs.file(target, 'external', 'connection_asset')
                if name in ('bridge.json', 'role_workflow.json', 'tasks/request.md') or not isinstance(expected, str) or not SHA.fullmatch(expected):
                    raise ValueError('Manifest ownership')
                data = obs.read(target, MAX_DATABASE_BYTES)
                ref['ownership'] = 'MANIFEST_HASH_MATCH' if data is not None and hashlib.sha256(data).hexdigest() == expected else 'MODIFIED_OR_MISSING'
                if ref['ownership'] != 'MANIFEST_HASH_MATCH':
                    obs.blockers.add('CONNECTION_ASSET_CHANGED')
        except (OSError, ValueError, TypeError):
            obs.blockers.add('CONNECTION_MANIFEST_UNKNOWN')
    obs.tree(obs.project / '.bridge-integration-backups', lambda _: ('recovery', 'connection_backup'))


def flow_detail(obs, flow_id):
    project, state = obs.project, obs.config['state']
    from . import records_flow
    owned=records_flow.status(obs.config,flow_id)
    if owned and owned['body_state']!='NORMAL':
        obs.controls(state/'queue.sqlite3')
        return dict(id=flow_id,kind='flow',state='BODY_DELETED' if owned['body_state']=='DELETED' else 'BODY_PURGING',body_state=owned['body_state'],ownership='PREPARED_OWNED',queue_mode='SHARED_PARENT',roles=[])
    flow = project / '.role-flows' / flow_id
    saved = obs.json(flow / 'result.json', 'body', 'flow_metadata_or_log')
    selection = dict(id=flow_id, kind='flow', state='UNKNOWN', ownership='UNCONFIRMED', queue_mode='UNKNOWN')
    if saved:
        if saved.get('id') != flow_id:
            obs.blockers.add('FLOW_IDENTITY_UNKNOWN')
        selection['state'] = saved.get('state') if saved.get('state') in FLOW_STATES else 'UNKNOWN'
        selection['queue_mode'] = 'SHARED_PARENT' if saved.get('queue_mode') == 'SHARED_PARENT' else 'PRIVATE' if saved.get('queue_mode') is None else 'UNKNOWN'
    else:
        obs.blockers.add('FLOW_METADATA_MISSING')
    if selection['state'] in {'PREPARING','DEVELOPING','TESTING','REVIEWING','UNKNOWN'}:
        obs.blockers.add('FLOW_NOT_TERMINAL')
    obs.locks(state, flow_id)
    obs.tree(flow, classify_flow)
    roles = []
    for role in ('developer', 'reviewer'):
        role_state = flow / ('developer-state' if role == 'developer' else 'review-input/.agent-bridge')
        obs.locks(role_state)
        raw_config = obs.json(flow / (role+'-config.json'), 'body', 'flow_metadata_or_log')
        submitted = obs.json(flow / (role+'-submitted.json'), 'body', 'flow_metadata_or_log')
        meta = (saved or {}).get(role)
        if meta is None:
            if raw_config or submitted:
                obs.blockers.add('ROLE_SCOPE_UNKNOWN')
            continue
        try:
            task = identifier(meta['task']); root = project if role == 'developer' else flow / 'review-input'
            if not raw_config:
                raise ValueError('Missing role config')
            cfg = load_config(flow / (role+'-config.json'), data=raw_config)
            queue = state if selection['queue_mode'] == 'SHARED_PARENT' else role_state
            if not same(cfg['root'], root) or not same(cfg['state'], role_state) or not same(meta.get('config'), flow / (role+'-config.json')):
                raise ValueError('Role path mismatch')
            if not submitted or submitted.get('task') != task or not same(submitted.get('config'), flow / (role+'-config.json')):
                raise ValueError('Role receipt mismatch')
            if selection['queue_mode'] == 'SHARED_PARENT' and (not same(meta.get('queue_state'), queue) or not same(submitted.get('queue_state'), queue)):
                raise ValueError('Queue mismatch')
            dbpath = queue / 'queue.sqlite3'; db = obs.database(dbpath)
            if db is None:
                raise ValueError('Queue unavailable')
            row = db.execute('SELECT endpoint,result FROM tasks WHERE id=?', (task,)).fetchone()
            home = role_state / 'workspaces' / task
            if not row or row['endpoint'] != role or not same(object_json(row['result'] or '{}').get('workspace'), home):
                raise ValueError('Task mismatch')
            if selection['queue_mode'] == 'SHARED_PARENT':
                linked = db.execute('SELECT flow,role,data FROM role_tasks WHERE id=?', (task,)).fetchone()
                if not linked or linked['flow'] != flow_id or linked['role'] != role or object_json(linked['data']).get('config') != raw_config:
                    raise ValueError('Frozen role mismatch')
            record = obs.task(dbpath, task, home, role=role, input_root=root)
            obs.controls(dbpath); roles.append(record)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RecursionError):
            obs.blockers.add('ROLE_SCOPE_UNKNOWN')
    selection['roles'] = roles
    if selection['queue_mode'] == 'SHARED_PARENT':
        db = obs.database(state / 'queue.sqlite3')
        if not obs.table(db,'role_tasks') or {(row['id'],row['role']) for row in db.execute('SELECT id,role FROM role_tasks WHERE flow=?',(flow_id,))} != {(row['id'],row['role']) for row in roles if row}:
            obs.blockers.add('ROLE_SCOPE_UNKNOWN')
    elif selection['queue_mode'] == 'PRIVATE':
        parent = obs.database(state / 'queue.sqlite3')
        if obs.table(parent, 'role_tasks') and parent.execute('SELECT 1 FROM role_tasks WHERE flow=? LIMIT 1', (flow_id,)).fetchone():
            obs.blockers.add('ROLE_SCOPE_UNKNOWN')
    if roles and not obs.blockers.intersection({'ROLE_SCOPE_UNKNOWN', 'FLOW_IDENTITY_UNKNOWN', 'FLOW_METADATA_MISSING', 'SHARED_STATE_UNCONFIRMED'}):
        selection['ownership'] = 'ROLE_LINK_VERIFIED'
    archive(obs, flow)
    journal = obs.json(flow / 'cleanup.json', 'body', 'flow_metadata_or_log')
    expected_stage = None
    if journal:
        try:
            operation = identifier(journal['operation'])
            stage = state / ('archive-staging-'+operation)
            if journal.get('flow') != flow_id or not same(project / relative(journal['staging']), stage):
                raise ValueError('Staging identity')
            expected_stage = stage
            obs.tree(stage, lambda _: ('body', 'archive_staging'))
            if journal.get('state') != 'ARCHIVE_READY':
                obs.blockers.add('ARCHIVE_INCOMPLETE')
        except (KeyError, ValueError, TypeError):
            obs.blockers.add('STAGING_SCOPE_UNKNOWN')
    for entry in obs.entries(state):
        if not entry.name.startswith('archive-staging-') or same(entry, expected_stage or ''):
            continue
        try:
            raw = obs.read(entry / 'records.json')
            meta = object_json(raw) if raw is not None else None
            if not meta or not isinstance(meta.get('flow'), str):
                raise ValueError('Unattributed staging')
            identifier(meta['flow'])
            if meta['flow'] == flow_id:
                obs.tree(entry, lambda _: ('body','archive_staging'))
                obs.blockers.add('STAGING_SCOPE_UNKNOWN')
        except (OSError, ValueError, TypeError, RecursionError):
            obs.add(str(entry), 'unknown', 'unattributed_staging', status='unknown', reason='STAGING_SCOPE_UNKNOWN')
    job = state / 'p1-jobs' / flow_id
    receipt = obs.json(Path(str(job)+'.json'), 'body', 'role_job_receipt')
    for suffix in ('.txt', '.log'):
        obs.file(Path(str(job)+suffix), 'body', 'role_job_payload')
    if receipt:
        obs.add(str(job)+'.json#identity', 'control', 'role_job_identity')
        if receipt.get('flow') != flow_id or not same(receipt.get('config'), obs.config_path):
            obs.blockers.add('ROLE_JOB_SCOPE_UNKNOWN')
        if receipt.get('state') not in ('FINISHED', 'FAILED') or not finite(receipt.get('finished')):
            obs.blockers.add('ROLE_JOB_UNCONFIRMED')
    obs.consumers(state / 'queue.sqlite3', {row['id'] for row in roles if row})
    return selection


def inventory(obs, kind, cursor, limit):
    state = obs.config['state']; db = obs.database(state / 'queue.sqlite3')
    from . import records_guard
    prepared = records_guard.validate(db) if db is not None else None
    if prepared and (not same(prepared['project'],obs.project) or not same(prepared['config'],obs.config_path)):
        obs.blockers.add('RECORDS_PROJECT_MISMATCH')
    ids, items = [], []
    if kind == 'task':
        if db is None:
            return dict(items=[], more=False, next_cursor=None, queue_body_schema=0)
        rows = db.execute('SELECT id,state FROM tasks WHERE id>? ORDER BY id LIMIT ?', (cursor,limit+1)).fetchall()
        for row in rows:
            identifier(row['id']); ids.append(row['id'])
            if len(items) == limit:
                break
            linked = db.execute('SELECT flow,role FROM role_tasks WHERE id=?', (row['id'],)).fetchone() if obs.table(db, 'role_tasks') else None
            item = dict(id=row['id'], kind='task', state=row['state'] if row['state'] in TASK_STATES else 'UNKNOWN', ownership='LEGACY_UNATTRIBUTED')
            if prepared:
                records_guard.decorate(db,item)
                own=db.execute('SELECT project,config FROM records_tasks WHERE task=?',(row['id'],)).fetchone()
                if own and same(own['project'],obs.project) and same(own['config'],obs.config_path):
                    item['ownership']='PREPARED_OWNED'
                item['body_selectable']=item['ownership']=='PREPARED_OWNED' and item.get('body_state')=='NORMAL' and item['state'] in TERMINAL
            if linked:
                identifier(linked['flow'])
                item.update(ownership='ROLE_REFERENCE', body_selectable=False, flow=linked['flow'], role=linked['role'] if linked['role'] in ('developer','reviewer') else 'UNKNOWN')
            items.append(item)
    else:
        names = {p.name for p in obs.entries(obs.project / '.role-flows')}
        for path in obs.entries(state / 'p1-jobs'):
            if path.suffix == '.json':
                names.add(path.stem)
        if obs.table(db, 'role_tasks'):
            names.update(row[0] for row in db.execute('SELECT DISTINCT flow FROM role_tasks'))
        if prepared and prepared['version']==2:
            names.update(row[0] for row in db.execute('SELECT flow FROM records_flows'))
        valid = []
        for name in names:
            try:
                identifier(name)
                if name > cursor:
                    valid.append(name)
            except ValueError:
                obs.blockers.add('UNKNOWN_FLOW_ENTRY')
        ids = sorted(valid)[:limit+1]
        for name in ids[:limit]:
            saved = obs.json(obs.project / '.role-flows' / name / 'result.json', 'body', 'flow_metadata_or_log')
            item=dict(id=name, kind='flow', state=(saved or {}).get('state') if (saved or {}).get('state') in FLOW_STATES else 'UNKNOWN', ownership='UNCONFIRMED',body_selectable=False)
            owned=db.execute('SELECT * FROM records_flows WHERE flow=?',(name,)).fetchone() if prepared and prepared['version']==2 else None
            if owned:
                item.update(ownership='PREPARED_OWNED',body_state=owned['body_state'],body_selectable=owned['body_state']=='NORMAL' and owned['writer_state']=='CLOSED')
                if owned['body_state']!='NORMAL': item['state']='BODY_DELETED' if owned['body_state']=='DELETED' else 'BODY_PURGING'
            items.append(item)
    pending = [dict(operation=identifier(row['operation']), state=row['state'] if row['state'] in ('PURGING','PARTIAL') else 'UNKNOWN') for row in db.execute("SELECT operation,state,data FROM records_operations WHERE state!='DONE' ORDER BY operation LIMIT 8") if object_json(row['data']).get('kind') in ('purge','flow_purge')] if prepared else []
    return dict(items=items, more=len(ids)>limit, next_cursor=items[-1]['id'] if len(ids)>limit else None, queue_body_schema=prepared['version'] if prepared else 0, pending_body_operations=pending)


def inspect(project, config=None, kind='task', cursor='', limit=25, known_configs=None, record_id=None):
    page_args(kind, cursor, limit, record_id is not None)
    if record_id is not None:
        identifier(record_id)
    if known_configs is None:
        known_configs = []
    if not isinstance(known_configs, list) or len(known_configs)>16 or any(not isinstance(p, str) or not Path(p).is_absolute() for p in known_configs):
        raise ValueError('Select at most 16 absolute known configuration paths')
    obs = Observer(project)
    result = dict(schema=SCHEMA, kind=kind, project=str(obs.project), read_only=True, mutation_supported=False,
                  consistency='OBSERVATION_ONLY', scope='selected_config_and_known_references', exclusions=['other_workspaces_and_unknown_configs', 'provider_history', 'authentication', 'global_profiles'],
                  items=[], more=False, next_cursor=None, complete=False)
    try:
        obs.setup(config, known_configs)
        if obs.config:
            result.update(config=str(obs.config_path), database=str(obs.config['state'] / 'queue.sqlite3'))
            if record_id is None:
                result.update(inventory(obs, kind, cursor, limit))
            else:
                if kind == 'task':
                    dbpath = obs.config['state'] / 'queue.sqlite3'; db = obs.database(dbpath)
                    linked = db.execute('SELECT flow,role FROM role_tasks WHERE id=?', (record_id,)).fetchone() if obs.table(db, 'role_tasks') else None
                    if linked:
                        identifier(linked['flow'])
                        result['selection'] = flow_detail(obs, linked['flow'])
                        result['selection'].update(requested_task=record_id, required_scope='whole_flow')
                    else:
                        home = obs.config['state'] / 'workspaces' / record_id
                        row = obs.task(dbpath, record_id, home)
                        result['selection'] = dict(kind='task', ownership='LEGACY_UNATTRIBUTED', **(row or dict(id=record_id,state='UNKNOWN')))
                        obs.tree(home, classify_task); obs.controls(dbpath)
                        obs.consumers(dbpath, {record_id})
                        obs.blockers.add('LEGACY_OWNERSHIP_UNCONFIRMED')
                else:
                    result['selection'] = flow_detail(obs, record_id)
                project_refs(obs)
                if any(r['category']=='recovery' and r['status']!='absent' for r in obs.refs.values()):
                    obs.blockers.add('RECOVERY_DATA_PRESERVED')
                if any(r['category']=='unknown' for r in obs.refs.values()):
                    obs.blockers.add('REFERENCE_SCOPE_UNKNOWN')
        obs.verify()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RecursionError) as error:
        obs.blockers.add(str(error) if isinstance(error, ObservationError) else 'INVENTORY_UNKNOWN')
        result.update(items=[], more=False, next_cursor=None)
    finally:
        obs.close()
    unknown = {'INVENTORY_LIMIT','READ_LIMIT','METADATA_LIMIT','DATABASE_SIZE_LIMIT','DATABASE_UNREADABLE','BATCH_SCHEMA_UNKNOWN','CONFIG_UNKNOWN','KNOWN_CONFIG_UNKNOWN','DATA_CHANGED','INVENTORY_UNKNOWN','UNSAFE_OR_UNREADABLE','REFERENCE_SCOPE_UNKNOWN','ROLE_SCOPE_UNKNOWN','ARCHIVE_UNKNOWN','CONTEXT_SCOPE_UNKNOWN','STAGING_SCOPE_UNKNOWN','RESULT_UNKNOWN','FLOW_METADATA_MISSING','WORKSPACE_SCOPE_UNKNOWN','RECORD_MISSING','UNKNOWN_FLOW_ENTRY','FLOW_IDENTITY_UNKNOWN','ROLE_JOB_SCOPE_UNKNOWN'}
    result['complete'] = not bool(obs.blockers & unknown) and not any(ref['status']=='unknown' for ref in obs.refs.values())
    result['blockers'] = sorted(obs.blockers)
    result['mutation_reason'] = 'READ_ONLY_STAGE'
    if record_id is not None:
        refs = sorted(obs.refs.values(), key=lambda ref: ref['id'])
        page = [dict(ref, links=ref['links'][:8], link_count=len(ref['links']), links_more=len(ref['links'])>8) for ref in refs if ref['id'] > cursor]
        result.update(references=page[:limit], more=len(page)>limit, next_cursor=page[limit-1]['id'] if len(page)>limit else None)
        totals = {}
        for category in ('body','control','external','recovery','unknown'):
            group = [r for r in refs if r['category']==category]
            totals[category] = dict(references=len(group), present=sum(r['status']=='present' for r in group),
                                    bytes=sum(r['bytes'] or 0 for r in group) if result['complete'] and all(r['bytes'] is not None for r in group if r['status']!='absent') else None)
        result['summary'] = dict(categories=totals, references=len(refs) if result['complete'] else None,
                               observed_references=len(refs), physical_files=sum(r['physical'] and r['status']=='present' for r in refs) if result['complete'] else None,
                               byte_basis='logical_references_not_reclaimable_space; database_payloads_and_containers_are_separate')
    result['revision'] = digest([[(k,v[1]) for k,v in sorted(obs.observed.items())], result.get('selection'), result['blockers']])
    return result


def dispatch(action, args):
    return inspect(**args)
