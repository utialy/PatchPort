"""Read-only whole-flow cleanup planning. No archive creation or deletion."""
import contextlib
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import time

from agent_bridge import workspace
from agent_bridge.answers import answer_for
from agent_bridge.cleanup import TERMINAL, candidate
from agent_bridge.storage import identifier, load_config, probe_lock, readonly_database
from flow_archive import PHASES, is_archived, load_view, validate_records, _path as archive_path
import flow_lifecycle
from role_review import object_json, resolve, safe

ACTIVE = {'QUEUED', 'RUNNING', 'PLAN_QUEUED', 'PLAN_RUNNING', 'ROLE_QUEUED', 'ROLE_RUNNING'}
STATE_FILES = {'queue.sqlite3', 'queue.sqlite3-wal', 'queue.sqlite3-shm', 'queue.sqlite3-journal',
               'runner.lock', 'promotion.lock', 'health.json'}
TASK_FILES = {'baseline.json', 'changes.json', 'context.json', 'prompt.txt', 'process.json',
              'answer.txt', 'provider-reply.txt', 'events.jsonl', 'stderr.log', 'exit-code.txt'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def valid_time(value):
    if type(value) not in (int, float) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def inventory(root, *, _digest=workspace.digest):
    """Inspect hidden entries too; refuse links, hardlinks and device boundaries."""
    root_device = root.lstat().st_dev
    files, directories = [], []
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_dev != root_device):
            raise ValueError('Unsafe flow entry: link/reparse point or filesystem boundary')
        rel = path.relative_to(root).as_posix()
        if not valid_time(info.st_mtime):
            raise ValueError('Invalid artifact timestamp')
        item = dict(path=rel, mtime_ns=info.st_mtime_ns, size=info.st_size)
        if stat.S_ISDIR(info.st_mode):
            directories.append(dict(item, kind='directory'))
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            digest = _digest(path)
            after = path.lstat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('Artifact changed during inventory')
            files.append(dict(item, kind='file', sha256=digest))
        else:
            raise ValueError('Unsupported artifact type or hardlink')
    return sorted(files, key=lambda x:x['path']), sorted(directories, key=lambda x:x['path'])


