"""Explicit ordinary-queue ownership, write fencing and runner lifetime leases."""
import contextlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .storage import FileLock, Transaction, checked_local_path, identifier, path_identity, probe_lock, readonly_database

SCHEMA = 1


class RecordsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def database(state):
    return checked_local_path(Path(state) / 'queue.sqlite3')


def gate(state):
    return checked_local_path(Path(state) / 'records-management.lock')


def meta(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='records_meta'").fetchone():
        return None
    row = db.execute('SELECT * FROM records_meta WHERE id=1').fetchone()
    if not row or row['version'] not in (SCHEMA, 2, 3):
        raise RecordsError('RECORDS_SCHEMA_UNKNOWN')
    value=dict(row)
    if value['version']==3:
        frozen=db.execute('SELECT base_version FROM connection_freezes WHERE project=? AND active=1',(path_identity(value['project']),)).fetchone()
        if not frozen or frozen[0] not in (1,2): raise RecordsError('CONNECTION_GUARD_DAMAGED')
        value['base_version']=frozen[0]
    return value


def triggers(version=1):
    pending = "NOT EXISTS(SELECT 1 FROM records_operations WHERE state!='DONE')"
    normal = "EXISTS(SELECT 1 FROM records_tasks r WHERE r.task=NEW.id AND r.body_state='NORMAL')"
    runner = "EXISTS(SELECT 1 FROM records_writers w WHERE w.owner=records_actor() AND w.state='ACTIVE')"
    closed = "EXISTS(SELECT 1 FROM records_tasks r JOIN records_writers w ON r.writer=w.owner WHERE r.task=NEW.id AND w.state='CLOSED')"
    rules = {
        'tasks': {'INSERT': "records_actor()='@submit' AND " + normal + ' AND '+pending,
                  'UPDATE': normal+' AND '+pending+" AND NEW.id=OLD.id AND NEW.batch=OLD.batch AND NEW.endpoint=OLD.endpoint AND NEW.created=OLD.created AND ("+runner+" OR (records_actor()='@runtime' AND "+closed+" AND NEW.state=OLD.state AND NEW.started IS OLD.started AND NEW.finished IS OLD.finished))"},
        'context_plans': {'INSERT': "records_actor()='@submit' AND "+normal+' AND '+pending},
        'usage_snapshots': {op: runner+' AND '+normal+' AND '+pending for op in ('INSERT','UPDATE')},
        'artifact_cleanup': {op: "records_actor()='@runtime' AND "+closed+' AND '+normal+' AND '+pending for op in ('INSERT','UPDATE')},
        'control': {'UPDATE': "(records_actor()='@runtime' OR "+runner+") AND NEW.calls_started>=OLD.calls_started AND NEW.id=OLD.id"},
        'runner_lifecycle': {'INSERT': runner, 'UPDATE': "records_actor()='@runtime' OR "+runner, 'DELETE': runner},
        'records_tasks': {'INSERT': "records_actor()='@submit' AND NEW.body_state='NORMAL' AND NEW.writer IS NULL AND NEW.operation IS NULL AND EXISTS(SELECT 1 FROM records_meta m WHERE records_path(NEW.project)=records_path(m.project) AND records_path(NEW.config)=records_path(m.config)) AND "+pending,
                          'UPDATE': runner+" AND OLD.body_state='NORMAL' AND NEW.body_state=OLD.body_state AND NEW.task=OLD.task AND NEW.project=OLD.project AND NEW.config=OLD.config AND NEW.operation IS OLD.operation AND NEW.writer=records_actor() AND "+pending},
    }
    tables=['tasks','context_plans','usage_snapshots','artifact_cleanup','control','runner_lifecycle','role_tasks','records_meta','records_tasks','records_writers','records_operations']
    if version == 2:
        tables.append('records_flows')
        rules['role_tasks']={'INSERT': "records_actor()='@submit' AND EXISTS(SELECT 1 FROM records_flows f WHERE f.flow=NEW.flow AND f.body_state='NORMAL' AND f.writer_state='ACTIVE') AND "+pending}
        rules['tasks']['UPDATE'] += " OR (records_actor()='@runtime' AND OLD.state='ROLE_QUEUED' AND NEW.state='CANCELLED' AND NEW.id=OLD.id AND NEW.batch=OLD.batch AND NEW.endpoint=OLD.endpoint AND NEW.created=OLD.created AND "+normal+' AND '+pending+")"
    result=[]
    for table in tables:
        for operation in ('INSERT','UPDATE','DELETE'):
            allowed=rules.get(table,{}).get(operation,'0')
            result.append(f"CREATE TRIGGER records_guard_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT CASE WHEN records_actor()='@admin' OR ({allowed}) THEN 1 ELSE RAISE(ABORT,'RECORDS_WRITER_REQUIRED') END; END")
    return result


def validate(db):
    value=meta(db)
    if value is None:
        return None
    names={'tasks','context_plans','usage_snapshots','artifact_cleanup','control','runner_lifecycle','role_tasks','records_meta','records_tasks','records_writers','records_operations'}
    if value.get('base_version',value['version'])==2:
        names.add('records_flows')
    from . import connection_guard
    connection_guard.validate(db)
    actual={r['name']:r['sql'] for r in db.execute("SELECT name,sql,tbl_name FROM sqlite_master WHERE type='trigger'") if r['tbl_name'] in names and not r['name'].startswith(connection_guard.PREFIX)}
    expected={sql.split()[2]:sql for sql in triggers(value.get('base_version',value['version']))}
    if actual!=expected:
        raise RecordsError('RECORDS_GUARD_DAMAGED')
    return value


def connect(state, lease=None, actor='@runtime'):
    db=sqlite3.connect(Path(state)/'queue.sqlite3',timeout=15)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        from .connection_guard import functions
        functions(db)
        db.create_function('records_actor',0,lambda: lease.identity() if lease else actor)
        db.create_function('records_path',1,path_identity,deterministic=True)
        validate(db)
        return Transaction(db)
    except BaseException:
        db.close(); raise


def snapshot(state, *, live=False):
    path=database(state)
    if not path.exists():
        return None
    if live:
        # Runtime guards may race committed usage updates. A read-only rollback-mode
        # connection takes SQLite's normal read lock instead of rejecting a live journal.
        with path.open('rb') as stream:
            header=stream.read(20)
        if header[:16]!=b'SQLite format 3\x00' or header[18:20]!=b'\x01\x01':
            raise RecordsError('DATABASE_MODE_UNKNOWN')
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
        except BaseException:
            db.close(); raise
        return db
    if path.stat().st_size>64*1024*1024:
        raise RecordsError('DATABASE_SIZE_LIMIT')
    return readonly_database(path)


def binding(config, *, live=False):
    db=snapshot(config['state'],live=live)
    if db is None:
        return None
    try:
        value=validate(db)
        if value and (path_identity(value['project'])!=path_identity(config['root']) or path_identity(value['config'])!=path_identity(config['config_path'])):
            raise RecordsError('RECORDS_PROJECT_MISMATCH')
        if value:
            check_input_scope(config)
        return value
    finally:
        db.close()


def check_input_scope(config):
    from .workspace import safe
    protected=[Path(path_identity(config['state'])),Path(path_identity(config['root']/'.role-flows'))]
    for relative in config.get('include',[])+config.get('writable',[])+config.get('context_required',[]):
        candidate=Path(path_identity(safe(config['root'],relative)))
        if any(candidate==root or candidate.is_relative_to(root) or root.is_relative_to(candidate) for root in protected):
            raise RecordsError('RECORDS_INPUT_SCOPE_UNKNOWN')


def reject_roles(config, *, live=False):
    value=binding(config,live=live)
    if value and value['version']==3: raise RecordsError('CONNECTION_REMOVED')
    if value and value['version']!=2:
        raise RecordsError('ROLE_QUEUE_UPGRADE_REQUIRED')


def check_start(config, *, live=True):
    from .connection_guard import require_project
    require_project(config['root'])
    if not binding(config,live=live):
        return
    db=snapshot(config['state'],live=live)
    try:
        if meta(db)['version']==3: raise RecordsError('CONNECTION_REMOVED')
        if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone():
            raise RecordsError('BODY_PURGE_PENDING')
    finally:
        db.close()


def prepare(db, config):
    if meta(db):
        raise RecordsError('ALREADY_PREPARED')
    db.execute('CREATE TABLE records_meta(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,project TEXT NOT NULL,config TEXT NOT NULL,generation TEXT NOT NULL)')
    db.execute('CREATE TABLE records_tasks(task TEXT PRIMARY KEY,project TEXT NOT NULL,config TEXT NOT NULL,writer TEXT,body_state TEXT NOT NULL,operation TEXT)')
    db.execute('CREATE TABLE records_writers(owner TEXT PRIMARY KEY,state TEXT NOT NULL,opened REAL NOT NULL,closed REAL)')
    db.execute('CREATE TABLE records_operations(operation TEXT PRIMARY KEY,state TEXT NOT NULL,data TEXT NOT NULL)')
    db.execute('INSERT INTO records_meta VALUES (1,?,?,?,?)',(SCHEMA,str(config['root']),str(config['config_path']),uuid.uuid4().hex))
    for sql in triggers():
        db.execute(sql)


def body_state(state, task):
    db=snapshot(state)
    if db is None:
        return None
    try:
        if not validate(db):
            return None
        row=db.execute('SELECT body_state,operation FROM records_tasks WHERE task=?',(identifier(task),)).fetchone()
        return dict(row) if row else dict(body_state='LEGACY',operation=None)
    finally:
        db.close()


def require_artifacts(config, task, *, write=False, active=False, live=True):
    if not binding(config,live=live):
        return False
    db=snapshot(config['state'],live=live)
    try:
        value=db.execute('SELECT r.body_state,w.state AS writer_state FROM records_tasks r LEFT JOIN records_writers w ON r.writer=w.owner WHERE r.task=?',(identifier(task),)).fetchone()
        if value is None:
            if write:
                raise RecordsError('LEGACY_OWNERSHIP_UNCONFIRMED')
            return True
        if value['body_state']!='NORMAL':
            raise RecordsError('BODY_DELETED' if value['body_state']=='DELETED' else 'BODY_PURGE_PENDING')
        if write and db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone():
            raise RecordsError('BODY_PURGE_PENDING')
        if write and value['writer_state']!=('ACTIVE' if active else 'CLOSED'):
            raise RecordsError('RECORDS_WRITER_UNCONFIRMED')
        if write and not active and blockers(db,config['state']):
            raise RecordsError('RECORDS_WRITER_UNCONFIRMED')
        return True
    finally:
        db.close()


def decorate(db, row):
    if meta(db):
        item=db.execute('SELECT body_state,operation FROM records_tasks WHERE task=?',(row['id'],)).fetchone()
        row['body_state']=item['body_state'] if item else 'LEGACY'
        row['body_deleted']=bool(item and item['body_state']=='DELETED')
        row['body_operation']=item['operation'] if item else None
    return row


def blockers(db, state):
    values=[]
    if not meta(db):
        return ['RECORDS_PREPARATION_REQUIRED']
    for row in db.execute('SELECT * FROM records_writers'):
        lock=checked_local_path(Path(state)/'records-writers'/(identifier(row['owner'])+'.lock'))
        if row['state']!='CLOSED':
            values.append('RECORDS_WRITER_ACTIVE' if row['state']=='ACTIVE' else 'RECORDS_WRITER_LOST')
        elif probe_lock(lock)!='FREE':
            values.append('RECORDS_WRITER_UNCONFIRMED')
    if meta(db)['version']==2:
        for row in db.execute('SELECT flow,writer_state FROM records_flows'):
            if row['writer_state']!='CLOSED':
                values.append('FLOW_WRITER_'+row['writer_state'])
            elif probe_lock(checked_local_path(Path(state)/'flow-writers'/(identifier(row['flow'])+'.lock')))!='FREE':
                values.append('FLOW_WRITER_UNCONFIRMED')
    return sorted(set(values))


class Writer:
    def __init__(self,config):
        self.config=config
        self.owner=uuid.uuid4().hex
        self.lock=None
        self.stamp=None
        self.lock_stamp=None

    def identity(self):
        if self.lock is None:
            raise RecordsError('RECORDS_WRITER_LOST')
        db=database(self.config['state']).stat(); lock=self.lock.path.stat()
        if (db.st_dev,db.st_ino)!=self.stamp or (lock.st_dev,lock.st_ino)!=self.lock_stamp:
            raise RecordsError('RECORDS_WRITER_LOST')
        return self.owner

    def open(self):
        state=self.config['state']
        with FileLock(gate(state)):
            from .connection_guard import require_project
            require_project(self.config['root'])
            binding(self.config,live=True)
            self.lock=FileLock(checked_local_path(state/'records-writers'/(self.owner+'.lock')))
            self.lock.__enter__()
            try:
                with connect(state,actor='@admin') as db:
                    db.execute('BEGIN IMMEDIATE'); validate(db)
                    if probe_lock(checked_local_path(state/'runner.lock')) == 'LOCKED' or any(probe_lock(checked_local_path(state/'records-writers'/(row['owner']+'.lock'))) == 'LOCKED' for row in db.execute("SELECT owner FROM records_writers WHERE state='ACTIVE'")):
                        raise RecordsError('RECORDS_WRITER_ACTIVE')
                    if db.execute("SELECT 1 FROM records_operations WHERE state!='DONE'").fetchone():
                        raise RecordsError('BODY_PURGE_PENDING')
                    db.execute("INSERT INTO records_writers VALUES (?,'ACTIVE',?,NULL)",(self.owner,time.time()))
                info=database(state).stat(); lock=self.lock.path.stat()
                self.stamp=(info.st_dev,info.st_ino); self.lock_stamp=(lock.st_dev,lock.st_ino)
            except BaseException:
                self.release(); raise
        return self

    def release(self):
        if self.lock:
            self.lock.__exit__(None,None,None); self.lock=None

    def close(self,clean):
        try:
            self.identity()
            with FileLock(gate(self.config['state'])), connect(self.config['state'],actor='@admin') as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE records_writers SET state=?,closed=? WHERE owner=?',('CLOSED' if clean else 'LOST',time.time(),self.owner))
        finally:
            self.release()


@contextlib.contextmanager
def runner_scope(config):
    if not binding(config,live=True):
        yield None; return
    writer=Writer(config).open(); clean=False
    try:
        yield writer
        clean=True
    finally:
        writer.close(clean)
