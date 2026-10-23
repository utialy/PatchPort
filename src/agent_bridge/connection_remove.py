"""Explicit integration-file removal with durable receipts independent of config files."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from . import connection_guard as guard, records_guard, records_body, interactive_guard, runner_manager
from .storage import FileLock, atomic_json, checked_local_path, guarded_removal, identifier, path_identity, probe_lock, readonly_database

SCHEMA=1
ACTIONS={
    'connection.inventory':({'project'},{'config','known_configs','databases'}),
    'connection.remove.preview':({'project','config','paths'},{'known_configs','databases'}),
    'connection.resume.preview':({'project','database','operation'},set()),
    'connection.reconnect.preview':({'project','database','operation'},set()),
    'connection.operation':({'project','database','operation'},set()),
    'connection.apply':({'plan_id','apply'},set()),
    'connection.cancel':({'plan_id'},set()),
}
MANAGED={name+'.py' for name in ('bridge','role_flow','role_review','role_manage','project_overview','flow_lifecycle','flow_cleanup','flow_archive','flow_archive_writer')}
MANAGED|={root+'/'+tail for root in ('.agents/skills/peer-consult','.claude/skills/peer-consult') for tail in ('SKILL.md','references/usage.md','references/roles.md')}


def fail(code): raise records_guard.RecordsError(code)
def fingerprint(value): return records_body.fingerprint(value)
def identity(path): return records_body.identity(path)


def checked_config(path):
    path=checked_local_path(path)
    if path.stat().st_size>4*1024*1024: fail('CONNECTION_CONFIG_LIMIT')
    return runner_manager.checked_config(path)


def file_info(root,relative):
    from .records import relative as check_relative
    path=checked_local_path(root/check_relative(relative),root)
    if not path.exists(): return None
    if not path.is_file() or path.stat().st_size>64*1024*1024: fail('CONNECTION_FILE_LIMIT')
    before=path.stat()
    with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
    after=path.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns): fail('PLAN_CHANGED')
    return dict(path=relative,identity=[before.st_dev,before.st_ino],sha256=digest,bytes=before.st_size)


def read_manifest(root):
    item=file_info(root,'.bridge-integration.json')
    if item is None: return None,None
    if item['bytes']>4*1024*1024: fail('CONNECTION_MANIFEST_LIMIT')
    from .records import object_json, SHA
    value=object_json((root/item['path']).read_text(encoding='utf-8-sig'))
    if set(value)!={'schema','python','files'} or value['schema']!=1 or not isinstance(value['python'],str) or not isinstance(value['files'],dict) or len(value['files'])>128:
        fail('CONNECTION_MANIFEST_UNKNOWN')
    if any(not isinstance(d,str) or not SHA.fullmatch(d) for d in value['files'].values()): fail('CONNECTION_MANIFEST_UNKNOWN')
    return value,item


def database_list(primary,extra):
    if not isinstance(extra,list) or len(extra)>8: raise ValueError('Select at most eight known ledgers')
    found={path_identity(primary):dict(path=str(primary),identity=identity(primary),primary=True)}
    for raw in extra:
        path=interactive_guard.database_path(raw)
        if path.exists(): found.setdefault(path_identity(path),dict(path=str(path),identity=identity(path),primary=False))
    return sorted(found.values(),key=lambda x:path_identity(x['path']))


def other_configs(root,state,config,known,selected=()):
    values=[]; snapshots=[]
    if not isinstance(known,list) or len(known)>16: raise ValueError('Select at most sixteen known configs')
    for candidate in known:
        path=checked_local_path(candidate)
        if path_identity(path)==path_identity(config) or not path.exists(): continue
        other=checked_config(path); snapshots.append([str(path),other['_source_sha256']])
        a,b=Path(path_identity(state)),Path(path_identity(other['state']))
        if a==b or a.is_relative_to(b) or b.is_relative_to(a) or path_identity(other['root'])==path_identity(root): values.append('CONNECTION_SHARED_REFERENCE')
        for rel in other.get('include',[])+other.get('writable',[]):
            source=Path(path_identity(other['root']/rel))
            if any(Path(path_identity(root/name))==source or Path(path_identity(root/name)).is_relative_to(source) for name in selected): values.append('CONNECTION_SHARED_REFERENCE')
    return values,snapshots


def blockers(root,state,ledgers,operation=None,*,held=False):
    values=[]; stamps=[]
    if os.name!='nt': values.append('CONNECTION_PLATFORM_UNSUPPORTED')
    if checked_local_path(state/'service.json').exists(): values.append('SERVICE_REGISTRATION_PRESENT')
    if any(state.glob('archive-staging-*')): values.append('ARCHIVE_STAGING_UNCONFIRMED')
    for name in ('runner-management.lock','runner.lock','promotion.lock'):
        if not held and probe_lock(checked_local_path(state/name))=='LOCKED': values.append('RUNNER_OR_MANAGEMENT_ACTIVE')
    for item in ledgers:
        path=checked_local_path(item['path'])
        if identity(path)!=item['identity']: fail('OPERATION_IDENTITY_CHANGED')
        if not path.exists(): values.append('RECORDS_PREPARATION_REQUIRED'); continue
        db=readonly_database(path)
        try:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not item['primary'] and ('tasks' in tables or 'control' in tables): values.append('CONNECTION_SHARED_LEDGER')
            if item['primary']:
                meta=records_guard.validate(db)
                if not meta: values.append('RECORDS_PREPARATION_REQUIRED')
                else:
                    if path_identity(meta['project'])!=path_identity(root): values.append('RECORDS_PROJECT_MISMATCH')
                    values+=records_guard.blockers(db,state)
                    if db.execute('SELECT 1 FROM records_operations WHERE state!=? AND operation!=? LIMIT 1',('DONE',operation or '')).fetchone(): values.append('BODY_PURGE_PENDING')
                    if db.execute("SELECT 1 FROM tasks t LEFT JOIN records_tasks r ON r.task=t.id WHERE r.body_state IS NULL OR r.body_state!='DELETED' OR t.finished IS NULL").fetchone(): values.append('BODY_CLEANUP_REQUIRED')
                    if 'records_flows' in tables and db.execute("SELECT 1 FROM records_flows WHERE body_state!='DELETED' OR writer_state!='CLOSED'").fetchone(): values.append('BODY_CLEANUP_REQUIRED')
                lifecycle=db.execute('SELECT data FROM runner_lifecycle WHERE id=1').fetchone() if 'runner_lifecycle' in tables else None
                lifecycle=json.loads(lifecycle[0]) if lifecycle else {}
                if lifecycle and lifecycle.get('mode')!='STOPPED': values.append('RUNNER_UNCONFIRMED')
                launch=checked_local_path(state/'runner-launch.json')
                if launch.exists():
                    raw=launch.read_bytes(); ticket=json.loads(raw)
                    if ticket.get('outcome')!='FAILED' and lifecycle.get('confirmed_launch_sha256')!=hashlib.sha256(raw).hexdigest(): values.append('RUNNER_UNCONFIRMED')
            version=interactive_guard.version(db)
            if not item['primary'] and version not in interactive_guard.SUPPORTED: values.append('HISTORY_PREPARATION_REQUIRED')
            if version not in (0,*interactive_guard.SUPPORTED): values.append('HISTORY_PREPARATION_REQUIRED')
            if version in interactive_guard.SUPPORTED:
                interactive_guard.validate_schema(db)
                if any(path_identity(r[0])!=path_identity(root) for r in db.execute('SELECT project FROM interactive_sessions UNION SELECT project FROM interactive_owners')): values.append('CONNECTION_SHARED_LEDGER')
                if db.execute("SELECT 1 FROM interactive_owners WHERE state!='CLOSED'").fetchone(): values.append('KNOWN_LEDGER_WRITER_ACTIVE')
                values+=interactive_guard.project_blockers(db,path,root)
                rows=db.execute('SELECT s.project,c.redacted,s.finished FROM interactive_sessions s LEFT JOIN interactive_session_control c ON s.id=c.session')
                if any(path_identity(r['project'])==path_identity(root) and (r['redacted']!=1 or r['finished'] is None) for r in rows): values.append('BODY_CLEANUP_REQUIRED')
                if db.execute('SELECT 1 FROM interactive_deliveries d LEFT JOIN interactive_delivery_control c ON d.id=c.delivery WHERE c.delivery IS NULL').fetchone(): values.append('LEDGER_SCOPE_UNKNOWN')
                for row in db.execute('SELECT project,redacted FROM interactive_delivery_control'):
                    if path_identity(row['project'])==path_identity(root) and row['redacted']!=1: values.append('BODY_CLEANUP_REQUIRED')
                if version==3:
                    rows=db.execute('SELECT s.project,r.redacted,b.request,b.binding,b.request_json,b.response FROM interactive_local_requests r LEFT JOIN interactive_sessions s ON s.id=r.session LEFT JOIN interactive_local_bodies b ON b.request=r.id')
                    for row in rows:
                        if row['project'] is None or row['request'] is None:
                            values.append('LEDGER_SCOPE_UNKNOWN')
                        elif path_identity(row['project'])==path_identity(root) and (row['redacted']!=1 or any(row[key] for key in ('binding','request_json','response'))):
                            values.append('BODY_CLEANUP_REQUIRED')
            if operation is None: stamps.append([item['path'],hashlib.sha256(db.serialize()).hexdigest()])
        finally: db.close()
    return sorted(set(values)),stamps


def inventory(project,config=None,known_configs=None,databases=None,*,_held=False):
    root=checked_local_path(project)
    mark=guard.marker(root)
    if mark and mark['state']!='RECONNECTED':
        return dict(project=str(root),items=[],eligible=False,blockers=['CONNECTION_REMOVED'],receipt={k:mark[k] for k in ('project','database','operation')},connection_state=mark['state'])
    cfg=checked_config(config or root/'bridge.json')
    if path_identity(cfg['root'])!=path_identity(root): fail('RECORDS_PROJECT_MISMATCH')
    mark=guard.database_marker(cfg)
    if mark:
        return dict(project=str(root),items=[],eligible=False,blockers=['CONNECTION_REMOVED'],receipt={k:mark[k] for k in ('project','database','operation')},connection_state=mark['state'])
    config_path=checked_local_path(cfg['config_path'],root); config_relative=config_path.relative_to(root).as_posix()
    if any(p in {'.git','.aws','.ssh','.codex','.vscode','.agents','.claude','.venv','venv','node_modules','__pycache__'} for p in config_path.relative_to(root).parts): fail('CONNECTION_CONFIG_SCOPE_UNKNOWN')
    manifest,stamp=read_manifest(root); items=[]
    for relative,digest in (manifest or {}).get('files',{}).items():
        if relative not in MANAGED:
            items.append(dict(path=relative,kind='unowned',selectable=False,reason='MANIFEST_SCOPE_UNKNOWN')); continue
        info=file_info(root,relative)
        items.append(dict(info or {'path':relative},kind='managed',selectable=bool(info and info['sha256']==digest),reason='OWNED_HASH_MATCH' if info and info['sha256']==digest else 'MODIFIED_OR_MISSING'))
    for relative in dict.fromkeys((config_relative,'role_workflow.json','tasks/request.md')):
        info=file_info(root,relative)
        if info: items.append(dict(info,kind='user_config' if relative==config_relative else 'user_file',selectable=True,reason='EXPLICIT_USER_SELECTION_REQUIRED'))
    ledgers=database_list(cfg['state']/'queue.sqlite3',databases or [])
    values,db_stamps=blockers(root,cfg['state'],ledgers,held=_held)
    refs,known=other_configs(root,cfg['state'],config_path,known_configs or [])
    values+=refs
    return dict(project=str(root),config=str(config_path),state=str(cfg['state']),database=str(cfg['state']/'queue.sqlite3'),config_sha256=cfg['_source_sha256'],items=items,
                manifest=manifest,manifest_stamp=stamp,ledgers=ledgers,blockers=sorted(set(values)),eligible=not values,
                known_configs=known_configs or [],revision=fingerprint([items,stamp,db_stamps,known,values]),connection_state='ATTACHED')


def preview(args,*,held=False):
    report=inventory(**{k:v for k,v in args.items() if k!='paths'},_held=held)
    paths=args['paths']
    if not isinstance(paths,list) or not 1<=len(paths)<=64 or len(set(paths))!=len(paths) or any(not isinstance(p,str) for p in paths): raise ValueError('Select 1..64 explicit relative file paths')
    if report['connection_state']!='ATTACHED': return report,None
    selected=[]; values=list(report['blockers'])
    for name in paths:
        match=[i for i in report['items'] if i['path']==name and i['selectable']]
        if len(match)!=1: values.append('CONNECTION_FILE_NOT_OWNED')
        else: selected.append(match[0])
    root=Path(report['project']); state=Path(report['state'])
    refs,_=other_configs(root,state,report['config'],report['known_configs'],paths); values+=refs
    selected.sort(key=lambda i:(i['kind']=='user_config',i['kind']!='managed',i['path']))
    payload={k:report[k] for k in ('project','config','state','database','config_sha256','manifest','manifest_stamp','ledgers','known_configs')}
    payload['items']=[dict(item,status='PENDING') for item in selected]
    receipt_bytes=len(json.dumps(payload).encode())
    if receipt_bytes>4*1024*1024 or Path(report['database']).exists() and Path(report['database']).stat().st_size+2*receipt_bytes+2*1024*1024>64*1024*1024:
        values.append('RECEIPT_SIZE_LIMIT')
    result=dict(kind='remove',project=report['project'],database=report['database'],paths=[i['path'] for i in selected],items=selected,
                eligible=not values,blockers=sorted(set(values)),files=len(selected),bytes=sum(i.get('bytes',0) for i in selected),
                preserves=['database','ids','usage','control','backups','unselected_files','other_projects','profiles','credentials'],
                compatibility='EXPLICIT_RECONNECT_REQUIRED',revision=fingerprint([report['revision'],selected,values]))
    return result,payload


def load(project,database,operation):
    root=checked_local_path(project); path=checked_local_path(database,root); identifier(operation)
    if path.name!='queue.sqlite3': raise ValueError('Expected primary queue')
    db=readonly_database(path)
    try:
        meta=records_guard.validate(db)
        if not meta or path_identity(meta['project'])!=path_identity(root): fail('RECORDS_PROJECT_MISMATCH')
        row=db.execute('SELECT data FROM records_operations WHERE operation=?',(operation,)).fetchone()
        if not row: fail('OPERATION_MISSING')
        data=json.loads(row[0])
        if data.get('kind')!='connection_remove' or data['database_identity']!=identity(path): fail('OPERATION_IDENTITY_CHANGED')
        return data
    finally: db.close()


def public(data):
    mark=guard.marker(Path(data['project']))
    reconnected=bool(data.get('reconnected') and mark and mark['state']=='RECONNECTED')
    return dict(operation=data['operation'],project=data['project'],database=data['database'],kind='connection_remove',state=data['phase'],
                config=data['config'],
                complete=data['phase']=='DONE',ok=data['phase']=='DONE',reconnected=reconnected,
                items=[{k:i[k] for k in ('path','kind','status')} for i in data['items']],
                deleted=sum(i['status']=='DELETED' for i in data['items']),files=len(data['items']),
                reconnect_pending=bool(data.get('reconnected') and not reconnected),records_preserved=True)


def persist(data):
    with guard.connect(data['database']) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE records_operations SET state=?,data=? WHERE operation=?',('DONE' if data['phase']=='DONE' else 'PARTIAL',json.dumps(data),data['operation']))


def publish(data,state):
    existing=guard.marker(Path(data['project']))
    if existing and existing['operation']!=data['operation'] and existing['state']!='RECONNECTED': fail('CONNECTION_MARKER_CHANGED')
    atomic_json(Path(data['project'])/guard.MARKER,dict(schema=1,project=path_identity(data['project']),database=data['database'],operation=data['operation'],state=state))


@contextlib.contextmanager
def locks(data):
    root=Path(data['project']); state=Path(data['state'])
    with FileLock(guard.gate(root)),FileLock(records_guard.gate(state)),contextlib.ExitStack() as stack:
        for item in data['ledgers']:
            stack.enter_context(FileLock(interactive_guard.lock_path(item['path'],root)))
        for name in ('runner-management.lock','runner.lock','promotion.lock'):
            stack.enter_context(FileLock(checked_local_path(state/name)))
        yield


def remaining(data):
    root=Path(data['project']); facts=[]
    for item in data['items']:
        actual=file_info(root,item['path'])
        if item['status']=='DELETED' and actual is not None: fail('ARTIFACT_RECREATED')
        if actual and any(actual[k]!=item[k] for k in ('sha256','identity','bytes')): fail('PLAN_CHANGED')
        facts.append(actual)
    manifest,stamp=read_manifest(root)
    desired=dict(data['manifest'],files={k:v for k,v in data['manifest']['files'].items() if k not in {i['path'] for i in data['items'] if i['kind']=='managed'}}) if data['manifest'] else None
    if manifest not in (data['manifest'],desired): fail('PLAN_CHANGED')
    if manifest==data['manifest'] and stamp!=data['manifest_stamp'] and not data.get('manifest_writing'): fail('PLAN_CHANGED')
    return facts,desired


def resume_report(data,reconnect=False,*,held=False):
    root=Path(data['project']); state=Path(data['state']); values=[]
    if reconnect:
        if data['phase']!='DONE': values.append('CONNECTION_REMOVE_PENDING')
        cfg=checked_config(data['config'])
        if path_identity(cfg['root'])!=path_identity(root) or path_identity(cfg['state'])!=path_identity(state): values.append('RECORDS_PROJECT_MISMATCH')
        facts=cfg['_source_sha256']
    else:
        if data['phase']=='DONE': values.append('OPERATION_ALREADY_COMPLETE')
        facts,_=remaining(data)
    blocked,_=blockers(root,state,data['ledgers'],data['operation'],held=held); values+=blocked
    for item in data['ledgers']:
        db=readonly_database(Path(item['path']))
        try:
            row=db.execute('SELECT operation,active FROM connection_freezes WHERE project=?',(path_identity(root),)).fetchone() if guard.present(db) else None
            if row and row['operation']!=data['operation'] and (row['active'] or reconnect): values.append('OPERATION_IDENTITY_CHANGED')
            if reconnect and not row: values.append('OPERATION_IDENTITY_CHANGED')
        finally: db.close()
    refs,known=other_configs(root,state,data['config'],data['known_configs'],[i['path'] for i in data['items']]); values+=refs
    return dict(kind='reconnect' if reconnect else 'resume',operation=data['operation'],database=data['database'],project=data['project'],paths=[i['path'] for i in data['items']],
                eligible=not values,blockers=sorted(set(values)),revision=fingerprint([data,facts,known,values]))


def continue_remove(data):
    root=Path(data['project'])
    try:
        publish(data,'REMOVING')
        for item in data['ledgers']:
            if identity(Path(item['path']))!=item['identity']: fail('OPERATION_IDENTITY_CHANGED')
            with guard.connect(item['path']) as db:
                db.execute('BEGIN IMMEDIATE'); guard.freeze(db,root,data['operation'],item['primary'])
        facts,desired=remaining(data)
        targets=[(root/i['path'],i['sha256']) for i,actual in zip(data['items'],facts) if actual]
        with guarded_removal(root,targets) as remove:
            for removed in remove():
                relative=Path(removed).relative_to(root).as_posix()
                for item in data['items']:
                    if item['path']==relative: item['status']='DELETED'
                persist(data)
        for item in data['items']:
            if (root/item['path']).exists(): fail('DELETE_UNCONFIRMED')
            item['status']='DELETED'
        if desired is not None:
            data['manifest_writing']=True; persist(data)
            atomic_json(root/'.bridge-integration.json',desired)
        publish(data,'REMOVED'); data['phase']='DONE'; persist(data)
    except (OSError,ValueError,RuntimeError,sqlite3.Error):
        data['phase']='PARTIAL'
        try: persist(data)
        except (OSError,ValueError,RuntimeError,sqlite3.Error): pass
    return public(data)


def apply(payload,plan_id):
    if payload['kind']=='remove':
        data=payload['data']
        with locks(data):
            with guard.connect(data['database']) as db:
                db.execute('BEGIN IMMEDIATE')
                report,fresh=preview(dict(project=data['project'],config=data['config'],paths=payload['report']['paths'],known_configs=data['known_configs'],databases=[i['path'] for i in data['ledgers'] if not i['primary']]),held=True)
                if not report['eligible'] or report['revision']!=payload['report']['revision']: fail('PLAN_CHANGED')
                data=dict(fresh,operation=plan_id,kind='connection_remove',phase='PARTIAL',database_identity=identity(Path(data['database'])))
                guard.freeze(db,Path(data['project']),plan_id,True)
                db.execute("INSERT INTO records_operations VALUES (?,'PARTIAL',?)",(plan_id,json.dumps(data)))
            return continue_remove(data)
    data=load(**payload['scope'])
    with locks(data):
        data=load(**payload['scope'])
        report=resume_report(data,payload['kind']=='reconnect',held=True)
        if report['revision']!=payload['report']['revision'] or not report['eligible']: fail('PLAN_CHANGED')
        if payload['kind']=='resume': return continue_remove(data)
        for item in sorted(data['ledgers'],key=lambda i:i['primary']):
            with guard.connect(item['path']) as db:
                db.execute('BEGIN IMMEDIATE'); guard.thaw(db,data['project'],data['operation'],item['primary'])
        data['reconnected']=True; persist(data)
        publish(data,'RECONNECTED')
        return public(data)


def dispatch(session,action,args):
    from .management import MAX_PLANS,PLAN_SECONDS,fail as problem
    if action in ('connection.apply','connection.cancel'):
        item=session.plans.get(args['plan_id'])
        if not item or not item[1].get('connection_mutation'): problem('PLAN_EXPIRED')
        _,payload=session.plans.pop(args['plan_id'])
        return dict(cancelled=True) if action=='connection.cancel' else apply(payload,args['plan_id'])
    if action=='connection.operation':
        result=public(load(**args)); result.pop('ok')
        return dict(found=True,**result)
    if action=='connection.inventory':
        report=inventory(**args)
        return {k:v for k,v in report.items() if k not in ('manifest','manifest_stamp','ledgers','known_configs','config_sha256')}
    if action=='connection.remove.preview': report,data=preview(args); payload=dict(kind='remove',data=data)
    else:
        data=load(**args); report=resume_report(data,action=='connection.reconnect.preview'); payload=dict(kind=report['kind'],scope=args)
    if not report['eligible']: return dict(plan_id=None,report=report)
    if len(session.plans)>=MAX_PLANS: problem('PLAN_CAPACITY')
    plan=secrets.token_hex(24); payload.update(connection_mutation=True,report=report)
    session.plans[plan]=(time.monotonic()+PLAN_SECONDS,payload)
    return dict(plan_id=plan,expires_in=PLAN_SECONDS,report=report)
