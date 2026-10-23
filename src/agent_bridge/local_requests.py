"""Durable, single-use local inference admission; this module never calls a model."""
import hashlib
import json
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit

from . import history, interactive_guard as guard, usage
from .storage import FileLock, checked_local_path, identifier, path_identity, probe_lock

SCHEMA = 3
TERMINAL = ('COMPLETED', 'FAILED', 'TRUNCATED', 'CANCELLED', 'UNCERTAIN')
ACTIVE = ('PREPARED', 'DISPATCH_AUTHORIZED', 'STREAMING')
MAX_BODY = 128 * 1024
TABLES = {
    'interactive_local_requests': 'CREATE TABLE interactive_local_requests (id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES interactive_sessions(id), state TEXT NOT NULL, created REAL NOT NULL, dispatched REAL, finished REAL, binding_sha256 TEXT NOT NULL, body_sha256 TEXT NOT NULL, grant_sha256 TEXT, origin TEXT NOT NULL, delivery_id TEXT, root TEXT, hops INTEGER NOT NULL, reason TEXT NOT NULL, redacted INTEGER NOT NULL DEFAULT 0)',
    'interactive_local_bodies': 'CREATE TABLE interactive_local_bodies (request TEXT PRIMARY KEY REFERENCES interactive_local_requests(id), binding TEXT NOT NULL, request_json TEXT NOT NULL, response TEXT NOT NULL)',
    'interactive_local_usage': 'CREATE TABLE interactive_local_usage (request TEXT PRIMARY KEY REFERENCES interactive_local_requests(id), data TEXT NOT NULL)',
}
BASE = {'database', 'owner', 'session_id', 'request_id'}
ACTIONS = {
    'local.schema.preview': ({'database', 'project'}, set()),
    'local.schema.apply': ({'plan_id', 'apply'}, set()),
    'local.schema.cancel': ({'plan_id'}, set()),
    'local.prepare': (BASE | {'binding', 'request_body', 'apply'}, set()),
    'local.dispatch': (BASE | {'binding_sha256', 'apply'}, set()),
    'local.finish': (BASE | {'binding_sha256', 'outcome', 'apply'}, set()),
    'local.result': ({'database', 'project', 'request_id'}, set()),
    'local.context': ({'database', 'project', 'session_id'}, set()),
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def request_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid request UUID')
    return value


def validate_tables(db):
    for name, sql in TABLES.items():
        row = db.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)).fetchone()
        if row is None or row[0] != sql:
            raise guard.LedgerError('LEDGER_GUARD_DAMAGED')
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='interactive_local_delivery_once'").fetchone()
    if row is None or row[0] != 'CREATE UNIQUE INDEX interactive_local_delivery_once ON interactive_local_requests(delivery_id) WHERE delivery_id IS NOT NULL':
        raise guard.LedgerError('LEDGER_GUARD_DAMAGED')


def schema_report(db, path):
    version = guard.version(db) if db is not None else 0
    blockers, snapshot = [], []
    if version == 3:
        guard.validate_schema(db)
        blockers.append('ALREADY_READY')
    elif version in (0, 1):
        report = history.preparation(db)
        blockers.extend(report['blockers'])
        snapshot.append(report['revision'])
    elif version == 2:
        guard.validate_schema(db)
        for row in db.execute('SELECT * FROM interactive_owners ORDER BY owner'):
            snapshot.append(dict(row))
            if row['state'] != 'CLOSED':
                blockers.append('WRITER_ACTIVE' if row['state'] == 'ACTIVE' else 'WRITER_LOST')
            elif probe_lock(guard.owner_path(path, row['owner'])) != 'FREE':
                blockers.append('OWNER_CLOSE_UNCONFIRMED')
    else:
        blockers.append('UNSUPPORTED_SCHEMA')
    if db is not None:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks = [('records_operations', "state!='DONE'", 'BODY_PURGE_PENDING'),
                  ('records_writers', "state!='CLOSED'", 'WRITER_ACTIVE'),
                  ('tasks', "state LIKE '%RUNNING'", 'WRITER_ACTIVE')]
        for table, where, reason in checks:
            if table in tables:
                rows = [dict(row) for row in db.execute('SELECT * FROM ' + table + ' WHERE ' + where)]
                if rows:
                    blockers.append(reason)
                snapshot.append([table, rows])
        if 'cleanup_journals' in tables:
            rows = [dict(row) for row in db.execute('SELECT * FROM cleanup_journals')]
            if any(json.loads(row['data']).get('phase') != 'DONE' for row in rows):
                blockers.append('CLEANUP_PENDING')
            snapshot.append(['cleanup_journals', rows])
    return dict(schema=version, target_schema=SCHEMA, eligible=not blockers,
                blockers=sorted(set(blockers)), revision=digest(encoded([version, snapshot])),
                legacy_preserved=True, older_writers_blocked=True)


