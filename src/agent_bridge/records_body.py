"""Explicit ordinary-task body removal with durable partial results and no replay."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time

from . import records, records_guard as guard, runner_manager
from .storage import (FileLock, Store, checked_local_path, identifier, path_identity, probe_lock,
                      readonly_database, guarded_removal, relocate_owned_directory, remove_empty_owned_directory)

SCHEMA = 1
ACTIONS = {
    'records.prepare.preview': ({'project','config'}, {'known_configs'}),
    'records.roles.prepare.preview': ({'project','config'}, {'known_configs'}),
    'records.flow.purge.preview': ({'project','config','flow_id'}, {'known_configs'}),
    'records.purge.preview': ({'project','config','task_ids'}, {'known_configs'}),
    'records.resume.preview': ({'project','config','operation'}, {'known_configs'}),
    'records.apply': ({'plan_id','apply'}, set()),
    'records.cancel': ({'plan_id'}, set()),
    'records.operation': ({'project','database','operation'}, set()),
}
MAX_FILES=20000
MAX_BYTES=256*1024*1024


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def identity(path):
    path=checked_local_path(path)
    if not path.exists():
        return None
    info=path.stat(); return [info.st_dev,info.st_ino]


def config_for(project, path):
    config=runner_manager.checked_config(path)
    if path_identity(config['root'])!=path_identity(checked_local_path(project)):
        raise guard.RecordsError('RECORDS_PROJECT_MISMATCH')
    guard.check_input_scope(config)
    return config


def external_blockers(config, known=None, *, roles=False):
    values=[]
    if os.name!='nt':
        values.append('RECORDS_PLATFORM_UNSUPPORTED')
    root,state=config['root'],config['state']
    if not isinstance(known or [],list) or len(known or [])>16:
        raise ValueError('Select at most 16 known configurations')
    for candidate in known or []:
        if not isinstance(candidate,str) or not Path(candidate).is_absolute():
            raise ValueError('Expected absolute known configuration')
        if path_identity(candidate)==path_identity(config['config_path']):
            continue
        try:
            if not checked_local_path(candidate).exists():
                continue
            other=runner_manager.checked_config(candidate)
            a,b=Path(path_identity(state)),Path(path_identity(other['state']))
            if a==b or a.is_relative_to(b) or b.is_relative_to(a):
                values.append('SHARED_STATE_UNCONFIRMED')
        except (OSError,ValueError,KeyError,TypeError):
            values.append('KNOWN_CONFIG_UNKNOWN')
    from . import records_flow
    queue=guard.database(state)
    role_support=roles or (queue.exists() and queue.stat().st_size>=100 and records_flow.supported(config))
    for path in (root/'.role-flows',state/'p1-jobs'):
        path=checked_local_path(path)
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            if not role_support:
                values.append('ROLE_QUEUE_UPGRADE_REQUIRED')
            elif path.is_dir():
                db=guard.snapshot(state)
                try:
                    owned={r[0] for r in db.execute('SELECT flow FROM records_flows')} if db is not None and guard.meta(db) and guard.meta(db)['version']==2 else set()
                    for entry in path.iterdir():
                        checked_local_path(entry,path)
                        if path.name=='p1-jobs' and entry.name=='start.lock': continue
                        name=entry.name if path.name=='.role-flows' else entry.stem
                        if name not in owned or (path.name=='p1-jobs' and entry.suffix not in ('.json','.txt','.log')):
                            values.append('ROLE_SCOPE_UNKNOWN')
                finally:
                    if db is not None: db.close()
    if checked_local_path(state/'service.json').exists():
        values.append('SERVICE_REGISTRATION_PRESENT')
    if checked_local_path(state/'runner-launch.json').exists():
        observed=runner_manager.inspect(config)
        if any(code in observed['problems'] for code in ('LAUNCH_UNCONFIRMED','LAUNCH_UNKNOWN','DATABASE_UNKNOWN')):
            values.append('RUNNER_UNCONFIRMED')
    return sorted(set(values))


def lock_blockers(config):
    values=[]
    for name in ('runner-management.lock','runner.lock','promotion.lock'):
        if probe_lock(checked_local_path(config['state']/name))=='LOCKED':
            values.append('RUNNER_OR_MANAGEMENT_ACTIVE')
    return values


def prepare_report(config, known=None, *, held=False, connection=None, original_identity=None):
    values=external_blockers(config,known)+(lock_blockers(config) if not held else [])
    path=guard.database(config['state']); schema=0; image_hash=None
    db=connection if connection is not None else guard.snapshot(config['state'])
    try:
        if db is not None:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                image_hash=hashlib.sha256(db.serialize()).hexdigest()
            existing=guard.validate(db)
            if existing:
                schema=1; values.append('ALREADY_PREPARED')
            else:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'tasks' in tables:
                    columns={r[1] for r in db.execute('PRAGMA table_info(tasks)')}
                    if not {'id','batch','endpoint','prompt','state','created','started','finished','result'}<=columns:
                        values.append('BATCH_SCHEMA_UNKNOWN')
                    elif db.execute("SELECT 1 FROM tasks WHERE state NOT IN ('DONE','ERROR','TIMEOUT','CANCELLED','INTERRUPTED') OR finished IS NULL LIMIT 1").fetchone():
                        values.append('TASK_NOT_TERMINAL')
                if 'role_tasks' in tables and db.execute('SELECT 1 FROM role_tasks LIMIT 1').fetchone():
                    values.append('ROLE_QUEUE_UPGRADE_REQUIRED')
                if 'runner_lifecycle' in tables:
                    row=db.execute('SELECT data FROM runner_lifecycle WHERE id=1').fetchone()
                    if row and json.loads(row[0]).get('mode')!='STOPPED':
                        values.append('RUNNER_UNCONFIRMED')
                if 'interactive_sessions' in tables and db.execute('SELECT 1 FROM interactive_sessions WHERE finished IS NULL LIMIT 1').fetchone():
                    values.append('INTERACTIVE_UNFINISHED')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('tasks','control','role_tasks','context_plans','usage_snapshots','artifact_cleanup','runner_lifecycle')").fetchone():
                    values.append('LEGACY_TRIGGER_UNKNOWN')
    finally:
        if db is not None and connection is None:
            db.close()
    return dict(kind='prepare',schema=schema,eligible=not values,blockers=sorted(set(values)),
                legacy_preserved=True,legacy_purge_supported=False,roles_supported=False,
                revision=fingerprint([config['_source_sha256'],original_identity if connection is not None else identity(path),image_hash,values]))


def scan(root, *, resume=False):
    root=checked_local_path(root)
    if not root.exists():
        return dict(identity=None,entries=[],bytes=0)
    if not root.is_dir():
        raise guard.RecordsError('ARTIFACT_SCOPE_UNKNOWN')
    found=[]; pending=[root]; size=0; device=root.stat().st_dev
    while pending:
        selected=pending.pop()
        path=checked_local_path(selected,None if selected==root else root); info=path.stat()
        rel=path.relative_to(root).as_posix()
        if info.st_dev!=device or any(p in records.EXCLUDED or p.startswith('backup-') for p in path.relative_to(root).parts):
            raise guard.RecordsError('RECOVERY_OR_EXCLUDED_DATA')
        entry=dict(path=rel,identity=[info.st_dev,info.st_ino],kind='directory' if stat.S_ISDIR(info.st_mode) else 'file',mtime=info.st_mtime_ns)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as children:
                for child in children:
                    pending.append(Path(child.path))
                    if len(found)+len(pending)>MAX_FILES:
                        raise guard.RecordsError('INVENTORY_LIMIT')
        elif stat.S_ISREG(info.st_mode):
            size+=info.st_size
            if size>MAX_BYTES:
                raise guard.RecordsError('ARTIFACT_SIZE_LIMIT')
            with path.open('rb') as stream:
                entry.update(bytes=info.st_size,sha256=hashlib.file_digest(stream,'sha256').hexdigest())
        else:
            raise guard.RecordsError('ARTIFACT_SCOPE_UNKNOWN')
        after=path.stat()
        if (info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            raise guard.RecordsError('PLAN_CHANGED')
        found.append(entry)
        if len(found)>MAX_FILES:
            raise guard.RecordsError('INVENTORY_LIMIT')
    return dict(identity=identity(root),entries=sorted(found,key=lambda v:v['path']),bytes=size)


def select(config, ids, known=None, *, held=False):
    values=external_blockers(config,known)+(lock_blockers(config) if not held else [])
    meta=guard.binding(config)
    if not meta:
        return dict(kind='purge',eligible=False,blockers=['RECORDS_PREPARATION_REQUIRED']),[]
    db=guard.snapshot(config['state'])
    snapshots=[]; plans=[]; required=set()
    try:
        guard.validate(db); values+=guard.blockers(db,config['state'])
        if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone():
            values.append('BODY_PURGE_PENDING')
        if meta['version']==1 and db.execute('SELECT 1 FROM role_tasks LIMIT 1').fetchone():
            values.append('ROLE_QUEUE_UPGRADE_REQUIRED')
        for task in ids:
            if db.execute('SELECT 1 FROM role_tasks WHERE id=?',(task,)).fetchone():
                values.append('WHOLE_FLOW_REQUIRED')
            row=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            own=db.execute('SELECT * FROM records_tasks WHERE task=?',(task,)).fetchone()
            if row is None:
                raise guard.RecordsError('RECORD_MISSING')
            row=dict(row); snapshots.append(row)
            if not own:
                values.append('LEGACY_OWNERSHIP_UNCONFIRMED'); continue
            own=dict(own); snapshots.append(own)
            if own['project']!=meta['project'] or own['config']!=meta['config']:
                values.append('RECORDS_PROJECT_MISMATCH')
            if own['body_state']!='NORMAL':
                values.append('BODY_DELETED' if own['body_state']=='DELETED' else 'BODY_PURGE_PENDING')
            writer=db.execute('SELECT state FROM records_writers WHERE owner=?',(own['writer'],)).fetchone()
            if not writer or writer[0]!='CLOSED':
                values.append('RECORDS_WRITER_UNCONFIRMED')
            if row['state'] not in records.TERMINAL or row['finished'] is None:
                values.append('TASK_NOT_TERMINAL')
            result=json.loads(row['result'] or '{}')
            home=checked_local_path(config['state']/'workspaces'/task,config['state'])
            if not isinstance(result,dict) or result.get('workspace') is not None and path_identity(result['workspace'])!=path_identity(home):
                values.append('WORKSPACE_SCOPE_UNKNOWN')
            artifact=scan(home)
            for table in ('context_plans','usage_snapshots','artifact_cleanup'):
                snapshots.append([table,[dict(v) for v in db.execute('SELECT * FROM '+table+' WHERE id=?',(task,))]])
            plans.append(dict(task=task,artifact=artifact,exit_code=result.get('exit_code') if type(result.get('exit_code')) is int else None))
            if sum(len(p['artifact']['entries']) for p in plans)>MAX_FILES or sum(p['artifact']['bytes'] for p in plans)>MAX_BYTES:
                raise guard.RecordsError('INVENTORY_LIMIT')
        for row in db.execute('SELECT id,data FROM context_plans'):
            if row['id'] in ids:
                continue
            try:
                data=records.object_json(row['data'])
                for summary in data.get('payload',{}).get('summaries',[]):
                    if summary.get('record',{}).get('provenance',{}).get('source_task_id') in ids:
                        required.add(identifier(row['id']))
            except (ValueError,TypeError,AttributeError):
                values.append('CONTEXT_SCOPE_UNKNOWN')
        if required:
            values.append('RELATED_TASKS_REQUIRED')
        snapshots.append([dict(r) for r in db.execute('SELECT * FROM records_writers ORDER BY owner')])
        receipt_bytes=len(json.dumps(plans).encode())
        if receipt_bytes>4*1024*1024 or guard.database(config['state']).stat().st_size+receipt_bytes*2+2*1024*1024>64*1024*1024:
            values.append('RECEIPT_SIZE_LIMIT')
    finally:
        db.close()
    report=dict(kind='purge',schema=1,eligible=not values,blockers=sorted(set(values)),task_ids=ids,required_ids=sorted(required),
                tasks=len(ids),files=sum(sum(e['kind']=='file' for e in p['artifact']['entries']) for p in plans),bytes=sum(p['artifact']['bytes'] for p in plans),
                preserves=['task_ids','state_and_timestamps','usage','control','interactive_records','unselected_tasks','originals','provider_history'],
                revision=fingerprint([config['_source_sha256'],meta,snapshots,plans,values]))
    return report,plans


@contextlib.contextmanager
def locks(config):
    state=config['state']
    with FileLock(guard.gate(state)),FileLock(checked_local_path(state/'runner-management.lock')),FileLock(checked_local_path(state/'runner.lock')),FileLock(checked_local_path(state/'promotion.lock')):
        yield


def public(data):
    if data['kind']=='connection_remove':
        from .connection_remove import public as connection_public
        return connection_public(data)
    if data['kind'] in ('flow_purge','roles_prepare'):
        from .records_flow import public as flow_public
        return flow_public(data)
    return dict(operation=data['operation'],kind=data['kind'],state=data['state'],ok=data['state']=='DONE',
                task_ids=[p['task'] for p in data.get('items',[])],
                items=[dict(task=p['task'],state=p['status'],files=sum(e['kind']=='file' for e in p['artifact']['entries'])) for p in data.get('items',[])],
                body_deleted=data['kind']=='purge' and data['state']=='DONE',legacy_preserved=data['kind']=='prepare')


def operation(project, database, operation):
    identifier(operation)
    path=checked_local_path(database)
    if path.name!='queue.sqlite3':
        raise ValueError('Expected a queue')
    if not path.exists():
        return dict(found=False,operation=operation)
    db=guard.snapshot(path.parent)
    try:
        meta=guard.validate(db)
        if not meta or path_identity(meta['project'])!=path_identity(checked_local_path(project)):
            raise guard.RecordsError('RECORDS_PROJECT_MISMATCH')
        row=db.execute('SELECT data FROM records_operations WHERE operation=?',(operation,)).fetchone()
        if not row:
            return dict(found=False,operation=operation)
        data=json.loads(row[0])
        if data.get('database_identity')!=identity(path):
            raise guard.RecordsError('OPERATION_IDENTITY_CHANGED')
        report=public(data); report['complete']=report.pop('ok')
        return dict(found=True,**report)
    finally:
        db.close()


def load_operation(config, operation_id):
    guard.binding(config)
    db=guard.snapshot(config['state'])
    try:
        row=db.execute('SELECT data FROM records_operations WHERE operation=?',(identifier(operation_id),)).fetchone()
        if not row:
            raise guard.RecordsError('OPERATION_MISSING')
        return json.loads(row[0])
    finally:
        db.close()


def persist(config,data):
    with guard.connect(config['state'],actor='@admin') as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE records_operations SET state=?,data=? WHERE operation=?',(data['state'],json.dumps(data),data['operation']))


def remaining(config,data,item):
    state=config['state']; home=checked_local_path(state/'workspaces'/identifier(item['task']),state)
    quarantine=checked_local_path(state/'record-bodies'/identifier(data['operation'])/item['task'],state)
    if item['status']=='DELETED':
        if home.exists() or quarantine.exists():
            raise guard.RecordsError('ARTIFACT_RECREATED')
        return home,quarantine,None
    if home.exists() and quarantine.exists():
        raise guard.RecordsError('ARTIFACT_RECREATED')
    target=quarantine if quarantine.exists() else home
    actual=scan(target,resume=True)
    expected=item['artifact']
    if actual['identity'] is not None and actual['identity']!=expected['identity']:
        raise guard.RecordsError('PLAN_CHANGED')
    original={e['path']:e for e in expected['entries']}
    for entry in actual['entries']:
        old=original.get(entry['path'])
        if not old or entry['identity']!=old['identity'] or entry['kind']!=old['kind'] or entry.get('sha256')!=old.get('sha256'):
            raise guard.RecordsError('PLAN_CHANGED')
    if target==home and actual['identity'] is not None and actual!=expected:
        raise guard.RecordsError('PLAN_CHANGED')
    return home,quarantine,actual


def resume_report(config, operation_id, known=None, *, held=False):
    data=load_operation(config,operation_id)
    if data['kind']=='connection_remove': raise guard.RecordsError('OPERATION_NAMESPACE_MISMATCH')
    if data['kind']=='flow_purge':
        from .records_flow import resume_report as flow_resume
        return flow_resume(config,operation_id,known,held=held)
    values=external_blockers(config,known)+(lock_blockers(config) if not held else [])
    if data['kind']!='purge' or data['state']=='DONE':
        return dict(kind='resume',operation=operation_id,eligible=False,blockers=['OPERATION_ALREADY_COMPLETE'],tasks=len(data.get('items',[])),revision=fingerprint(data)),data
    if data['config_sha256']!=config['_source_sha256']:
        values.append('PLAN_CHANGED')
    if data.get('database_identity')!=identity(guard.database(config['state'])):
        values.append('OPERATION_IDENTITY_CHANGED')
    db=guard.snapshot(config['state'])
    try:
        values+=guard.blockers(db,config['state'])
        for item in data.get('items',[]):
            row=db.execute('SELECT operation FROM records_tasks WHERE task=?',(item['task'],)).fetchone()
            if not row or row[0]!=operation_id:
                values.append('OPERATION_IDENTITY_CHANGED')
    finally:
        db.close()
    seen=[]
    for item in data.get('items',[]):
        _,_,actual=remaining(config,data,item); seen.append(actual)
    return dict(kind='resume',operation=operation_id,eligible=not values,blockers=sorted(set(values)),tasks=len(data.get('items',[])),
                revision=fingerprint([data,seen,values])),data


def remove_files(config,data):
    state=config['state']
    data['state']='PURGING'; persist(config,data)
    try:
        for item in data['items']:
            home,quarantine,actual=remaining(config,data,item)
            if item['status']=='DELETED':
                continue
            if actual['identity'] is not None:
                if home.exists():
                    relocate_owned_directory(home,quarantine,state,item['artifact']['identity'])
                item['status']='QUARANTINED'; persist(config,data)
                _,_,actual=remaining(config,data,item)
                entries=[(quarantine/e['path'],e['sha256']) for e in actual['entries'] if e['kind']=='file']
                with guarded_removal(quarantine,entries) as remove:
                    for _ in remove():
                        pass
                for entry in sorted((e for e in actual['entries'] if e['kind']=='directory'),key=lambda e:len(Path(e['path']).parts),reverse=True):
                    remove_empty_owned_directory(quarantine/entry['path'],state,entry['identity'])
            if home.exists() or quarantine.exists():
                raise guard.RecordsError('DELETE_UNCONFIRMED')
            with guard.connect(state,actor='@admin') as db:
                db.execute('BEGIN IMMEDIATE')
                marker=dict(body_state='DELETED',body_deleted=True,operation=data['operation'],exit_code=item['exit_code'])
                db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps(marker),item['task']))
                db.execute('UPDATE context_plans SET data=? WHERE id=?',(json.dumps(marker),item['task']))
                db.execute("UPDATE records_tasks SET body_state='DELETED' WHERE task=?",(item['task'],))
                db.execute("INSERT INTO artifact_cleanup VALUES (?,'DELETED',?) ON CONFLICT(id) DO UPDATE SET state='DELETED',updated=excluded.updated",(item['task'],time.time()))
                updated=dict(data,items=[dict(value,status='DELETED') if value is item else value for value in data['items']])
                db.execute('UPDATE records_operations SET data=? WHERE operation=?',(json.dumps(updated),data['operation']))
            item['status']='DELETED'
        data['state']='DONE'; persist(config,data)
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
        data['state']='PARTIAL'
        data['reason']=error.code if isinstance(error,guard.RecordsError) else 'FILE_OR_RECEIPT_FAILURE'
        try:
            persist(config,data)
        except (OSError,ValueError,RuntimeError,sqlite3.Error):
            data['reason']='DELETE_UNCONFIRMED'
    return public(data)


def apply(payload, operation_id):
    if payload['kind'] in ('roles_prepare','flow_purge','flow_resume'):
        from .records_flow import apply as flow_apply
        return flow_apply(payload,operation_id)
    config=config_for(payload['project'],payload['config'])
    if config['_source_sha256']!=payload['config_sha256']:
        raise guard.RecordsError('PLAN_CHANGED')
    with locks(config):
        if identity(guard.database(config['state']))!=payload['identity']:
            raise guard.RecordsError('PLAN_CHANGED')
        if payload['kind']=='prepare':
            with guard.connect(config['state'],actor='@admin') as db:
                db.execute('BEGIN IMMEDIATE')
                report=prepare_report(config,payload['known_configs'],held=True,connection=db,original_identity=payload['identity'])
                if report['revision']!=payload['report']['revision'] or not report['eligible']:
                    raise guard.RecordsError('PLAN_CHANGED')
                Store.create_tables(db); guard.prepare(db,config)
                data=dict(operation=operation_id,kind='prepare',state='DONE',items=[],database_identity=identity(guard.database(config['state'])))
                db.execute("INSERT INTO records_operations VALUES (?,'DONE',?)",(operation_id,json.dumps(data)))
            return public(data)
        if payload['kind']=='resume':
            report,data=resume_report(config,payload['operation'],payload['known_configs'],held=True)
            if report['revision']!=payload['report']['revision'] or not report['eligible']:
                raise guard.RecordsError('PLAN_CHANGED')
            return remove_files(config,data)
        with guard.connect(config['state'],actor='@admin') as db:
            db.execute('BEGIN IMMEDIATE')
            report,plans=select(config,payload['task_ids'],payload['known_configs'],held=True)
            if report['revision']!=payload['report']['revision'] or not report['eligible']:
                raise guard.RecordsError('PLAN_CHANGED')
            data=dict(operation=operation_id,kind='purge',state='PURGING',config_sha256=config['_source_sha256'],database_identity=identity(guard.database(config['state'])),items=[dict(p,status='DB_SCRUBBED') for p in plans])
            for item in data['items']:
                marker=dict(body_state='PURGING',body_deleted=False,operation=operation_id,exit_code=item['exit_code'])
                db.execute('UPDATE tasks SET prompt=?,result=? WHERE id=?',('',json.dumps(marker),item['task']))
                db.execute('UPDATE context_plans SET data=? WHERE id=?',(json.dumps(marker),item['task']))
                db.execute("UPDATE records_tasks SET body_state='PURGING',operation=? WHERE task=?",(operation_id,item['task']))
            db.execute("INSERT INTO records_operations VALUES (?,'PURGING',?)",(operation_id,json.dumps(data)))
        return remove_files(config,data)


def dispatch(session,action,args):
    from .management import MAX_PLANS, PLAN_SECONDS, fail
    if action in ('records.apply','records.cancel'):
        item=session.plans.get(args['plan_id'])
        if item is None or not item[1].get('records_mutation'):
            fail('PLAN_EXPIRED')
        _,payload=session.plans.pop(args['plan_id'])
        return apply(payload,args['plan_id']) if action=='records.apply' else dict(cancelled=True)
    if action=='records.operation':
        return operation(**args)
    config=config_for(args['project'],args['config']); known=args.get('known_configs',[])
    ids=args.get('task_ids')
    if action=='records.roles.prepare.preview':
        from .records_flow import prepare_report as roles_prepare
        report=roles_prepare(config,known)
    elif action=='records.flow.purge.preview':
        from .records_flow import select as flow_select
        report,_=flow_select(config,identifier(args['flow_id']),known)
    elif action=='records.purge.preview':
        if not isinstance(ids,list) or not 1<=len(ids)<=25 or len(set(ids))!=len(ids):
            raise ValueError('Select 1..25 unique task IDs')
        ids=sorted(identifier(i) for i in ids)
        report,_=select(config,ids,known)
    elif action=='records.resume.preview':
        report,_=resume_report(config,args['operation'],known)
    else:
        report=prepare_report(config,known)
    if not report['eligible']:
        return dict(plan_id=None,report=report)
    if len(session.plans)>=MAX_PLANS:
        fail('PLAN_CAPACITY')
    plan=secrets.token_hex(24)
    session.plans[plan]=(time.monotonic()+PLAN_SECONDS,dict(records_mutation=True,kind=report['kind'],project=str(config['root']),config=str(config['config_path']),
        config_sha256=config['_source_sha256'],identity=identity(guard.database(config['state'])),known_configs=known,task_ids=ids,flow=report.get('flow'),operation=args.get('operation'),report=report))
    return dict(plan_id=plan,expires_in=PLAN_SECONDS,report=report)
