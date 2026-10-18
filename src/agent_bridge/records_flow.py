"""Explicit shared-flow ownership, lifetime fencing and resumable body removal."""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from . import records_body as body, records_guard as guard
from .storage import (FileLock, atomic_json, checked_local_path, identifier, path_identity,
                      probe_lock, guarded_removal, relocate_owned_directory, remove_empty_owned_directory)

SCHEMA = 1


def supported(config, *, live=False):
    value=guard.binding(config,live=live)
    return bool(value and value['version']==2)


def status(config, flow, *, live=False):
    if not supported(config,live=live):
        return None
    db=guard.snapshot(config['state'],live=live)
    try:
        row=db.execute('SELECT * FROM records_flows WHERE flow=?',(identifier(flow),)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def require(config, flow, *, write=False, allow_active=False):
    value=status(config,flow,live=allow_active)
    if value and value['body_state']!='NORMAL':
        raise guard.RecordsError('BODY_DELETED' if value['body_state']=='DELETED' else 'BODY_PURGE_PENDING')
    if write and supported(config,live=allow_active):
        if not value:
            raise guard.RecordsError('LEGACY_OWNERSHIP_UNCONFIRMED')
        if not allow_active and value['writer_state']!='CLOSED':
            raise guard.RecordsError('FLOW_WRITER_UNCONFIRMED')
        guard.check_start(config,live=allow_active)
    return value


def ensure_new(config, flow):
    guard.reject_roles(config)
    if status(config,flow):
        raise guard.RecordsError('FLOW_ID_ALREADY_RECORDED')


def reserve(config, flow):
    """Record a launch before any receipt or prompt is created."""
    if not supported(config,live=True):
        return None
    owner=uuid.uuid4().hex
    with FileLock(guard.gate(config['state'])),guard.connect(config['state'],actor='@admin') as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM records_flows WHERE flow=?',(identifier(flow),)).fetchone():
            raise guard.RecordsError('FLOW_ID_ALREADY_RECORDED')
        if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone():
            raise guard.RecordsError('BODY_PURGE_PENDING')
        db.execute("INSERT INTO records_flows VALUES (?,?,?,?,?,'ACCEPTED','NORMAL',NULL,?,NULL)",
                   (identifier(flow),str(config['root']),str(config['config_path']),owner,'SHARED_PARENT',time.time()))
    return owner


class Writer:
    def __init__(self,config,flow,owner):
        self.config=config; self.flow=identifier(flow); self.owner=owner; self.lock=None

    def check(self,db):
        if self.lock is None or body.identity(guard.database(self.config['state']))!=self.stamp or body.identity(self.lock.path)!=self.lock_stamp:
            raise guard.RecordsError('FLOW_WRITER_LOST')
        row=db.execute('SELECT * FROM records_flows WHERE flow=?',(self.flow,)).fetchone()
        if not row or row['owner']!=self.owner or row['writer_state']!='ACTIVE' or row['body_state']!='NORMAL':
            raise guard.RecordsError('FLOW_WRITER_REQUIRED')

    def __enter__(self):
        state=self.config['state']
        with FileLock(guard.gate(state)):
            self.lock=FileLock(checked_local_path(state/'flow-writers'/(self.flow+'.lock')))
            self.lock.__enter__()
            try:
                with guard.connect(state,actor='@admin') as db:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone():
                        raise guard.RecordsError('BODY_PURGE_PENDING')
                    changed=db.execute("UPDATE records_flows SET writer_state='ACTIVE' WHERE flow=? AND owner=? AND writer_state='ACCEPTED' AND body_state='NORMAL'",(self.flow,self.owner)).rowcount
                    if changed!=1:
                        raise guard.RecordsError('FLOW_WRITER_REQUIRED')
                self.stamp=body.identity(guard.database(state)); self.lock_stamp=body.identity(self.lock.path)
            except BaseException:
                self.lock.__exit__(None,None,None); self.lock=None; raise
        return self

    def __exit__(self,typ,value,tb):
        try:
            with FileLock(guard.gate(self.config['state'])),guard.connect(self.config['state'],actor='@admin') as db:
                db.execute('BEGIN IMMEDIATE'); self.check(db)
                db.execute('UPDATE records_flows SET writer_state=?,closed=? WHERE flow=?',('LOST' if typ else 'CLOSED',time.time(),self.flow))
        finally:
            self.lock.__exit__(typ,value,tb); self.lock=None


@contextlib.contextmanager
def scope(config,flow,existing=None):
    if not supported(config,live=True):
        yield None; return
    if existing is not None:
        with guard.connect(config['state']) as db:
            existing.check(db)
        if existing.flow!=flow:
            raise guard.RecordsError('FLOW_WRITER_REQUIRED')
        yield existing; return
    owner=reserve(config,flow)
    with Writer(config,flow,owner) as writer:
        yield writer


def prepare_report(config,known=None,*,held=False):
    values=body.external_blockers(config,known)+(body.lock_blockers(config) if not held else [])
    db=guard.snapshot(config['state'])
    try:
        meta=guard.validate(db) if db is not None else None
        if not meta:
            values.append('RECORDS_PREPARATION_REQUIRED')
        elif meta['version']==2:
            values.append('ALREADY_PREPARED')
        else:
            values+=guard.blockers(db,config['state'])
            if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone(): values.append('BODY_PURGE_PENDING')
            if db.execute('SELECT 1 FROM role_tasks').fetchone(): values.append('ROLE_QUEUE_UPGRADE_REQUIRED')
            if db.execute("SELECT 1 FROM tasks WHERE finished IS NULL").fetchone(): values.append('TASK_NOT_TERMINAL')
        snapshot=hashlib.sha256(db.serialize()).hexdigest() if db else None
    finally:
        if db: db.close()
    return dict(kind='roles_prepare',eligible=not values,blockers=sorted(set(values)),schema=2,
                roles_supported=True,legacy_preserved=True,private_purge_supported=False,
                revision=body.fingerprint([config['_source_sha256'],body.identity(guard.database(config['state'])),snapshot,values]))


def external_blockers(config,known=None):
    values=body.external_blockers(config,known,roles=True)
    state=config['state']
    if any(state.glob('archive-staging-*')):
        values.append('ARCHIVE_STAGING_UNCONFIRMED')
    return values


def select(config,flow,known=None,*,held=False):
    flow=identifier(flow); values=external_blockers(config,known)
    if not held:
        values+=body.lock_blockers(config)
        for path in (config['state']/'flow-locks'/(flow+'.lock'),config['state']/'p1-jobs'/'start.lock'):
            if probe_lock(checked_local_path(path))=='LOCKED': values.append('FLOW_WRITER_ACTIVE')
    value=status(config,flow)
    if not value:
        return dict(kind='flow_purge',flow=flow,eligible=False,blockers=['LEGACY_OWNERSHIP_UNCONFIRMED']),None
    if value['body_state']!='NORMAL': values.append('BODY_DELETED' if value['body_state']=='DELETED' else 'BODY_PURGE_PENDING')
    db=guard.snapshot(config['state']); rows=[]; snapshots=[]
    try:
        values+=guard.blockers(db,config['state'])
        if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone(): values.append('BODY_PURGE_PENDING')
        for row in db.execute('SELECT t.*,r.role,r.data AS role_data FROM tasks t JOIN role_tasks r ON t.id=r.id WHERE r.flow=? ORDER BY r.role',(flow,)):
            row=dict(row); rows.append(row)
            own=db.execute('SELECT * FROM records_tasks WHERE task=?',(row['id'],)).fetchone()
            writer=db.execute('SELECT state FROM records_writers WHERE owner=?',(own['writer'],)).fetchone() if own else None
            if not own or own['project']!=value['project'] or own['config']!=value['config'] or own['body_state']!='NORMAL': values.append('ROLE_SCOPE_UNKNOWN')
            if row['state'] not in body.records.TERMINAL or row['finished'] is None: values.append('TASK_NOT_TERMINAL')
            if (not writer or writer[0]!='CLOSED') and not (own and own['writer'] is None and row['started'] is None and row['state']=='CANCELLED'):
                values.append('RECORDS_WRITER_UNCONFIRMED')
            snapshots.append(dict(own) if own else None)
            for table in ('context_plans','artifact_cleanup','usage_snapshots'):
                snapshots.append([table,[dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE id=?',(row['id'],))]])
        ids={row['id'] for row in rows}
        for row in db.execute('SELECT id,data FROM context_plans'):
            if row['id'] not in ids:
                data=body.records.object_json(row['data'])
                if any(s.get('record',{}).get('provenance',{}).get('source_task_id') in ids for s in data.get('payload',{}).get('summaries',[])):
                    values.append('RELATED_TASKS_REQUIRED')
    finally:
        db.close()
    home=checked_local_path(config['root']/'.role-flows'/flow,config['root'])
    artifact=body.scan(home); phase=None
    metadata={'result.json','developer-config.json','reviewer-config.json','developer-submitted.json','reviewer-submitted.json','cleanup.json','archive/records.json','archive/manifest.json'}
    if any(e['kind']=='file' and e['path'] in metadata and e['bytes']>body.records.MAX_JSON_BYTES for e in artifact['entries']):
        raise guard.RecordsError('METADATA_LIMIT')
    if artifact['identity'] is None:
        values.append('FLOW_METADATA_MISSING')
    else:
        if any(Path(e['path']).name in ('queue.sqlite3','queue.sqlite3-wal','queue.sqlite3-journal') for e in artifact['entries']): values.append('PRIVATE_QUEUE_UNCONFIRMED')
        from .p1 import helper
        saved=body.records.object_json((home/'result.json').read_text(encoding='utf-8-sig'))
        phase=saved.get('state')
        if saved.get('id')!=flow or saved.get('queue_mode')!='SHARED_PARENT' or saved.get('state') not in helper('role_manage').FINISHED_PHASES:
            values.append('FLOW_NOT_TERMINAL')
        if {r['role'] for r in rows}!={r for r in ('developer','reviewer') if saved.get(r) is not None}:
            values.append('ROLE_SCOPE_UNKNOWN')
        for row in rows:
            resolved=helper('role_review').resolve(config['root'],flow,row['role'],database_reader=guard.readonly_database,allow_unstarted=True)
            if resolved['task']!=row['id'] or not resolved['shared']: values.append('ROLE_SCOPE_UNKNOWN')
        if (home/'archive').exists() or (home/'cleanup.json').exists():
            try: helper('flow_archive').load_snapshot(config['root'],flow)
            except (OSError,ValueError,KeyError,TypeError): values.append('ARCHIVE_INCOMPLETE')
    jobs=[]; receipt=None
    for suffix in ('.json','.txt','.log'):
        path=checked_local_path(config['state']/'p1-jobs'/(flow+suffix),config['state'])
        if path.exists():
            info=path.stat()
            if info.st_size>body.MAX_BYTES: raise guard.RecordsError('ARTIFACT_SIZE_LIMIT')
            if suffix=='.json' and info.st_size>body.records.MAX_JSON_BYTES: raise guard.RecordsError('METADATA_LIMIT')
            with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
            jobs.append(dict(path=path.name,identity=body.identity(path),sha256=digest,bytes=info.st_size))
            if suffix=='.json': receipt=body.records.object_json(path.read_text(encoding='utf-8-sig'))
    if jobs and (not receipt or receipt.get('flow')!=flow or receipt.get('config')!=str(config['config_path']) or receipt.get('state') not in ('FINISHED','FAILED') or not body.records.finite(receipt.get('finished'))):
        values.append('ROLE_JOB_UNCONFIRMED')
    plan=dict(flow=flow,original_phase=phase,artifact=artifact,jobs=jobs,task_ids=sorted(r['id'] for r in rows))
    if len(json.dumps(plan).encode())>4*1024*1024 or artifact['bytes']+sum(j['bytes'] for j in jobs)>body.MAX_BYTES:
        values.append('INVENTORY_LIMIT')
    if guard.database(config['state']).stat().st_size+len(json.dumps(plan).encode())*2+2*1024*1024>64*1024*1024:
        values.append('RECEIPT_SIZE_LIMIT')
    report=dict(kind='flow_purge',flow=flow,eligible=not values,blockers=sorted(set(values)),tasks=len(rows),task_ids=plan['task_ids'],
                files=sum(e['kind']=='file' for e in artifact['entries'])+len(jobs),bytes=artifact['bytes']+sum(j['bytes'] for j in jobs),
                preserves=['flow_role_task_ids','usage','control','originals','other_projects','provider_history'],
                revision=body.fingerprint([config['_source_sha256'],value,rows,snapshots,plan,values]))
    return report,plan


@contextlib.contextmanager
def locks(config,flow=None):
    from .p1 import helper
    with FileLock(guard.gate(config['state'])),contextlib.ExitStack() as stack:
        if flow:
            stack.enter_context(FileLock(checked_local_path(config['state']/'p1-jobs'/'start.lock')))
            stack.enter_context(FileLock(helper('flow_lifecycle').lock_path(config['root'],config,flow)))
        for name in ('runner-management.lock','runner.lock','promotion.lock'):
            stack.enter_context(FileLock(checked_local_path(config['state']/name)))
        yield


def marker(data,complete=False):
    return dict(id=data['flow'],state='BODY_DELETED' if complete else 'BODY_PURGING',body_state='DELETED' if complete else 'PURGING',operation=data['operation'],queue_mode='SHARED_PARENT',original_phase=data['original_phase'])


def remaining(config,data):
    home=checked_local_path(config['root']/'.role-flows'/identifier(data['flow']),config['root'])
    quarantine=checked_local_path(config['state']/'record-bodies'/identifier(data['operation'])/'flow',config['state'])
    expected=data['artifact']; target=home
    if home.exists() and body.identity(home)!=expected['identity']:
        if body.identity(home)!=data.get('marker_identity') or sorted(p.name for p in home.iterdir())!=['result.json'] or body.records.object_json((home/'result.json').read_text(encoding='utf-8')) not in (marker(data),marker(data,True)):
            raise guard.RecordsError('ARTIFACT_RECREATED')
        target=quarantine
    elif quarantine.exists():
        if home.exists(): raise guard.RecordsError('ARTIFACT_RECREATED')
        target=quarantine
    actual=body.scan(target)
    if actual['identity'] is not None and actual['identity']!=expected['identity']:
        raise guard.RecordsError('PLAN_CHANGED')
    entries={e['path']:e for e in expected['entries']}
    for entry in actual['entries']:
        old=entries.get(entry['path'])
        if not old or any(entry.get(k)!=old.get(k) for k in ('identity','kind','sha256')):
            raise guard.RecordsError('PLAN_CHANGED')
    if target==home and actual['identity'] is not None and actual!=expected:
        raise guard.RecordsError('PLAN_CHANGED')
    for item in data['jobs']:
        path=checked_local_path(config['state']/'p1-jobs'/item['path'],config['state'])
        if path.exists() and (body.identity(path)!=item['identity'] or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']):
            raise guard.RecordsError('PLAN_CHANGED')
    return home,quarantine,actual


def resume_report(config,operation,known=None,*,held=False):
    data=body.load_operation(config,operation); values=external_blockers(config,known)
    if data['state']=='DONE': values.append('OPERATION_ALREADY_COMPLETE')
    if data['config_sha256']!=config['_source_sha256'] or data['database_identity']!=body.identity(guard.database(config['state'])):
        values.append('OPERATION_IDENTITY_CHANGED')
    if not held: values+=body.lock_blockers(config)
    db=guard.snapshot(config['state'])
    try:
        values+=guard.blockers(db,config['state'])
        row=db.execute('SELECT operation FROM records_flows WHERE flow=?',(data['flow'],)).fetchone()
        if not row or row[0]!=operation: values.append('OPERATION_IDENTITY_CHANGED')
    finally: db.close()
    _,_,actual=remaining(config,data)
    return dict(kind='flow_resume',flow=data['flow'],operation=operation,eligible=not values,blockers=sorted(set(values)),tasks=len(data['task_ids']),task_ids=data['task_ids'],revision=body.fingerprint([data,actual,values])),data


def public(data):
    return dict(operation=data['operation'],kind=data['kind'],flow=data.get('flow'),original_phase=data.get('original_phase'),state=data['state'],ok=data['state']=='DONE',
                task_ids=data.get('task_ids',[]),body_deleted=data['kind']=='flow_purge' and data['state']=='DONE',
                items=[dict(task=task,state='DELETED' if data['state']=='DONE' else 'PARTIAL') for task in data.get('task_ids',[])])


def remove_files(config,data):
    data['state']='PURGING'; body.persist(config,data)
    try:
        home,quarantine,actual=remaining(config,data)
        tombstone=checked_local_path(config['state']/'record-bodies'/data['operation']/'marker',config['state'])
        if not data.get('marker_identity'):
            tombstone.mkdir(parents=True,exist_ok=True)
            if list(tombstone.iterdir()):
                if sorted(p.name for p in tombstone.iterdir())!=['result.json'] or body.records.object_json((tombstone/'result.json').read_text(encoding='utf-8'))!=marker(data):
                    raise guard.RecordsError('ARTIFACT_RECREATED')
            else:
                atomic_json(tombstone/'result.json',marker(data))
            data['marker_identity']=body.identity(tombstone); body.persist(config,data)
        if body.identity(home)==data['artifact']['identity']:
            relocate_owned_directory(home,quarantine,config['root'],data['artifact']['identity'])
        if not home.exists():
            relocate_owned_directory(tombstone,home,config['root'],data['marker_identity'])
        _,_,actual=remaining(config,data)
        if actual['identity'] is not None:
            with guarded_removal(quarantine,[(quarantine/e['path'],e['sha256']) for e in actual['entries'] if e['kind']=='file']) as remove:
                for _ in remove(): pass
            for entry in sorted((e for e in actual['entries'] if e['kind']=='directory'),key=lambda e:len(Path(e['path']).parts),reverse=True):
                remove_empty_owned_directory(quarantine/entry['path'],config['state'],entry['identity'])
        jobs=config['state']/'p1-jobs'
        with guarded_removal(jobs,[(jobs/j['path'],j['sha256']) for j in data['jobs'] if (jobs/j['path']).exists()]) as remove:
            for _ in remove(): pass
        if quarantine.exists() or any((jobs/j['path']).exists() for j in data['jobs']): raise guard.RecordsError('DELETE_UNCONFIRMED')
        atomic_json(home/'result.json',marker(data,True))
        with guard.connect(config['state'],actor='@admin') as db:
            db.execute('BEGIN IMMEDIATE')
            for task in data['task_ids']:
                deleted=dict(body_state='DELETED',body_deleted=True,operation=data['operation'])
                db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps(deleted),task))
                db.execute('UPDATE context_plans SET data=? WHERE id=?',(json.dumps(deleted),task))
                db.execute("UPDATE records_tasks SET body_state='DELETED' WHERE task=?",(task,))
                db.execute("INSERT INTO artifact_cleanup VALUES (?,'DELETED',?) ON CONFLICT(id) DO UPDATE SET state='DELETED',updated=excluded.updated",(task,time.time()))
            db.execute("UPDATE records_flows SET body_state='DELETED' WHERE flow=?",(data['flow'],))
            completed=dict(data,state='DONE')
            db.execute("UPDATE records_operations SET state='DONE',data=? WHERE operation=?",(json.dumps(completed),data['operation']))
        data=completed
    except (OSError,ValueError,RuntimeError,sqlite3.Error):
        data['state']='PARTIAL'
        try: body.persist(config,data)
        except (OSError,ValueError,RuntimeError,sqlite3.Error): pass
    return public(data)


def apply(payload,operation):
    config=body.config_for(payload['project'],payload['config'])
    if config['_source_sha256']!=payload['config_sha256'] or body.identity(guard.database(config['state']))!=payload['identity']:
        raise guard.RecordsError('PLAN_CHANGED')
    with locks(config,payload.get('flow')):
        if payload['kind']=='roles_prepare':
            with guard.connect(config['state'],actor='@admin') as db:
                db.execute('BEGIN IMMEDIATE')
                report=prepare_report(config,payload['known_configs'],held=True)
                if not report['eligible'] or report['revision']!=payload['report']['revision']: raise guard.RecordsError('PLAN_CHANGED')
                for sql in guard.triggers(): db.execute('DROP TRIGGER '+sql.split()[2])
                db.execute('CREATE TABLE records_flows(flow TEXT PRIMARY KEY,project TEXT NOT NULL,config TEXT NOT NULL,owner TEXT NOT NULL,queue_mode TEXT NOT NULL,writer_state TEXT NOT NULL,body_state TEXT NOT NULL,operation TEXT,created REAL NOT NULL,closed REAL)')
                db.execute('UPDATE records_meta SET version=2 WHERE id=1')
                for sql in guard.triggers(2): db.execute(sql)
                data=dict(kind='roles_prepare',operation=operation,state='DONE',database_identity=body.identity(guard.database(config['state'])))
                db.execute("INSERT INTO records_operations VALUES (?,'DONE',?)",(operation,json.dumps(data)))
            return public(data)
        if payload['kind']=='flow_resume':
            report,data=resume_report(config,payload['operation'],payload['known_configs'],held=True)
            if not report['eligible'] or report['revision']!=payload['report']['revision']: raise guard.RecordsError('PLAN_CHANGED')
            return remove_files(config,data)
        with guard.connect(config['state'],actor='@admin') as db:
            db.execute('BEGIN IMMEDIATE')
            report,plan=select(config,payload['flow'],payload['known_configs'],held=True)
            if not report['eligible'] or report['revision']!=payload['report']['revision']: raise guard.RecordsError('PLAN_CHANGED')
            data=dict(plan,kind='flow_purge',operation=operation,state='PURGING',config_sha256=config['_source_sha256'],database_identity=body.identity(guard.database(config['state'])))
            for task in data['task_ids']:
                scrub=dict(body_state='PURGING',body_deleted=False,operation=operation)
                db.execute('UPDATE tasks SET prompt=?,result=? WHERE id=?',('',json.dumps(scrub),task))
                db.execute('UPDATE context_plans SET data=? WHERE id=?',(json.dumps(scrub),task))
                db.execute('UPDATE role_tasks SET data=? WHERE id=?',(json.dumps(scrub),task))
                db.execute("UPDATE records_tasks SET body_state='PURGING',operation=? WHERE task=?",(operation,task))
            db.execute("UPDATE records_flows SET body_state='PURGING',operation=? WHERE flow=?",(operation,data['flow']))
            db.execute("INSERT INTO records_operations VALUES (?,'PURGING',?)",(operation,json.dumps(data)))
        return remove_files(config,data)