def upgrade(db, path):
    """Upgrade under an explicit immediate transaction after whole-ledger checks."""
    report = schema_report(db, path)
    if not report['eligible']:
        raise guard.LedgerError(report['blockers'][0])
    if guard.version(db) == 0:
        from .interactive import create_tables
        create_tables(db)
    if guard.version(db) == 1:
        guard.upgrade(db)
    for sql in guard.trigger_sql(2):
        db.execute('DROP TRIGGER ' + sql.split()[2])
    for sql in TABLES.values():
        db.execute(sql)
    db.execute('CREATE UNIQUE INDEX interactive_local_delivery_once ON interactive_local_requests(delivery_id) WHERE delivery_id IS NOT NULL')
    db.execute('UPDATE interactive_meta SET version=3 WHERE id=1')
    for sql in guard.trigger_sql(3):
        db.execute(sql)
    from . import connection_guard
    connection_guard.refresh(db)
    guard.validate_schema(db)


def binding_value(binding, body):
    fields = {'helper_id', 'generation', 'core_epoch', 'profile_revision', 'backend', 'endpoint_origin',
              'model_id', 'body_sha256', 'origin', 'delivery_id', 'root', 'hops'}
    if not isinstance(binding, dict) or not fields <= binding.keys() or binding.keys() - fields - {'model_aliases'}:
        raise ValueError('Invalid request binding')
    for key in ('helper_id', 'generation', 'core_epoch', 'profile_revision'):
        identifier(binding[key])
    if binding['backend'] not in ('ollama', 'lmstudio') or binding['origin'] not in ('human', 'peer'):
        raise ValueError('Invalid backend or origin')
    endpoint = binding['endpoint_origin']
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if parsed is None or parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1', 'localhost') or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Use a canonical loopback origin')
    port = parsed.port if parsed.port is not None else 80
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    canonical = 'http://' + ('[::1]' if parsed.hostname == '::1' else parsed.hostname) + (':' + str(port) if port != 80 else '')
    if endpoint != canonical:
        raise ValueError('Use a canonical loopback origin')
    model = binding['model_id']
    if not isinstance(model, str) or not model.strip() or len(model) > 512 or any(ord(c) < 32 or ord(c)==127 for c in model):
        raise ValueError('Invalid model')
    aliases = binding.get('model_aliases', [])
    if not isinstance(aliases, list) or len(aliases) > 16 or any(not isinstance(x, str) or not x.strip() or len(x)>512 or any(ord(c)<32 or ord(c)==127 for c in x) for x in aliases):
        raise ValueError('Invalid model aliases')
    if type(binding['hops']) is not int or not 0 <= binding['hops'] <= 12:
        raise ValueError('Invalid hops')
    if binding['origin'] == 'peer':
        identifier(binding['delivery_id']); identifier(binding['root'])
    elif binding['delivery_id'] is not None or binding['root'] is not None or binding['hops'] != 0:
        raise ValueError('Human requests cannot impersonate deliveries')
    if not isinstance(body, str) or len(body.encode('utf-8')) > MAX_BODY or digest(body) != binding['body_sha256']:
        raise ValueError('Request body hash or size mismatch')
    payload = json.loads(body)
    allowed = {'model', 'messages', 'stream', 'options'} if binding['backend'] == 'ollama' else {'model', 'messages', 'stream', 'max_tokens'}
    if not isinstance(payload, dict) or set(payload) != allowed or payload['model'] != model or payload['stream'] is not True:
        raise ValueError('Invalid text-only request')
    messages = payload['messages']
    if not isinstance(messages, list) or not 1 <= len(messages) <= 257:
        raise ValueError('Invalid messages')
    for item in messages:
        if not isinstance(item, dict) or set(item) != {'role', 'content'} or item['role'] not in ('system','user','assistant') or not isinstance(item['content'], str) or not item['content'].strip():
            raise ValueError('Text messages required')
    if messages[-1]['role'] != 'user' or not messages[-1]['content'].strip():
        raise ValueError('User input required')
    options = payload.get('options')
    if binding['backend'] == 'ollama':
        if not isinstance(options, dict) or set(options) != {'num_ctx','num_predict'}:
            raise ValueError('Explicit context and output bounds required')
        values = [options['num_ctx'], options['num_predict']]
    else:
        values = [payload['max_tokens']]
    if any(type(value) is not int or not 1 <= value <= 2097152 for value in values) or values[-1] > 32768:
        raise ValueError('Invalid token bound')
    if binding['backend'] == 'ollama' and options['num_predict'] >= options['num_ctx']:
        raise ValueError('Output must fit the context')
    return json.loads(encoded(binding))


