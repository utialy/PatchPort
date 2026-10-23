"""Explicit schema preparation and atomic body redaction with preserved controls."""
import hashlib
import json
from pathlib import Path
import secrets
import time

from . import interactive, interactive_guard as guard
from .storage import FileLock, checked_local_path, identifier, path_identity

ACTIONS = {
    'history.list': ({'database', 'project'}, {'cursor', 'limit'}),
    'history.prepare.preview': ({'database', 'project'}, set()),
    'history.purge.preview': ({'database', 'project', 'session_ids'}, set()),
    'history.apply': ({'plan_id', 'apply'}, set()),
    'history.cancel': ({'plan_id'}, set()),
    'history.operation': ({'database', 'project', 'operation'}, set()),
}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_identity(path):
    path = guard.database_path(path)
    if not path.exists():
        return None
    info = path.stat()
    return [info.st_dev, info.st_ino]


def same_project(value, project):
    return path_identity(value) == path_identity(project)


def preparation(db):
    schema = guard.version(db) if db is not None else 0
    blockers, snapshot = [], []
    if schema in guard.SUPPORTED:
        guard.validate_schema(db)
        blockers.append('ALREADY_READY')
    elif schema == 1:
        for table in ('interactive_sessions', 'interactive_events', 'interactive_deliveries', 'interactive_usage'):
            snapshot.append([table, [dict(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]])
        if db.execute("SELECT 1 FROM interactive_sessions WHERE finished IS NULL OR state!='STOPPED' LIMIT 1").fetchone() or db.execute("SELECT 1 FROM interactive_deliveries WHERE status IN ('queued','sent','accepted') LIMIT 1").fetchone():
            blockers.append('LEGACY_WRITER_UNCONFIRMED')
    elif schema != 0:
        blockers.append('UNSUPPORTED_SCHEMA')
    return dict(kind='prepare', schema=schema, blockers=blockers, eligible=not blockers,
                legacy_preserved=True, legacy_deletion_supported=False, revision=fingerprint([schema, snapshot]))


def selection(db, path, project, ids):
    guard.validate_schema(db)
    blockers = guard.project_blockers(db, path, project)
    sessions, events, usage, deliveries, controls, required = [], [], [], [], [], set()
    local_requests, local_bodies, local_usage = [], [], []
    nodes = set()
    for sid in ids:
        row = db.execute('SELECT s.*,c.stopped,c.redacted,c.operation FROM interactive_sessions s LEFT JOIN interactive_session_control c ON c.session=s.id WHERE s.id=?', (sid,)).fetchone()
        if row is None or not same_project(row['project'], project):
            raise guard.LedgerError('SESSION_PROJECT_MISMATCH')
        sessions.append(dict(row)); nodes.add(row['node'])
        if row['stopped'] is None:
            blockers.append('LEGACY_SESSION')
        elif row['redacted']:
            blockers.append('ALREADY_DELETED')
        elif row['stopped'] != 1 or row['state'] != 'STOPPED' or row['finished'] is None:
            blockers.append('SESSION_EXIT_UNCONFIRMED')
        owner = db.execute('SELECT * FROM interactive_owners WHERE owner=?', (row['owner'],)).fetchone()
        if not owner or owner['state'] != 'CLOSED' or not same_project(owner['project'], project):
            blockers.append('OWNER_CLOSE_UNCONFIRMED')
        events.extend(dict(item) for item in db.execute('SELECT * FROM interactive_events WHERE session=? ORDER BY id', (sid,)))
        usage.extend(dict(item) for item in db.execute('SELECT * FROM interactive_usage WHERE session=?', (sid,)))
        if guard.version(db) == 3:
            local_requests.extend(dict(item) for item in db.execute('SELECT * FROM interactive_local_requests WHERE session=? ORDER BY id', (sid,)))
            local_bodies.extend(dict(item) for item in db.execute('SELECT b.* FROM interactive_local_bodies b JOIN interactive_local_requests r ON r.id=b.request WHERE r.session=? ORDER BY b.request', (sid,)))
            local_usage.extend(dict(item) for item in db.execute('SELECT u.* FROM interactive_local_usage u JOIN interactive_local_requests r ON r.id=u.request WHERE r.session=? ORDER BY u.request', (sid,)))
    if any(item['state'] not in ('COMPLETED', 'FAILED', 'TRUNCATED', 'CANCELLED', 'UNCERTAIN') for item in local_requests):
        blockers.append('LOCAL_REQUEST_PENDING')
    if len(local_requests) != len(local_bodies):
        blockers.append('LOCAL_BODY_SCOPE_UNKNOWN')
    for row in db.execute('SELECT d.*,c.project AS linked_project,c.source_session,c.target_session,c.redacted FROM interactive_deliveries d LEFT JOIN interactive_delivery_control c ON c.delivery=d.id ORDER BY d.id'):
        data = json.loads(row['data'])
        if not isinstance(data, dict):
            raise guard.LedgerError('DELIVERY_SCOPE_UNKNOWN')
        if row['source_session'] is None:
            if (not data.get('project') or same_project(data['project'], project)) and (not data.get('source') or not data.get('target') or data.get('source') in nodes or data.get('target') in nodes):
                blockers.append('DELIVERY_SCOPE_UNKNOWN')
            continue
        if row['source_session'] not in ids and row['target_session'] not in ids:
            continue
        if not same_project(row['linked_project'], project) or not same_project(data.get('project', ''), project):
            blockers.append('DELIVERY_SCOPE_UNKNOWN')
        if row['source_session'] not in ids:
            required.add(row['source_session'])
        if row['target_session'] not in ids:
            required.add(row['target_session'])
        if row['status'] in ('queued', 'sent', 'accepted'):
            blockers.append('DELIVERY_PENDING')
        deliveries.append(dict(row))
        controls.append(dict(db.execute('SELECT * FROM interactive_delivery_control WHERE delivery=?', (row['id'],)).fetchone()))
    if required:
        blockers.append('RELATED_SESSIONS_REQUIRED')
    owners = [dict(row) for row in db.execute('SELECT * FROM interactive_owners ORDER BY owner') if same_project(row['project'], project)]
    revision = fingerprint([sessions, events, usage, deliveries, controls, owners, local_requests, local_bodies, local_usage])
    return dict(kind='purge', schema=guard.version(db), session_ids=ids, sessions=len(sessions), events=len(events), deliveries=len(deliveries),
                local_requests=len(local_requests), local_bodies=len(local_bodies), local_body_bytes=sum(len(item[key].encode('utf-8')) for item in local_bodies for key in ('binding', 'request_json', 'response')),
                blockers=sorted(set(blockers)), required_sessions=sorted(required), eligible=not blockers, revision=revision,
                preserves=['usage', 'group_attempts', 'task_ids', 'control', 'other_sessions', 'provider_history']), deliveries


def listing(path, project, cursor='', limit=25):
    if not isinstance(cursor, str) or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid history page')
    if cursor:
        identifier(cursor)
    path, project = guard.database_path(path), checked_local_path(project)
    if not path.exists():
        return dict(schema=0, items=[], more=False, next_cursor=None, preparation_required=True)
    db = interactive.read_database(path)
    try:
        schema = guard.version(db)
        if schema == 0:
            return dict(schema=0, items=[], more=False, next_cursor=None, preparation_required=True)
        if schema in guard.SUPPORTED:
            guard.validate_schema(db)
        elif schema != 1:
            raise guard.LedgerError('UNSUPPORTED_SCHEMA')
        rows = db.execute('SELECT * FROM interactive_sessions WHERE bridge_path_key(project)=? AND id>? ORDER BY id LIMIT ?', (path_identity(project), cursor, limit+1)).fetchall()
        items = []
        for row in rows[:limit]:
            control = db.execute('SELECT * FROM interactive_session_control WHERE session=?', (row['id'],)).fetchone() if schema in guard.SUPPORTED else None
            deleted = bool(control and control['redacted'])
            reason = 'BODY_DELETED' if deleted else 'LEGACY_SESSION' if not control else 'SESSION_EXIT_UNCONFIRMED' if control['stopped'] != 1 or row['state'] != 'STOPPED' or row['finished'] is None else 'READY'
            item = dict(id=row['id'], node=row['node'], provider=row['provider'], name=row['name'], started=row['started'], finished=row['finished'], state=row['state'], body_deleted=deleted, reason=reason, selectable=reason=='READY')
            if schema == 3:
                local = db.execute("SELECT COUNT(*),SUM(state NOT IN ('COMPLETED','FAILED','TRUNCATED','CANCELLED','UNCERTAIN')) FROM interactive_local_requests WHERE session=?", (row['id'],)).fetchone()
                item.update(local_requests=local[0], local_pending=local[1] or 0)
                if local[1]:
                    item.update(reason='LOCAL_REQUEST_PENDING', selectable=False)
            items.append(item)
        return dict(schema=schema, items=items, more=len(rows)>limit, next_cursor=rows[limit-1]['id'] if len(rows)>limit else None,
                    preparation_required=schema not in guard.SUPPORTED, blockers=guard.project_blockers(db, path, project) if schema in guard.SUPPORTED else [])
    finally:
        db.close()


def operation(path, project, operation_id):
    identifier(operation_id)
    if not guard.database_path(path).exists():
        return dict(found=False, operation=operation_id)
    db = interactive.read_database(path)
    try:
        if guard.version(db) not in guard.SUPPORTED:
            return dict(found=False, operation=operation_id)
        guard.validate_schema(db)
        row = db.execute('SELECT result FROM interactive_redactions WHERE operation=? AND bridge_path_key(project)=?', (operation_id, path_identity(project))).fetchone()
        return dict(found=True, **json.loads(row[0])) if row else dict(found=False, operation=operation_id)
    finally:
        db.close()


def apply(payload, operation_id):
    path, project = guard.database_path(payload['database']), checked_local_path(payload['project'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(guard.lock_path(path, project)):
        if file_identity(path) != payload['identity']:
            raise guard.LedgerError('PLAN_CHANGED')
        with guard.connection(path, '@admin') as db:
            db.execute('BEGIN IMMEDIATE')
            if payload['kind'] == 'prepare':
                current = preparation(db)
                if current['revision'] != payload['report']['revision'] or not current['eligible']:
                    raise guard.LedgerError('PLAN_CHANGED')
                if guard.version(db) == 0:
                    interactive.create_tables(db)
                guard.upgrade(db)
                result = dict(operation=operation_id, kind='prepare', schema=2, legacy_preserved=True)
            else:
                current, deliveries = selection(db, path, project, payload['session_ids'])
                if current['revision'] != payload['report']['revision'] or not current['eligible']:
                    raise guard.LedgerError('PLAN_CHANGED')
                for sid in payload['session_ids']:
                    db.execute('UPDATE interactive_events SET data=? WHERE session=?', ('{"body_deleted":true}', sid))
                    db.execute("UPDATE interactive_sessions SET name='' WHERE id=?", (sid,))
                    if guard.version(db) == 3:
                        db.execute("UPDATE interactive_local_bodies SET binding='',request_json='',response='' WHERE request IN (SELECT id FROM interactive_local_requests WHERE session=?)", (sid,))
                        db.execute('UPDATE interactive_local_requests SET redacted=1 WHERE session=?', (sid,))
                    db.execute('UPDATE interactive_session_control SET redacted=1,operation=? WHERE session=?', (operation_id, sid))
                keep = {'id','owner','project','source','target','source_session','target_session','edgeId','generation','root','hops','groupId','round','status','created'}
                for row in deliveries:
                    data = {key:value for key,value in json.loads(row['data']).items() if key in keep}
                    data['body_deleted'] = True
                    db.execute('UPDATE interactive_deliveries SET data=? WHERE id=?', (json.dumps(data), row['id']))
                    db.execute('UPDATE interactive_delivery_control SET redacted=1,operation=? WHERE delivery=?', (operation_id, row['id']))
                nodes = sorted({db.execute('SELECT node FROM interactive_sessions WHERE id=?', (sid,)).fetchone()[0] for sid in payload['session_ids']})
                request_ids = sorted(row[0] for sid in payload['session_ids'] for row in db.execute('SELECT id FROM interactive_local_requests WHERE session=?', (sid,))) if guard.version(db) == 3 else []
                result = dict(operation=operation_id, kind='purge', body_deleted=True, session_ids=payload['session_ids'], node_ids=nodes, delivery_ids=[row['id'] for row in deliveries], local_request_ids=request_ids, sessions=current['sessions'], events=current['events'], deliveries=current['deliveries'], local_requests=current['local_requests'], local_bodies=current['local_bodies'])
            db.execute('INSERT INTO interactive_redactions VALUES (?,?,?)', (operation_id, str(project), json.dumps(result)))
            return result


def dispatch(session, action, args):
    from .management import MAX_PLANS, PLAN_SECONDS, fail
    if action in ('history.apply', 'history.cancel'):
        item = session.plans.get(args['plan_id'])
        if item is None or not item[1].get('history'):
            fail('PLAN_EXPIRED', 'Inspect the operation result or create a new preview')
        _, payload = session.plans.pop(args['plan_id'])
        return apply(payload, args['plan_id']) if action == 'history.apply' else dict(cancelled=True)
    path, project = guard.database_path(args['database']), checked_local_path(args['project'])
    if action == 'history.list':
        return listing(path, project, args.get('cursor', ''), args.get('limit', 25))
    if action == 'history.operation':
        return operation(path, project, args['operation'])
    ids = args.get('session_ids')
    if action == 'history.purge.preview':
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError('Select 1..100 sessions')
        for sid in ids:
            identifier(sid)
        if len(set(ids)) != len(ids):
            raise ValueError('Select unique sessions')
        ids = sorted(ids)
    identity = file_identity(path)
    db = interactive.read_database(path) if path.exists() else None
    try:
        if action == 'history.prepare.preview':
            report = preparation(db)
        else:
            if db is None:
                raise guard.LedgerError('UPGRADE_REQUIRED')
            report, _ = selection(db, path, project, ids)
    finally:
        if db is not None:
            db.close()
    if identity != file_identity(path):
        raise guard.LedgerError('PLAN_CHANGED')
    if not report['eligible']:
        return dict(plan_id=None, report=report)
    if len(session.plans) >= MAX_PLANS:
        fail('PLAN_CAPACITY')
    plan_id = secrets.token_hex(24)
    session.plans[plan_id] = (time.monotonic()+PLAN_SECONDS, dict(history=True, kind=report['kind'], database=str(path), project=str(project), identity=identity, session_ids=ids, report=report))
    return dict(plan_id=plan_id, expires_in=PLAN_SECONDS, report=report)
