"""Durable project disconnection and database write fences; never run a provider."""
import json
from pathlib import Path
import sqlite3

from .storage import checked_local_path, path_identity, Transaction

MARKER='.bridge-connection.json'
PREFIX='connection_guard_'
INTERACTIVE=('interactive_meta','interactive_owners','interactive_session_control','interactive_delivery_control','interactive_redactions','interactive_sessions','interactive_events','interactive_usage','interactive_deliveries','interactive_local_requests','interactive_local_bodies','interactive_local_usage')
BATCH=('tasks','context_plans','usage_snapshots','artifact_cleanup','control','runner_lifecycle','role_tasks','records_meta','records_tasks','records_writers','records_operations','records_flows')


def gate(project):
    return checked_local_path(Path(project)/'.bridge-connection.lock')


def marker(project):
    project=checked_local_path(project); path=checked_local_path(project/MARKER,project)
    if not path.exists(): return None
    if path.stat().st_size>65536: raise ValueError('Invalid connection marker')
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema')!=1 or value.get('project')!=path_identity(project) or value.get('state') not in ('REMOVING','REMOVED','RECONNECTED'):
        raise ValueError('Invalid connection marker')
    database=checked_local_path(value['database'],project)
    if database.name!='queue.sqlite3': raise ValueError('Invalid connection database')
    return value


def database_marker(config):
    from .storage import readonly_database
    path=checked_local_path(config['state']/'queue.sqlite3')
    if not path.exists(): return None
    db=readonly_database(path)
    try:
        if not present(db): return None
        validate(db)
        row=db.execute('SELECT operation FROM connection_freezes WHERE project=? AND active=1',(path_identity(config['root']),)).fetchone()
        if row: return dict(schema=1,project=path_identity(config['root']),database=str(path),operation=row[0],state='REMOVING')
    finally: db.close()


def require_project(project,*,connection=False):
    from .records_guard import RecordsError
    value=marker(project)
    if value is None and connection and (Path(project)/'bridge.json').exists():
        from .storage import load_config
        value=database_marker(load_config(Path(project)/'bridge.json'))
    if value and value['state']!='RECONNECTED': raise RecordsError('CONNECTION_REMOVED')


def present(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='connection_freezes'").fetchone())


def triggers(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    statements=[]
    active="EXISTS(SELECT 1 FROM connection_freezes f WHERE f.active=1)"
    for table in sorted((set(BATCH)|set(INTERACTIVE)|{'connection_freezes'}) & tables):
        for op in ('INSERT','UPDATE','DELETE'):
            row='OLD' if op=='DELETE' else 'NEW'
            condition=active
            if table=='connection_freezes': condition='1'
            elif table in INTERACTIVE and table!='interactive_meta':
                if table in ('interactive_owners','interactive_sessions','interactive_redactions','interactive_delivery_control'):
                    project=f'connection_path({row}.project)'
                elif table in ('interactive_events','interactive_usage','interactive_session_control','interactive_local_requests'):
                    project=f'(SELECT connection_path(s.project) FROM interactive_sessions s WHERE s.id={row}.session)'
                elif table in ('interactive_local_bodies','interactive_local_usage'):
                    project=f'(SELECT connection_path(s.project) FROM interactive_sessions s JOIN interactive_local_requests r ON r.session=s.id WHERE r.id={row}.request)'
                else:
                    project=f'(SELECT connection_path(o.project) FROM interactive_owners o WHERE o.owner={row}.owner)'
                condition=f'EXISTS(SELECT 1 FROM connection_freezes f WHERE f.active=1 AND f.project={project})'
            statements.append(f"CREATE TRIGGER {PREFIX}{table}_{op.lower()} BEFORE {op} ON {table} WHEN {condition} BEGIN SELECT CASE WHEN connection_admin()=1 THEN 1 ELSE RAISE(ABORT,'CONNECTION_REMOVED') END; END")
    return statements


def validate(db):
    if not present(db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name GLOB 'connection_guard_*'").fetchone():
            from .records_guard import RecordsError
            raise RecordsError('CONNECTION_GUARD_DAMAGED')
        return
    actual={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'connection_guard_*'")}
    expected={sql.split()[2]:sql for sql in triggers(db)}
    if actual!=expected:
        from .records_guard import RecordsError
        raise RecordsError('CONNECTION_GUARD_DAMAGED')


def functions(db,admin=False):
    db.create_function('connection_admin',0,lambda:int(admin))
    db.create_function('connection_path',1,path_identity,deterministic=True)


def refresh(db):
    """Refresh existing fences inside an explicit schema-change transaction."""
    if not present(db):
        return
    for row in list(db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'connection_guard_*'")):
        db.execute('DROP TRIGGER '+row[0])
    for sql in triggers(db):
        db.execute(sql)


def connect(path):
    db=sqlite3.connect(checked_local_path(path),timeout=15); db.row_factory=sqlite3.Row
    functions(db,True)
    db.create_function('records_actor',0,lambda:'@admin')
    db.create_function('records_path',1,path_identity,deterministic=True)
    db.create_function('bridge_owner',0,lambda:'@admin')
    db.create_function('bridge_path_key',1,path_identity,deterministic=True)
    db.execute('PRAGMA synchronous=FULL')
    return Transaction(db)


def freeze(db,project,operation,primary=False):
    from . import records_guard, interactive, interactive_guard
    if not present(db):
        db.execute('CREATE TABLE connection_freezes(project TEXT PRIMARY KEY,operation TEXT NOT NULL,active INTEGER NOT NULL,base_version INTEGER NOT NULL)')
    if interactive_guard.version(db)==0:
        interactive.create_tables(db); interactive_guard.upgrade(db)
    base=records_guard.meta(db) if primary else None
    version=base['version'] if base else 0
    if version==3: version=base['base_version']
    row=db.execute('SELECT operation FROM connection_freezes WHERE project=? AND active=1',(path_identity(project),)).fetchone()
    if row and row[0]!=operation: raise ValueError('Connection operation changed')
    db.execute('INSERT INTO connection_freezes VALUES (?,?,1,?) ON CONFLICT(project) DO UPDATE SET operation=excluded.operation,active=1,base_version=excluded.base_version',(path_identity(project),operation,version))
    if primary: db.execute('UPDATE records_meta SET version=3 WHERE id=1')
    refresh(db)


def thaw(db,project,operation,primary=False):
    validate(db)
    row=db.execute('SELECT * FROM connection_freezes WHERE project=?',(path_identity(project),)).fetchone()
    if not row or row['operation']!=operation: raise ValueError('Connection operation changed')
    db.execute('UPDATE connection_freezes SET active=0 WHERE project=?',(path_identity(project),))
    if primary: db.execute('UPDATE records_meta SET version=? WHERE id=1',(row['base_version'],))