def require_local(db, writer, sid):
    if guard.version(db) != SCHEMA:
        raise guard.LedgerError('LOCAL_UPGRADE_REQUIRED')
    row = guard.require_session(db, writer, identifier(sid))
    if row['provider'] != 'local' or row['stopped'] or row['finished'] is not None:
        raise guard.LedgerError('LOCAL_SESSION_REQUIRED')
    return row


def checked_request(db, writer, sid, rid, expected):
    require_local(db, writer, sid)
    row = db.execute('SELECT * FROM interactive_local_requests WHERE id=?', (request_id(rid),)).fetchone()
    if row is None or row['session'] != sid or row['binding_sha256'] != expected:
        raise guard.LedgerError('LOCAL_REQUEST_MISMATCH')
    if row['redacted']:
        raise guard.LedgerError('BODY_DELETED')
    checked_body(db, row)
    return row


def checked_body(db, row):
    body = db.execute('SELECT * FROM interactive_local_bodies WHERE request=?', (row['id'],)).fetchone()
    if body is None or digest(body['request_json']) != row['body_sha256']:
        raise guard.LedgerError('LOCAL_REQUEST_CHANGED')
    binding = binding_value(json.loads(body['binding']), body['request_json'])
    expected = digest(encoded(dict(session_id=row['session'], request_id=row['id'], binding=binding)))
    if expected != row['binding_sha256']:
        raise guard.LedgerError('LOCAL_REQUEST_CHANGED')
    return body


def event(db, row, kind):
    data = encoded(dict(local_request=row['id'], origin=row['origin']))
    db.execute('INSERT INTO interactive_events VALUES (?,?,?,?,?,?)',
               ('local-' + row['id'] + '-' + kind, row['session'], kind, row['id'], time.time(), data))