def database_rows(path):
    with contextlib.closing(readonly_database(path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tasks' not in tables:
            raise ValueError('Task table missing')
        rows = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')]
        roles = [dict(r) for r in db.execute('SELECT * FROM role_tasks ORDER BY id')] if 'role_tasks' in tables else []
        cleanups = {r['id']:r['state'] for r in db.execute('SELECT * FROM artifact_cleanup')} if 'artifact_cleanup' in tables else {}
        return dict(rows=rows, roles=roles, cleanups=cleanups,
                    digest=hashlib.sha256(db.serialize()).hexdigest())


def protected_source(path):
    parts = Path(path).parts
    return any(p in workspace.EXCLUDED - {'__pycache__'} or p in {'.ssh', '.aws', '.codex'} for p in parts) or any(
        p == '.env' or p.startswith('.env.') or p in {'auth.json', 'credentials.json', '.credentials.json'} for p in parts)


def preview(project, flow_id, days=30, *, now=None, _held_locks=(), _inventory=None, _details=None,
            _archive_operation=None):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('days must be 1..3650')
    identifier(flow_id)
    project = Path(project).resolve()
    base_data = object_json(safe(project, 'bridge.json'))
    base = load_config(project/'bridge.json', data=base_data)
    if base['root'] != project:
        raise ValueError('Requires configured project root')
    if Path.cwd().resolve().is_relative_to(project / '.role-flows') or Path.cwd().resolve().is_relative_to(base['state'] / 'workspaces'):
        raise ValueError('Workers cannot inspect original workflows')
    now = time.time() if now is None else now
    if not valid_time(now):
        raise ValueError('Invalid observation time')
    flow = safe(project, '.role-flows/' + flow_id)
    guard = flow_lifecycle.lock_path(project, base, flow_id)
    states = [base['state'], safe(project, '.role-flows/' + flow_id + '/developer-state'),
              safe(project, '.role-flows/' + flow_id + '/review-input/.agent-bridge')]
    locks = [(guard, 'FLOW_BUSY')]
    for state in states:
        locks.extend((safe(project, (state/name).relative_to(project).as_posix()), reason)
                     for name,reason in (('runner.lock','RUNNER_BUSY'),('promotion.lock','PROMOTION_BUSY')))
    result = dict(schema=1, id=flow_id, apply=False, apply_supported=False, read_only=True,
                  consistency='OBSERVATION_ONLY', observed_at=now, retention_days=days,
                  eligible=False, reasons=[], roles={}, locks=[], delete_entries=[], preserve_entries=[],
                  archive_entries=[], replace_entries=[], directories=[], delete_bytes=None,
                  archive_new_bytes=None, estimated_net_reclaim_bytes=None, plan_hash=None,
                  review_is_approval=False)
    def reason(value):
        if value not in result['reasons']:
            result['reasons'].append(value)
    for path, blocked in locks:
        observed = 'FREE' if path in _held_locks else probe_lock(path)
        result['locks'].append(dict(path=path.relative_to(project).as_posix(), observation=observed))
        if observed == 'LOCKED': reason(blocked)
    if result['reasons']:
        return result
    saved = object_json(safe(flow, 'result.json'))
    if saved.get('id') != flow_id:
        raise ValueError('Flow identity mismatch')
    result['saved_phase'] = saved.get('state')
    if is_archived(saved):
        view = load_view(project, flow_id)
        result['artifact_state'] = view['artifact_state']
        reason('ALREADY_COMPACTED' if view['artifact_state']=='COMPACTED' else 'CLEANUP_INCOMPLETE')
        return result
    own_journal = False
    if _archive_operation is not None and (flow/'cleanup.json').exists():
        journal = object_json(safe(flow, 'cleanup.json'))
        own_journal = (journal.get('operation') == _archive_operation and journal.get('flow') == flow_id
                       and journal.get('mode') == 'ARCHIVE_ONLY'
                       and journal.get('state') in ('PREPARING', 'ARCHIVE_FAILED', 'ARCHIVE_READY'))
    if (((flow/'cleanup.json').exists() or (flow/'archive').exists()) and not own_journal) or list(flow.glob('archive.tmp-*')):
        reason('CLEANUP_INCOMPLETE')
        return result
    if saved.get('state') not in PHASES:
        reason('FLOW_NOT_FINISHED')
        return result
    if type(saved.get('flow_lock_schema')) is not int or saved['flow_lock_schema'] != flow_lifecycle.FLOW_LOCK_SCHEMA:
        reason('UPGRADE_REQUIRED')
    elif result['locks'][0]['observation'] == 'MISSING':
        reason('FLOW_LOCK_MISSING')
    if saved.get('queue_mode') not in (None, 'SHARED_PARENT'):
        raise ValueError('Unknown queue mode')
    shared = saved.get('queue_mode') == 'SHARED_PARENT'
    result['queue_mode'] = 'SHARED_PARENT' if shared else 'PRIVATE'
    parent_db = safe(project, (base['state']/'queue.sqlite3').relative_to(project).as_posix())
    parent_data = database_rows(parent_db) if parent_db.exists() else None
    database_observations = {parent_db:parent_data} if parent_data is not None else {}
    if shared and parent_data is None:
        raise ValueError('Shared parent database missing')
    identity = [r for r in (parent_data or {}).get('roles',[]) if r['flow']==flow_id]
    if not shared and identity:
        raise ValueError('Private flow conflicts with parent role identity')
    if any(r['role'] not in ('developer','reviewer') for r in identity):
        raise ValueError('Unknown role in parent queue')
    selected_rows = {r['id']:r for r in parent_data['rows']} if shared else {}
    if shared and any(r['id'] not in selected_rows for r in identity):
        raise ValueError('Parent role task missing')
    records = {}
    db_evidence = []
    finished = []
    homes = {}
    backup_files = set()
    protected_backups = set()
    for index,role in enumerate(('developer','reviewer'), start=1):
        state = states[index]
        db_path = safe(project, (state/'queue.sqlite3').relative_to(project).as_posix())
        local = database_rows(db_path) if db_path.exists() else None
        if local is not None: database_observations[db_path] = local
        if shared and local is not None:
            raise ValueError('Unexpected private queue in shared flow')
        role_rows = [selected_rows[r['id']] for r in identity if r['role']==role] if shared else (local or {}).get('rows',[])
        if any(row['state'] in ACTIVE for row in role_rows):
            reason('ACTIVE_TASK')
        metadata = saved.get(role)
        submitted = flow/(role+'-submitted.json')
        if metadata is None:
            if role_rows or submitted.exists() or (flow/(role+'-config.json')).exists():
                raise ValueError('Role evidence exists without terminal metadata')
            result['roles'][role] = dict(observation='NOT_SUBMITTED')
            records[role] = None
            continue
        if not isinstance(metadata, dict) or len(role_rows) != 1 or metadata.get('task') != role_rows[0]['id']:
            raise ValueError('Role queue identity is missing or ambiguous')
        row = role_rows[0]
        if not isinstance(row['state'],str): raise ValueError('Invalid task state')
        normalized = row['state'].removeprefix('ROLE_').removeprefix('PLAN_')
        result['roles'][role] = dict(observation='READ', task=row['id'], state=normalized,
                                    queue=(base['state'] if shared else state).relative_to(project).as_posix())
        db_evidence.append(dict(role=role, row=row, role_records=[r for r in identity if r['role']==role]))
        if normalized not in TERMINAL:
            reason('ACTIVE_TASK')
            continue
        if not valid_time(row['finished']):
            raise ValueError('Invalid completion timestamp')
        finished.append(row['finished'])
        resolved = resolve(project, flow_id, role, database_reader=readonly_database)
        home = resolved['home']
        if not home.is_dir():
            cleanups = parent_data['cleanups'] if shared else local['cleanups']
            reason('PREVIOUSLY_PRUNED' if cleanups.get(row['id'])=='DELETED' else 'ARTIFACTS_MISSING')
            continue
        homes[role] = home.relative_to(flow).as_posix()
        submitted_data = object_json(safe(flow, role+'-submitted.json'))
        if any(submitted_data.get(key) != metadata.get(key) for key in ('task','batch','config')):
            raise ValueError('Submission metadata mismatch')
        if shared and submitted_data.get('queue_state') != str(base['state']):
            raise ValueError('Submission queue mismatch')
        backup_check = candidate(resolved['config'], resolved['row'], float('inf'))
        if not backup_check['eligible']:
            reason(backup_check['reason'])
        initial = object_json(safe(home,'baseline.json'))
        for path,digest in initial.items():
            safe(home/'project',path)
            if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
                raise ValueError('Invalid baseline digest')
        snapshot_root = flow/('original-input' if role=='developer' else 'review-input')
        if initial != workspace.files(snapshot_root):
            raise ValueError('Input snapshot differs from role baseline')
        after = workspace.files(home/'project')
        changes = json.loads(safe(home,'changes.json').read_text(encoding='utf-8'))
        expected = [dict(path=path,before=initial.get(path),after=after.get(path),
                         allowed=any(path==p or path.startswith(p.rstrip('/')+'/') for p in resolved['config']['writable']))
                    for path in sorted(initial.keys() | after.keys()) if initial.get(path)!=after.get(path)]
        if changes != expected or changes != resolved['result'].get('changes'):
            raise ValueError('Proposal differs from terminal change record')
        if normalized == 'DONE':
            required = TASK_FILES-{'provider-reply.txt', 'exit-code.txt'}
            if (any(not safe(home,name).is_file() for name in required)
                    or type(resolved['result'].get('exit_code')) is not int or resolved['result']['exit_code'] != 0):
                raise ValueError('Completed role evidence is incomplete')
        records[role] = dict(task=row['id'], state=normalized, answer=answer_for(resolved['result']), changes=changes)
        for backup in home.glob('backup-*'):
            if not backup.is_dir():
                raise ValueError('Invalid backup directory')
            journal = object_json(safe(backup,'journal.json'))
            if journal.get('state') != 'ROLLED_BACK':
                protected_backups.add(backup.relative_to(flow).as_posix())
            backup_files.add((backup/'journal.json').relative_to(flow).as_posix())
            journal_changes = journal.get('changes')
            if not isinstance(journal_changes,list) or not journal_changes:
                raise ValueError('Invalid recovery journal changes')
            for item in journal_changes:
                if not isinstance(item,dict) or not {'path','before','after'} <= item.keys():
                    raise ValueError('Invalid recovery journal change')
                if item.get('before') is not None:
                    backup_files.add(safe(backup,item['path']).relative_to(flow).as_posix())
                if protected_source(item['path']): reason('UNEXPECTED_EXCLUDED_DATA')
            if 'updated' in journal:
                if not valid_time(journal['updated']): raise ValueError('Invalid recovery timestamp')
                finished.append(journal['updated'])
    if any(r in result['reasons'] for r in ('ACTIVE_TASK','PREVIOUSLY_PRUNED','ARTIFACTS_MISSING')):
        return result
    if not homes:
        raise ValueError('No verifiable role artifacts')
    inspect_inventory = inventory if _inventory is None else _inventory
    files, directories = inspect_inventory(flow)
    known_top = {'result.json'} | {r+s for r in ('developer','reviewer') for s in ('-config.json','-submitted.json','.log')}
    checks = saved.get('checks',[])
    if not isinstance(checks,list): raise ValueError('Invalid test records')
    for check in checks:
        if (not isinstance(check,dict) or not isinstance(check.get('log'),str)
                or not re.fullmatch(r'test-[0-9]+\.log',check['log'])
                or type(check.get('exit_code')) is not int or not isinstance(check.get('argv'),list)
                or not check['argv'] or any(not isinstance(a,str) or not a for a in check['argv'])
                or not safe(flow,check['log']).is_file()):
            raise ValueError('Invalid or missing test log')
        known_top.add(check['log'])
    archive_entries = []
    def source_entry(item, source, path, *, payload=True):
        try:
            archive_path(path)
        except ValueError:
            reason('UNSUPPORTED_ARCHIVE_PATH')
            result['preserve_entries'].append(dict(item,reason='UNSUPPORTED_ARCHIVE_PATH'))
            return
        if payload and protected_source(path):
            reason('UNEXPECTED_EXCLUDED_DATA')
            result['preserve_entries'].append(dict(item, reason='UNEXPECTED_EXCLUDED_DATA'))
        elif payload and '__pycache__' in Path(path).parts:
            if not path.endswith('.pyc'):
                reason('UNEXPECTED_EXCLUDED_DATA')
                result['preserve_entries'].append(dict(item, reason='UNKNOWN_CACHE_FILE'))
            else:
                result['delete_entries'].append(dict(item, reason='DISCARD_CACHE'))
        else:
            archive_entries.append(dict(source=source,path=path,kind='file',sha256=item['sha256'],size=item['size'],mtime_ns=item['mtime_ns']))
            if item['path']=='result.json':
                result['replace_entries'].append(dict(item,reason='REPLACE_WITH_ARCHIVE_MARKER'))
            else:
                result['delete_entries'].append(dict(item,reason='ARCHIVE_FIRST'))
    for item in files:
        rel = item['path']
        if any(rel.startswith(prefix+'/') for prefix in protected_backups):
            result['preserve_entries'].append(dict(item,reason='RECOVERY_DATA_PROTECTED'))
            continue
        classified = False
        for index,role in enumerate(('developer','reviewer'),start=1):
            state_rel = states[index].relative_to(flow).as_posix()
            if rel.startswith(state_rel+'/'):
                tail = rel[len(state_rel)+1:]
                if tail in STATE_FILES:
                    result['preserve_entries'].append(dict(item,reason='QUEUE_LOCK_OR_HEALTH'))
                elif role in homes and rel.startswith(homes[role]+'/'):
                    local_path = rel[len(homes[role])+1:]
                    if local_path.startswith('project/'):
                        source_entry(item,role+'_workspace',local_path[len('project/'):])
                    elif local_path in TASK_FILES or rel in backup_files:
                        source_entry(item,'flow_metadata',rel,payload=False)
                    else:
                        reason('UNKNOWN_ARTIFACT')
                        result['preserve_entries'].append(dict(item,reason='UNKNOWN_ARTIFACT'))
                else:
                    reason('UNKNOWN_ARTIFACT')
                    result['preserve_entries'].append(dict(item,reason='UNKNOWN_ARTIFACT'))
                classified=True
                break
        if classified: continue
        if rel.startswith('original-input/'):
            source_entry(item,'original_input',rel[len('original-input/'):])
        elif rel.startswith('review-input/'):
            source_entry(item,'review_input',rel[len('review-input/'):])
        elif rel in known_top:
            source_entry(item,'flow_metadata',rel,payload=False)
        else:
            reason('UNKNOWN_ARTIFACT')
            result['preserve_entries'].append(dict(item,reason='UNKNOWN_ARTIFACT'))
    for item in directories:
        rel = item['path']
        if rel=='.': continue
        known = False
        for index,role in enumerate(('developer','reviewer'),start=1):
            state_rel = states[index].relative_to(flow).as_posix()
            if rel==state_rel or rel==state_rel+'/workspaces':
                known=True
                break
            if rel.startswith(state_rel+'/'):
                if role in homes:
                    home_rel = homes[role]
                    if rel==home_rel or rel==home_rel+'/project': known=True
                    elif rel.startswith(home_rel+'/project/'):
                        known=True
                        if protected_source(rel[len(home_rel+'/project/'):]): reason('UNEXPECTED_EXCLUDED_DATA')
                    elif any(p.startswith(rel+'/') for p in backup_files): known=True
                break
        else:
            if rel in ('original-input','review-input'): known=True
            elif rel.startswith(('original-input/','review-input/')):
                known=True
                if protected_source(rel.split('/',1)[1]): reason('UNEXPECTED_EXCLUDED_DATA')
        if not known:
            reason('UNKNOWN_ARTIFACT')
    latest = max([item['mtime_ns']/1_000_000_000 for item in files+directories
                  if not item['path'].endswith(('/runner.lock','/promotion.lock'))] + finished)
    result['latest_activity'] = latest
    if latest > now: reason('FUTURE_TIMESTAMP')
    elif latest >= now-days*86400: reason('RETENTION_PERIOD')
    projected_records = dict(schema=1,flow=flow_id,queue_mode=result['queue_mode'],original_phase=saved['state'],
                             roles=records,checks=checks,error=saved.get('error'),
                             original_changed_since_snapshot=saved.get('original_changed_since_snapshot'))
    validate_records(projected_records,flow_id,archive_entries)
    projected_manifest = dict(schema=1,flow=flow_id,entries=archive_entries,
        records_sha256=hashlib.sha256(canonical(projected_records)).hexdigest(),archive_digest='0'*64)
    unique = {entry['sha256']:entry['size'] for entry in archive_entries}
    metadata_bytes = len(canonical(projected_manifest)) + len(canonical(projected_records))
    marker_bytes = len(canonical(dict(id=flow_id,state='ARCHIVED',artifact_layout='FLOW_ARCHIVE_V1',
        original_phase=saved['state'],queue_mode=result['queue_mode'],archive_digest='0'*64,cleanup_operation='0'*32)))
    delete_bytes = sum(i['size'] for i in result['delete_entries'])
    replace_bytes = sum(i['size'] for i in result['replace_entries'])
    archive_bytes = sum(unique.values()) + metadata_bytes
    result.update(archive_entries=archive_entries,directories=directories,delete_bytes=delete_bytes,
                  archive_new_bytes=archive_bytes,archive_metadata_bytes=metadata_bytes,
                  temporary_archive_bytes=archive_bytes,marker_estimated_bytes=marker_bytes,
                  estimated_net_reclaim_bytes=delete_bytes+replace_bytes-archive_bytes-marker_bytes,
                  estimate_scope='Logical bytes; excludes cleanup journal and filesystem allocation overhead')
    if result['estimated_net_reclaim_bytes'] <= 0: reason('NO_SPACE_GAIN')
    binding = dict(schema=1,id=flow_id,days=days,project=str(project),parent_config=base_data,
                   files=files,directories=directories,roles=db_evidence,
                   databases=[dict(path=p.relative_to(project).as_posix(),digest=data['digest'])
                              for p,data in sorted(database_observations.items())],
                   delete=result['delete_entries'],preserve=result['preserve_entries'],replace=result['replace_entries'])
    result['plan_hash'] = hashlib.sha256(canonical(binding)).hexdigest()
    if _details is not None:
        _details.update(records=projected_records, binding=binding)
    if (inspect_inventory(flow)!=(files,directories) or object_json(flow/'result.json')!=saved
            or object_json(project/'bridge.json')!=base_data
            or any(database_rows(path)!=data for path,data in database_observations.items())
            or any(probe_lock(path)=='LOCKED' for path,_ in locks if path not in _held_locks)):
        reason('CHANGED_DURING_PREVIEW')
    result['eligible'] = not result['reasons']
    return result
