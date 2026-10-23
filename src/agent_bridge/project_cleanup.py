"""Reviewed cleanup stages with body-free receipts and explicit continuation."""
import contextlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from . import connection_guard, connection_remove, history, interactive, interactive_guard, records, records_body, records_flow, records_guard
from .storage import FileLock, Transaction, checked_local_path, identifier, path_identity, readonly_database, sync_directory

SCHEMA=1
ACTIONS={
    'cleanup.inventory':({'project'},{'config','database','known_configs','databases'}),
    'cleanup.preview':({'project','config','selection'},{'known_configs','databases'}),
    'cleanup.next.preview':({'project','database','operation'},set()),
    'cleanup.apply':({'plan_id','apply'},set()),
    'cleanup.cancel':({'plan_id'},set()),
    'cleanup.operation':({'project','database','operation'},set()),
}
TABLE_SQL='CREATE TABLE cleanup_journals(operation TEXT PRIMARY KEY,project TEXT NOT NULL,data TEXT NOT NULL)'
TRIGGERS=[f"CREATE TRIGGER cleanup_guard_{op.lower()} BEFORE {op} ON cleanup_journals BEGIN SELECT CASE WHEN cleanup_actor()=1 THEN 1 ELSE RAISE(ABORT,'CLEANUP_WRITER_REQUIRED') END; END" for op in ('INSERT','UPDATE','DELETE')]


def fail(code): raise records_guard.RecordsError(code)
def digest(value): return records_body.fingerprint(value)


def ledger_path(database):
    path=checked_local_path(database)
    if path.name!='queue.sqlite3': raise ValueError('Expected the original primary queue')
    return checked_local_path(path.parent/'project-cleanup.sqlite3',path.parent)