def check_peer(db, writer, sid, binding, body):
    linked = db.execute('SELECT d.owner,d.data,d.status,c.* FROM interactive_deliveries d JOIN interactive_delivery_control c ON c.delivery=d.id WHERE d.id=?', (binding['delivery_id'],)).fetchone()
    if linked is None or linked['owner'] != writer.identity() or linked['target_session'] != sid or linked['redacted'] or linked['status'] not in ('queued','sent','accepted') or path_identity(linked['project']) != path_identity(writer.project):
        raise guard.LedgerError('DELIVERY_SCOPE_UNKNOWN')
    data = json.loads(linked['data'])
    if any(data.get(key) != binding[key] for key in ('generation','root','hops')):
        raise guard.LedgerError('DELIVERY_IDENTITY_CHANGED')
    if json.loads(body)['messages'][-1]['content'] != data.get('text'):
        raise guard.LedgerError('DELIVERY_BODY_CHANGED')
    source = guard.require_session(db, writer, linked['source_session'])
    if source['stopped'] or source['finished'] is not None:
        raise guard.LedgerError('SESSION_ALREADY_STOPPED')


def prepare(db, writer, sid, rid, binding, body):
    request_id(rid)
    require_local(db, writer, sid)
    binding = binding_value(binding, body)
    bound = digest(encoded(dict(session_id=sid, request_id=rid, binding=binding)))
    previous = db.execute('SELECT * FROM interactive_local_requests WHERE id=?', (rid,)).fetchone()
    if previous:
        if previous['session'] != sid or previous['binding_sha256'] != bound or previous['redacted']:
            raise guard.LedgerError('LOCAL_REQUEST_MISMATCH')
        checked_body(db, previous)
        return dict(request_id=rid, state=previous['state'], binding_sha256=bound, duplicate=True)
    if db.execute("SELECT 1 FROM interactive_local_requests WHERE session=? AND state IN ('PREPARED','DISPATCH_AUTHORIZED','STREAMING')", (sid,)).fetchone():
        raise guard.LedgerError('LOCAL_REQUEST_ACTIVE')
    first = db.execute('SELECT b.binding FROM interactive_local_requests r JOIN interactive_local_bodies b ON b.request=r.id WHERE r.session=? ORDER BY r.created,r.id LIMIT 1', (sid,)).fetchone()
    if first:
        earlier = json.loads(first['binding'])
        fixed = ('helper_id','generation','core_epoch','profile_revision','backend','endpoint_origin','model_id')
        if any(earlier[key] != binding[key] for key in fixed) or earlier.get('model_aliases', []) != binding.get('model_aliases', []):
            raise guard.LedgerError('LOCAL_SESSION_BINDING_CHANGED')
    messages = json.loads(body)['messages']
    prefix = messages[:-1]
    if prefix and prefix[0]['role'] == 'system':
        prefix = prefix[1:]
    if prefix != context(db, writer.project, sid)['messages']:
        raise guard.LedgerError('LOCAL_CONTEXT_CHANGED')
    if binding['origin'] == 'peer':
        check_peer(db, writer, sid, binding, body)
    db.execute('INSERT INTO interactive_local_requests VALUES (?,?,?,?,NULL,NULL,?,?,NULL,?,?,?,?,?,0)',
               (rid, sid, 'PREPARED', time.time(), bound, binding['body_sha256'], binding['origin'], binding['delivery_id'], binding['root'], binding['hops'], 'PREPARED'))
    db.execute('INSERT INTO interactive_local_bodies VALUES (?,?,?,?)', (rid, encoded(binding), body, ''))
    row = db.execute('SELECT * FROM interactive_local_requests WHERE id=?', (rid,)).fetchone()
    event(db, row, 'submitted')
    return dict(request_id=rid, state='PREPARED', binding_sha256=bound, duplicate=False)


def set_delivery(db, row, status, reason, *, preserve_terminal=False):
    if row['delivery_id'] is None:
        return
    old = db.execute('SELECT data,status FROM interactive_deliveries WHERE id=?', (row['delivery_id'],)).fetchone()
    if old is None:
        raise guard.LedgerError('DELIVERY_SCOPE_UNKNOWN')
    if old['status'] in ('completed', 'uncertain', 'cancelled'):
        if preserve_terminal or old['status'] == status:
            return
        raise guard.LedgerError('LOCAL_DELIVERY_TERMINAL')
    data = json.loads(old['data']); data.update(status=status, reason=reason)
    db.execute('UPDATE interactive_deliveries SET status=?,data=?,at=? WHERE id=?', (status, encoded(data), time.time(), row['delivery_id']))


def dispatch_once(db, writer, sid, rid, expected):
    row = checked_request(db, writer, sid, rid, expected)
    if row['state'] != 'PREPARED':
        raise guard.LedgerError('LOCAL_GRANT_ALREADY_CONSUMED')
    body = checked_body(db, row)
    binding = json.loads(body['binding'])
    if binding['origin'] == 'peer':
        check_peer(db, writer, sid, binding, body['request_json'])
    grant = secrets.token_hex(32)
    db.execute("UPDATE interactive_local_requests SET state='DISPATCH_AUTHORIZED',dispatched=?,grant_sha256=?,reason='DISPATCH_AUTHORIZED' WHERE id=? AND state='PREPARED'", (time.time(), digest(grant), rid))
    set_delivery(db, row, 'sent', 'Local request dispatch authorized')
    db.execute("UPDATE interactive_sessions SET state='ACCEPTED' WHERE id=?", (sid,))
    event(db, row, 'accepted')
    return dict(request_id=rid, state='DISPATCH_AUTHORIZED', binding_sha256=expected, grant=grant)


def totals(db, sid):
    rows = db.execute('SELECT u.data FROM interactive_local_requests r LEFT JOIN interactive_local_usage u ON u.request=r.id WHERE r.session=? AND r.dispatched IS NOT NULL ORDER BY r.created,r.id', (sid,)).fetchall()
    values = [json.loads(row['data']) if row['data'] else {} for row in rows]
    known, missing, metrics = {}, {}, {key: None for key in usage.METRICS}
    for key in ('input_tokens', 'output_tokens'):
        known[key] = sum(item[key] for item in values if item.get(key) is not None)
        missing[key] = sum(item.get(key) is None for item in values)
        metrics[key] = known[key] if values and not missing[key] else None
    return dict(metrics=metrics, known_subtotals=known, unknown_requests=missing,
                requests=len(values), model='UNKNOWN', source='LOCAL_REQUEST_LEDGER',
                coverage='partial' if not values or any(missing.values()) else 'reported_tokens', scope='managed_session')


def refresh_usage(db, sid):
    snapshot = totals(db, sid)
    db.execute('INSERT INTO interactive_usage VALUES (?,1,?) ON CONFLICT(session) DO UPDATE SET sequence=interactive_usage.sequence+1,data=excluded.data', (sid, encoded(snapshot)))