def validate(db):
    tables={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
    triggers={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    if tables!={'cleanup_journals':TABLE_SQL} or triggers!={s.split()[2]:s for s in TRIGGERS}:
        fail('CLEANUP_JOURNAL_UNKNOWN')


def read_journals(database):
    path=ledger_path(database)
    if not path.exists() or path.stat().st_size==0: return None
    if path.stat().st_size>32*1024*1024: fail('CLEANUP_JOURNAL_LIMIT')
    db=readonly_database(path)
    try: validate(db)
    except BaseException: db.close(); raise
    return db


@contextlib.contextmanager
def journals(database):
    db=read_journals(database)
    try: yield db
    finally:
        if db is not None: db.close()


def save(data,*,create=False):
    path=ledger_path(data['database'])
    if len(json.dumps(data).encode())>256*1024: fail('CLEANUP_JOURNAL_LIMIT')
    with journals(data['database']) as old:
        if old is not None and create and any(records.object_json(r[0])['phase']!='DONE' for r in old.execute('SELECT data FROM cleanup_journals')):
            fail('CLEANUP_PENDING')
    fresh=not path.exists() or path.stat().st_size==0
    target=checked_local_path(path.with_name('project-cleanup-'+secrets.token_hex(12)+'.tmp')) if fresh else path
    before=records_body.identity(path)
    db=sqlite3.connect(target,timeout=15); db.row_factory=sqlite3.Row
    db.create_function('cleanup_actor',0,lambda:1)
    db.execute('PRAGMA synchronous=FULL')
    with Transaction(db) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='cleanup_journals'").fetchone():
            db.execute(TABLE_SQL)
            for sql in TRIGGERS: db.execute(sql)
        validate(db)
        if create:
            db.execute('INSERT INTO cleanup_journals VALUES (?,?,?)',(data['operation'],path_identity(data['project']),json.dumps(data)))
        elif db.execute('UPDATE cleanup_journals SET data=? WHERE operation=? AND project=?',(json.dumps(data),data['operation'],path_identity(data['project']))).rowcount!=1:
            fail('OPERATION_MISSING')
    if fresh:
        if records_body.identity(path)!=before: fail('PLAN_CHANGED')
        os.replace(target,path); sync_directory(path.parent)


def primary(project,database):
    root=checked_local_path(project); path=checked_local_path(database,root)
    if path.name!='queue.sqlite3': raise ValueError('Expected a queue')
    db=records_guard.snapshot(path.parent)
    if db is None: fail('RECORDS_PREPARATION_REQUIRED')
    try:
        meta=records_guard.validate(db)
        if not meta or path_identity(meta['project'])!=path_identity(root): fail('RECORDS_PROJECT_MISMATCH')
        return meta
    finally: db.close()


def load(project,database,operation):
    project=checked_local_path(project); database=checked_local_path(database,project)
    meta=primary(project,database); identifier(operation)
    db=read_journals(database)
    if db is None: fail('OPERATION_MISSING')
    try:
        row=db.execute('SELECT data FROM cleanup_journals WHERE operation=? AND project=?',(operation,path_identity(project))).fetchone()
        if not row: fail('OPERATION_MISSING')
        data=records.object_json(row[0])
        if data['schema']!=SCHEMA or data['operation']!=operation or path_identity(data['project'])!=path_identity(project) or path_identity(data['database'])!=path_identity(database) or data['database_identity']!=records_body.identity(Path(database)) or path_identity(data['config'])!=path_identity(meta['config']):
            fail('OPERATION_IDENTITY_CHANGED')
        return data
    finally: db.close()


def ids(values,limit):
    if not isinstance(values,list) or len(values)>limit or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values): raise ValueError('Select bounded unique IDs')
    return sorted(identifier(v) for v in values)


def scope(args):
    config=records_body.config_for(args['project'],args['config']); meta=records_guard.binding(config)
    if not meta: fail('RECORDS_PREPARATION_REQUIRED')
    if meta['version']==3: fail('CONNECTION_REMOVED')
    selection=args['selection']
    if not isinstance(selection,dict) or set(selection)-{'history','task_ids','flow_ids','paths'}: raise ValueError('Select supported cleanup scopes')
    tasks=ids(selection.get('task_ids',[]),25); flows=ids(selection.get('flow_ids',[]),8)
    paths=selection.get('paths',[])
    if not isinstance(paths,list) or len(paths)>64 or any(not isinstance(v,str) for v in paths) or len(set(paths))!=len(paths): raise ValueError('Select at most 64 file paths')
    for path in paths: records.relative(path)
    histories=selection.get('history',[])
    if not isinstance(histories,list) or len(histories)>4: raise ValueError('Select at most four history ledgers')
    steps=[]; seen=set(); databases=list(args.get('databases',[]))
    for entry in histories:
        if not isinstance(entry,dict) or set(entry)!={'database','session_ids'}: raise ValueError('Select an explicit history database and sessions')
        path=interactive_guard.database_path(entry['database']); selected=ids(entry['session_ids'],100)
        if not selected or path_identity(path) in seen: raise ValueError('Select unique nonempty history groups')
        seen.add(path_identity(path)); databases.append(str(path))
        steps.append(dict(kind='history',database=str(path),identity=history.file_identity(path),session_ids=selected))
    if tasks: steps.append(dict(kind='tasks',task_ids=tasks))
    steps.extend(dict(kind='flow',flow_id=flow) for flow in flows)
    databases=list({path_identity(p):str(checked_local_path(p)) for p in databases}.values())
    if len(databases)>8: raise ValueError('Select at most eight known ledgers')
    if paths: steps.append(dict(kind='connection',paths=sorted(paths)))
    if not steps: raise ValueError('Explicitly select at least one cleanup target')
    db=records_guard.snapshot(config['state'])
    try:
        for task in tasks:
            row=db.execute('SELECT * FROM records_tasks WHERE task=?',(task,)).fetchone()
            if not row or row['body_state']!='NORMAL' or row['project']!=meta['project'] or row['config']!=meta['config'] or db.execute('SELECT 1 FROM role_tasks WHERE id=?',(task,)).fetchone(): fail('CLEANUP_SCOPE_UNKNOWN')
        for flow in flows:
            if meta['version']!=2: fail('ROLE_QUEUE_UPGRADE_REQUIRED')
            row=db.execute('SELECT * FROM records_flows WHERE flow=?',(flow,)).fetchone()
            if not row or row['body_state']!='NORMAL' or row['project']!=meta['project'] or row['config']!=meta['config']: fail('CLEANUP_SCOPE_UNKNOWN')
        if paths:
            for row in db.execute('SELECT t.id,r.body_state,f.flow FROM tasks t LEFT JOIN records_tasks r ON t.id=r.task LEFT JOIN role_tasks f ON f.id=t.id'):
                if row['body_state']!='DELETED' and row['id'] not in tasks and row['flow'] not in flows: fail('CLEANUP_SELECTION_INCOMPLETE')
            if meta['version']==2 and any(row[0] not in flows for row in db.execute("SELECT flow FROM records_flows WHERE body_state!='DELETED'")): fail('CLEANUP_SELECTION_INCOMPLETE')
    finally: db.close()
    if paths:
        selected_history={path_identity(s['database']):set(s['session_ids']) for s in steps if s['kind']=='history'}
        for path in [config['state']/'queue.sqlite3',*[Path(p) for p in databases]]:
            if not path.exists(): continue
            with contextlib.closing(interactive.read_database(path)) as db:
                version=interactive_guard.version(db)
                if version in interactive_guard.SUPPORTED:
                    interactive_guard.validate_schema(db)
                    for row in db.execute('SELECT s.id,s.project,c.redacted FROM interactive_sessions s LEFT JOIN interactive_session_control c ON c.session=s.id'):
                        if path_identity(row['project'])==path_identity(config['root']) and row['redacted']!=1 and row['id'] not in selected_history.get(path_identity(path),set()): fail('CLEANUP_SELECTION_INCOMPLETE')
                    if version==3:
                        for row in db.execute("SELECT r.session,s.project FROM interactive_local_requests r LEFT JOIN interactive_sessions s ON s.id=r.session LEFT JOIN interactive_local_bodies b ON b.request=r.id WHERE r.redacted!=1 OR b.request IS NULL OR b.binding!='' OR b.request_json!='' OR b.response!=''"):
                            if row['project'] is None: fail('CLEANUP_SCOPE_UNKNOWN')
                            if path_identity(row['project'])==path_identity(config['root']) and row['session'] not in selected_history.get(path_identity(path),set()): fail('CLEANUP_SELECTION_INCOMPLETE')
    data=dict(schema=SCHEMA,project=str(config['root']),config=str(config['config_path']),config_sha256=config['_source_sha256'],database=str(config['state']/'queue.sqlite3'),
              database_identity=records_body.identity(config['state']/'queue.sqlite3'),known_configs=args.get('known_configs',[]),databases=databases,
              steps=[dict(s,status='WAITING',operation=None,attempts=[]) for s in steps],phase='WAITING')
    with journals(data['database']) as db:
        if db is not None:
            if any(records.object_json(r[0]).get('phase')!='DONE' for r in db.execute('SELECT data FROM cleanup_journals WHERE project=?',(path_identity(config['root']),))): fail('CLEANUP_PENDING')
    return data


def child(session,step,action,args):
    if step['kind']=='history': return history.dispatch(session,action,args)
    if step['kind']=='connection': return connection_remove.dispatch(session,action,args)
    return records_body.dispatch(session,action,args)


def child_plan(session,data,step):
    base=dict(project=data['project'],config=data['config'],known_configs=data['known_configs'])
    if step['kind']=='history':
        if history.file_identity(Path(step['database']))!=step['identity']: fail('OPERATION_IDENTITY_CHANGED')
        return child(session,step,'history.purge.preview',dict(project=data['project'],database=step['database'],session_ids=step['session_ids']))
    if step['kind']=='connection':
        if step['status']=='PARTIAL': return child(session,step,'connection.resume.preview',dict(project=data['project'],database=data['database'],operation=step['operation']))
        return child(session,step,'connection.remove.preview',dict(base,paths=step['paths'],databases=data['databases']))
    if step['status']=='PARTIAL': return child(session,step,'records.resume.preview',dict(base,operation=step['operation']))
    return child(session,step,'records.purge.preview' if step['kind']=='tasks' else 'records.flow.purge.preview',dict(base,**({'task_ids':step['task_ids']} if step['kind']=='tasks' else {'flow_id':step['flow_id']})))


def cancel_child(session,step,plan):
    if plan: child(session,step,'history.cancel' if step['kind']=='history' else 'connection.cancel' if step['kind']=='connection' else 'records.cancel',{'plan_id':plan})


def child_result(data,step):
    if not step.get('operation'): return dict(found=False)
    if step['kind']=='history':
        if history.file_identity(Path(step['database']))!=step['identity']: fail('OPERATION_IDENTITY_CHANGED')
        return history.operation(Path(step['database']),Path(data['project']),step['operation'])
    if step['kind']=='connection':
        try:
            result=connection_remove.public(connection_remove.load(data['project'],data['database'],step['operation'])); result.pop('ok'); return dict(found=True,**result)
        except records_guard.RecordsError as error:
            if error.code=='OPERATION_MISSING': return dict(found=False)
            raise
    return records_body.operation(data['project'],data['database'],step['operation'])


def observed(data):
    data=json.loads(json.dumps(data)); effects=[]
    for step in data['steps']:
        if step['status']=='WAITING': continue
        result=child_result(data,step)
        if result.get('found'):
            complete=result.get('body_deleted') if step['kind']=='history' else result.get('complete')
            step['status']='DONE' if complete else 'PARTIAL'
            if complete and step['kind']=='history': effects.append({key:result.get(key,[]) for key in ('session_ids','node_ids','delivery_ids')})
        elif step['status']!='DONE': step['status']='NOT_ACCEPTED'
        else: fail('CHILD_RECEIPT_MISSING')
    return data,effects


def residual(data):
    result=dict(control_ledger_retained=True,coordinator_retained=True,originals_retained=True,forensic_erasure=False,other_workspaces='NOT_SCANNED',provider_history='OUTSIDE_SCOPE',backups='PRESERVED',task_bodies=None,flow_bodies=None,history_bodies=None,backup_files=None,connection_removed=None,unknown=[])
    try:
        with contextlib.closing(records_guard.snapshot(Path(data['database']).parent)) as db:
            meta=records_guard.validate(db)
            mark=connection_guard.marker(Path(data['project']))
            result['connection_removed']=meta['version']==3 or bool(mark and mark['state']!='RECONNECTED')
            result['task_bodies']=db.execute("SELECT COUNT(*) FROM tasks t LEFT JOIN records_tasks r ON r.task=t.id WHERE r.body_state IS NULL OR r.body_state!='DELETED'").fetchone()[0]
            result['flow_bodies']=db.execute("SELECT COUNT(*) FROM records_flows WHERE body_state!='DELETED'").fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='records_flows'").fetchone() else 0
        count=0
        for path in {path_identity(p):Path(p) for p in [data['database'],*data['databases']]}.values():
            if not path.exists(): continue
            with contextlib.closing(interactive.read_database(path)) as db:
                version=interactive_guard.version(db)
                if version in interactive_guard.SUPPORTED:
                    interactive_guard.validate_schema(db)
                    remaining=set()
                    for row in db.execute('SELECT s.project,c.redacted FROM interactive_sessions s LEFT JOIN interactive_session_control c ON s.id=c.session'):
                        if path_identity(row['project'])==path_identity(data['project']) and row['redacted']!=1: count+=1
                    if version==3:
                        for row in db.execute("SELECT r.session,s.project,c.redacted AS session_redacted FROM interactive_local_requests r LEFT JOIN interactive_sessions s ON s.id=r.session LEFT JOIN interactive_session_control c ON c.session=s.id LEFT JOIN interactive_local_bodies b ON b.request=r.id WHERE r.redacted!=1 OR b.request IS NULL OR b.binding!='' OR b.request_json!='' OR b.response!=''"):
                            if row['project'] is None: raise ValueError('Unknown local request scope')
                            if path_identity(row['project'])==path_identity(data['project']) and row['session_redacted']==1: remaining.add(row['session'])
                        count+=len(remaining)
                elif version: raise ValueError('Legacy ledger')
        result['history_bodies']=count
        pending=[Path(data['project'])/'.bridge-integration-backups']; files=0; entries=0
        while pending:
            path=checked_local_path(pending.pop())
            if not path.exists(): continue
            entries+=1
            if entries>20000: raise ValueError('Inventory limit')
            if path.is_dir(): pending.extend(path.iterdir())
            elif path.is_file(): files+=1
            else: raise ValueError('Unknown file')
        result['backup_files']=files
    except (OSError,ValueError,RuntimeError,sqlite3.Error): result['unknown'].append('RESIDUAL_INCOMPLETE')
    return result


def report(data):
    current,effects=observed(data)
    completed=sum(s['status']=='DONE' for s in current['steps'])
    done=data['phase']=='DONE' and completed==len(current['steps'])
    state='DONE' if done else 'FINALIZE_PENDING' if completed==len(current['steps']) else 'PARTIAL' if any(s['status']=='PARTIAL' for s in current['steps']) else 'WAITING'
    left=residual(data)
    return dict(operation=data.get('operation'),project=data['project'],database=data['database'],state=state,scope_complete=done,completed_stages=completed,total_stages=len(current['steps']),connection_removed=left['connection_removed'],
                stages=[{k:v for k,v in s.items() if k not in ('identity','attempts')} for s in current['steps']],history_effects=effects,residual=left)


def inventory(args):
    root=checked_local_path(args['project']); database=args.get('database')
    mark=connection_guard.marker(root)
    if not database and mark and mark['state']!='RECONNECTED': database=mark['database']
    cfg=None
    if not database:
        cfg=records_body.config_for(root,args.get('config',root/'bridge.json')); database=str(cfg['state']/'queue.sqlite3')
    meta=primary(root,database); operations=[]
    with journals(database) as db:
        if db is not None:
            for row in db.execute('SELECT data FROM cleanup_journals WHERE project=? ORDER BY rowid DESC LIMIT 8',(path_identity(root),)):
                data=records.object_json(row[0]); operations.append(dict(operation=data['operation'],phase=data['phase']))
    result=dict(project=str(root),database=str(database),operations=operations,task_items=[],flow_items=[],history_items=[],file_items=[],blockers=[])
    if meta['version']==3: result['blockers']=['CONNECTION_REMOVED']; return result
    cfg=cfg or records_body.config_for(root,args.get('config',meta['config']))
    known=args.get('known_configs',[]); databases=args.get('databases',[])
    for kind,key in (('task','task_items'),('flow','flow_items')):
        listing=records.inspect(str(root),str(cfg['config_path']),kind=kind,limit=100,known_configs=known)
        result[key]=[item for item in listing['items'] if kind!='task' or item['ownership']!='ROLE_REFERENCE']; result[key+'_more']=listing['more']
    for path in {path_identity(p):Path(p) for p in [database,*databases]}.values():
        listed=history.listing(path,root,limit=100)
        result['history_items'].append(dict(database=str(path),items=[{k:v for k,v in row.items() if k!='name'} for row in listed['items']],more=listed['more']))
    linked=connection_remove.inventory(str(root),str(cfg['config_path']),known,databases)
    result['file_items']=linked['items']; result['blockers']=linked['blockers']
    return result


def preview(session,args):
    data=scope(args); reviews=[]; kept=None; values=[]; files=0; size=0
    try:
        for index,step in enumerate(data['steps']):
            plan=child_plan(session,data,step); review=plan['report']; blockers=list(review.get('blockers',[]))
            deferred=index>0 and step['kind']=='connection' and set(blockers)<={'BODY_CLEANUP_REQUIRED'}
            deferred=deferred or (index>0 and step['kind']=='flow' and any(s['kind']=='tasks' for s in data['steps'][:index]) and set(blockers)<={'RELATED_TASKS_REQUIRED'})
            reviews.append(dict(kind=step['kind'],status='DEFERRED' if blockers and deferred else 'READY' if plan['plan_id'] else 'BLOCKED',blockers=blockers))
            files+=review.get('files',0); size+=review.get('bytes',0)
            if blockers and not deferred: values+=blockers
            if index==0: kept=plan
            else: cancel_child(session,step,plan['plan_id'])
        if files>20000 or size>256*1024*1024: values.append('INVENTORY_LIMIT')
        if values or not kept or not kept['plan_id']:
            if kept: cancel_child(session,data['steps'][0],kept['plan_id'])
            return dict(report=dict(eligible=False,blockers=sorted(set(values)),stages=reviews)),None
        view=report(data); view.update(eligible=True,blockers=[],next_kind=data['steps'][0]['kind'],next_report=kept['report'],reviews=reviews)
        return dict(report=view),dict(data=data,index=0,child=kept['plan_id'],initial=True)
    except BaseException:
        if kept: cancel_child(session,data['steps'][0],kept.get('plan_id'))
        raise


def next_plan(session,args):
    raw=load(**args); data,_=observed(raw)
    if raw['phase']=='DONE': fail('OPERATION_ALREADY_COMPLETE')
    index=next((i for i,s in enumerate(data['steps']) if s['status']!='DONE'),len(data['steps']))
    view=report(raw); payload=dict(data=data,index=index,initial=False,scope=args,revision=digest(raw),child=None)
    if index==len(data['steps']): view.update(eligible=True,next_kind='finalize',next_report={}); return dict(report=view),payload
    step=data['steps'][index]
    if step['kind']!='connection' or step['status']!='PARTIAL':
        cfg=records_body.config_for(data['project'],data['config'])
        if cfg['_source_sha256']!=data['config_sha256']: fail('PLAN_CHANGED')
    plan=child_plan(session,data,step)
    view.update(eligible=bool(plan['plan_id']),blockers=plan['report'].get('blockers',[]),next_kind=step['kind'],next_report=plan['report'])
    payload['child']=plan['plan_id']
    return dict(report=view),payload if plan['plan_id'] else None


def apply(session,payload,operation):
    data=payload['data']; index=payload['index']; state=Path(data['database']).parent
    with FileLock(checked_local_path(state/'project-cleanup.lock')):
        if payload['initial']:
            cfg=records_body.config_for(data['project'],data['config'])
            if cfg['_source_sha256']!=data['config_sha256'] or records_body.identity(Path(data['database']))!=data['database_identity']: fail('PLAN_CHANGED')
            data=dict(data,operation=operation,created=time.time()); save(data,create=True)
        else:
            raw=load(**payload['scope'])
            if digest(raw)!=payload['revision']: fail('PLAN_CHANGED')
            actual,_=observed(raw)
            if next((i for i,s in enumerate(actual['steps']) if s['status']!='DONE'),len(actual['steps']))!=index: fail('PLAN_CHANGED')
        if index==len(data['steps']): data['phase']='DONE'; save(data); return report(data)
        step=data['steps'][index]; plan=payload['child']
        if plan not in session.plans: fail('PLAN_EXPIRED')
        if step['status']!='PARTIAL':
            if len(step['attempts'])>=8: fail('CLEANUP_ATTEMPT_LIMIT')
            if step['operation']: step['attempts'].append(step['operation'])
            step['operation']=plan
        step['status']='RUNNING'; data['phase']='RUNNING'; save(data)
        try:
            child(session,step,'history.apply' if step['kind']=='history' else 'connection.apply' if step['kind']=='connection' else 'records.apply',dict(plan_id=plan,apply=True))
            data,_=observed(data)
            data['phase']='DONE' if all(s['status']=='DONE' for s in data['steps']) else 'WAITING'
            save(data)
        except (OSError,ValueError,RuntimeError,sqlite3.Error):
            # Persisted child IDs are authoritative; an unknown response never resubmits them.
            return dict(ok=False,**report(load(data['project'],data['database'],data['operation'])))
        result=report(data)
        if result['state']=='PARTIAL': result['ok']=False
        return result


def dispatch(session,action,args):
    from .management import MAX_PLANS,PLAN_SECONDS
    if action in ('cleanup.apply','cleanup.cancel'):
        item=session.plans.get(args['plan_id'])
        if not item or not item[1].get('cleanup'): fail('PLAN_EXPIRED')
        _,payload=session.plans.pop(args['plan_id'])
        if action=='cleanup.cancel':
            if payload['child']: cancel_child(session,payload['data']['steps'][payload['index']],payload['child'])
            return dict(cancelled=True)
        return apply(session,payload,args['plan_id'])
    if action=='cleanup.inventory': return inventory(args)
    if action=='cleanup.operation': return dict(found=True,**report(load(**args)))
    if len(session.plans)>MAX_PLANS-2: fail('PLAN_CAPACITY')
    response,payload=preview(session,args) if action=='cleanup.preview' else next_plan(session,args)
    if payload is None: return dict(plan_id=None,**response)
    plan=secrets.token_hex(24); payload['cleanup']=True
    session.plans[plan]=(time.monotonic()+PLAN_SECONDS,payload)
    return dict(plan_id=plan,expires_in=PLAN_SECONDS,**response)