def finish(db, writer, sid, rid, expected, outcome):
    row = checked_request(db, writer, sid, rid, expected)
    if not isinstance(outcome, dict) or set(outcome) - {'state','reason','content','reported_model','usage'} or outcome.get('state') not in TERMINAL:
        raise ValueError('Invalid request outcome')
    if row['state'] in TERMINAL:
        return dict(request_id=rid, state=row['state'], duplicate=True)
    state, reason = outcome['state'], outcome.get('reason', outcome['state'])
    if not isinstance(reason, str) or re.fullmatch('[A-Z0-9_]{1,64}', reason) is None:
        raise ValueError('Use a bounded outcome code')
    content = outcome.get('content', '')
    if not isinstance(content, str) or len(content.encode('utf-16-le')) // 2 > 32000 or len(content.encode('utf-8')) > MAX_BODY:
        raise ValueError('Response too large')
    body = checked_body(db, row)
    binding = json.loads(body['binding'])
    reported_model = outcome.get('reported_model')
    if reported_model is not None and (not isinstance(reported_model, str) or len(reported_model) > 512 or any(ord(c)<32 or ord(c)==127 for c in reported_model)):
        raise ValueError('Invalid reported model')
    if state == 'COMPLETED':
        if row['state'] not in ('DISPATCH_AUTHORIZED', 'STREAMING') or not content.strip():
            raise guard.LedgerError('LOCAL_COMPLETION_INVALID')
        if outcome.get('reported_model') not in [binding['model_id'], *binding.get('model_aliases', [])]:
            state, reason = 'FAILED', 'UNSUPPORTED_RESPONSE'
    reported = outcome.get('usage') or {}
    if not isinstance(reported, dict):
        raise ValueError('Invalid usage')
    normalized = {}
    for key in ('input_tokens', 'output_tokens'):
        value = reported.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 2**53-1):
            raise ValueError('Invalid token count')
        normalized[key] = value
    normalized['cost_usd'] = None
    normalized['requested_model_ref'] = digest(binding['model_id'])
    normalized['reported_model_ref'] = digest(reported_model) if reported_model is not None else None
    db.execute('UPDATE interactive_local_requests SET state=?,reason=?,finished=? WHERE id=?', (state, reason, time.time(), rid))
    db.execute('UPDATE interactive_local_bodies SET response=? WHERE request=?', (encoded(dict(content=content, reported_model=reported_model)), rid))
    db.execute('INSERT INTO interactive_local_usage VALUES (?,?)', (rid, encoded(normalized)))
    set_delivery(db, row, 'completed' if state == 'COMPLETED' else ('uncertain' if row['dispatched'] is not None else 'cancelled'), reason)
    db.execute('UPDATE interactive_sessions SET state=? WHERE id=?', (state, sid))
    event(db, row, 'completed' if state == 'COMPLETED' else 'failed')
    refresh_usage(db, sid)
    return dict(request_id=rid, state=state, reason=reason, duplicate=False)


def abandon_session(db, sid):
    for row in db.execute("SELECT * FROM interactive_local_requests WHERE session=? AND state IN ('PREPARED','DISPATCH_AUTHORIZED','STREAMING')", (sid,)).fetchall():
        state = 'CANCELLED' if row['state'] == 'PREPARED' else 'UNCERTAIN'
        db.execute("UPDATE interactive_local_requests SET state=?,reason='OWNER_ENDED',finished=? WHERE id=?", (state, time.time(), row['id']))
        db.execute('INSERT OR IGNORE INTO interactive_local_usage VALUES (?,?)', (row['id'], encoded(dict(input_tokens=None, output_tokens=None, cost_usd=None))))
        set_delivery(db, row, 'cancelled' if state == 'CANCELLED' else 'uncertain', 'OWNER_ENDED', preserve_terminal=True)
    refresh_usage(db, sid)


def abandon_owner(db, owner):
    for row in db.execute("SELECT id FROM interactive_sessions WHERE owner=? AND provider='local'", (owner,)).fetchall():
        abandon_session(db, row['id'])


def read_result(db, project, rid):
    row = db.execute('SELECT r.* FROM interactive_local_requests r JOIN interactive_sessions s ON s.id=r.session WHERE r.id=? AND bridge_path_key(s.project)=?', (request_id(rid), path_identity(project))).fetchone()
    if row is None:
        raise guard.LedgerError('LOCAL_REQUEST_MISMATCH')
    body = db.execute('SELECT * FROM interactive_local_bodies WHERE request=?', (rid,)).fetchone()
    report = dict(request_id=rid, session_id=row['session'], state=row['state'], reason=row['reason'],
                  binding_sha256=row['binding_sha256'], dispatched=row['dispatched'] is not None,
                  body_deleted=bool(row['redacted']), grant_available=False)
    used = db.execute('SELECT data FROM interactive_local_usage WHERE request=?', (rid,)).fetchone()
    report['usage'] = json.loads(used['data']) if used else dict(input_tokens=None, output_tokens=None, cost_usd=None)
    if not row['redacted']:
        body = checked_body(db, row)
        response = json.loads(body['response']) if body['response'] else dict(content='', reported_model=None)
        report.update(binding=json.loads(body['binding']), request_body=body['request_json'], **response)
    return report


def context(db, project, sid):
    row = db.execute('SELECT s.provider,c.redacted FROM interactive_sessions s JOIN interactive_session_control c ON c.session=s.id WHERE s.id=? AND bridge_path_key(s.project)=?', (identifier(sid), path_identity(project))).fetchone()
    if row is None or row['provider'] != 'local':
        raise guard.LedgerError('LOCAL_SESSION_REQUIRED')
    if row['redacted']:
        raise guard.LedgerError('BODY_DELETED')
    rows = db.execute("SELECT * FROM interactive_local_requests WHERE session=? AND state='COMPLETED' AND redacted=0 ORDER BY created,id LIMIT 129", (sid,)).fetchall()
    messages = []
    for item in rows:
        body = checked_body(db, item)
        messages.extend([json.loads(body['request_json'])['messages'][-1], dict(role='assistant', content=json.loads(body['response'])['content'])])
    if len(rows) > 128 or len(encoded(messages).encode('utf-8')) > MAX_BODY:
        raise guard.LedgerError('LOCAL_CONTEXT_LIMIT')
    return dict(messages=messages, completed_requests=len(rows), automatic_replay=False)


def dispatch(session, action, args):
    from . import interactive
    from .management import MAX_PLANS, PLAN_SECONDS, fail
    if action in ('local.schema.apply', 'local.schema.cancel'):
        item = session.plans.pop(args['plan_id'], None)
        if item is None or not item[1].get('local_schema'):
            fail('PLAN_EXPIRED')
        plan = item[1]
        if action.endswith('cancel'):
            return dict(cancelled=True)
        path = guard.database_path(plan['database'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(guard.lock_path(path, plan['project'])):
            if history.file_identity(path) != plan['identity']:
                raise guard.LedgerError('PLAN_CHANGED')
            with guard.connection(path, '@admin') as db:
                db.execute('BEGIN IMMEDIATE')
                current = schema_report(db, path)
                if not current['eligible'] or current['revision'] != plan['report']['revision']:
                    raise guard.LedgerError('PLAN_CHANGED')
                upgrade(db, path)
        return dict(schema=SCHEMA, local_request_schema=1, legacy_preserved=True)
    path = guard.database_path(args['database'])
    if action == 'local.schema.preview':
        project = checked_local_path(args['project'])
        identity = history.file_identity(path)
        db = interactive.read_database(path) if path.exists() else None
        try:
            report = schema_report(db, path)
        finally:
            if db is not None:
                db.close()
        if not report['eligible']:
            return dict(plan_id=None, report=report)
        if len(session.plans) >= MAX_PLANS:
            fail('PLAN_CAPACITY')
        pid = secrets.token_hex(24)
        session.plans[pid] = (time.monotonic()+PLAN_SECONDS, dict(local_schema=True, database=str(path), project=str(project), identity=identity, report=report))
        return dict(plan_id=pid, report=report, expires_in=PLAN_SECONDS)
    if action in ('local.result', 'local.context'):
        db = interactive.read_database(path)
        try:
            if guard.version(db) != SCHEMA:
                raise guard.LedgerError('LOCAL_UPGRADE_REQUIRED')
            guard.validate_schema(db)
            project = checked_local_path(args['project'])
            return read_result(db, project, args['request_id']) if action == 'local.result' else context(db, project, args['session_id'])
        finally:
            db.close()
    writer = session.writers.get((path_identity(path), args['owner']))
    if writer is None:
        raise guard.LedgerError('WRITER_LEASE_REQUIRED')
    with guard.connection(path, writer) as db:
        db.execute('BEGIN IMMEDIATE')
        base = (db, writer, args['session_id'], args['request_id'])
        if action == 'local.prepare':
            return prepare(*base, args['binding'], args['request_body'])
        if action == 'local.dispatch':
            result = dispatch_once(*base, args['binding_sha256'])
            refresh_usage(db, args['session_id'])
            return result
        return finish(*base, args['binding_sha256'], args['outcome'])
