"""Durable interactive records alongside, never inside, the batch task namespace."""
import json
import re
from pathlib import Path
import sqlite3
import time

from . import usage
from .storage import Transaction, checked_local_path, identifier, readonly_database

SCHEMA = 1
KINDS = {'session_started', 'submitted', 'input_attempt', 'accepted', 'completed',
         'failed', 'interrupted', 'uncertain', 'cancelled', 'session_stopped', 'usage'}


def connect(path):
    db = sqlite3.connect(checked_local_path(path), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    return Transaction(db)


def initialize(path):
    """Initialize only the explicitly selected interactive namespace."""
    path = checked_local_path(path)
    if path.name not in ('queue.sqlite3', 'interactive.sqlite3'):
        raise ValueError('Unexpected ledger filename')
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS interactive_meta (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)')
        row = db.execute('SELECT version FROM interactive_meta WHERE id=1').fetchone()
        if row and row['version'] != SCHEMA:
            raise ValueError('Unsupported interactive schema')
        db.execute('INSERT OR IGNORE INTO interactive_meta VALUES (1,?)', (SCHEMA,))
        db.execute('CREATE TABLE IF NOT EXISTS interactive_sessions '
                   '(id TEXT PRIMARY KEY, node TEXT NOT NULL, project TEXT NOT NULL, provider TEXT NOT NULL, '
                   'name TEXT NOT NULL, owner TEXT NOT NULL, started REAL NOT NULL, finished REAL, state TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS interactive_events '
                   '(id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES interactive_sessions(id), '
                   'kind TEXT NOT NULL, turn TEXT, at REAL NOT NULL, data TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS interactive_events_session ON interactive_events(session,at)')
        db.execute('CREATE TABLE IF NOT EXISTS interactive_deliveries '
                   '(id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS interactive_usage '
                   '(session TEXT PRIMARY KEY REFERENCES interactive_sessions(id), sequence INTEGER NOT NULL, data TEXT NOT NULL)')
    return dict(schema=SCHEMA, database=str(path))


def begin(path, session_id, node, project, provider, name, owner):
    for value in (session_id, node, owner):
        identifier(value)
    if provider not in ('claude', 'codex', 'terminal') or not isinstance(name, str) or len(name) > 80:
        raise ValueError('Invalid interactive session')
    project = str(checked_local_path(project))
    with connect(path) as db:
        db.execute('INSERT INTO interactive_sessions VALUES (?,?,?,?,?,?,?,NULL,?)',
                   (session_id, node, project, provider, name, owner, time.time(), 'STARTING'))
    return dict(session_id=session_id)


def record(path, session_id, event_id, kind, turn=None, data=None):
    identifier(session_id)
    identifier(event_id)
    if kind not in KINDS or (turn is not None and (not isinstance(turn, str) or len(turn) > 128)):
        raise ValueError('Invalid interactive event')
    if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False).encode()) > 192 * 1024:
        raise ValueError('Invalid interactive event data')
    encoded = json.dumps(data, allow_nan=False, separators=(',', ':'))
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM interactive_sessions WHERE id=?', (session_id,)).fetchone():
            raise ValueError('Unknown interactive session')
        previous = db.execute('SELECT session,kind,turn,data FROM interactive_events WHERE id=?', (event_id,)).fetchone()
        if previous:
            if tuple(previous) != (session_id, kind, turn, encoded):
                raise ValueError('Conflicting event identity')
            return dict(duplicate=True)
        db.execute('INSERT INTO interactive_events VALUES (?,?,?,?,?,?)',
                   (event_id, session_id, kind, turn, time.time(), encoded))
        state = {'session_started': 'READY', 'submitted': 'SUBMITTED', 'input_attempt': 'INPUT_ATTEMPT',
                 'accepted': 'ACCEPTED', 'completed': 'COMPLETED', 'failed': 'FAILED',
                 'interrupted': 'INTERRUPTED', 'uncertain': 'UNCERTAIN', 'session_stopped': 'STOPPED'}.get(kind)
        if state:
            db.execute('UPDATE interactive_sessions SET state=?,finished=? WHERE id=?',
                       (state, time.time() if kind == 'session_stopped' else None, session_id))
        if kind == 'usage':
            sequence = data.get('sequence')
            values = data.get('metrics')
            if type(sequence) is not int or sequence < 1 or not isinstance(values, dict):
                raise ValueError('Invalid usage sequence')
            normalized = {key: usage.number(values.get(key), key.endswith('_tokens')) for key in usage.METRICS}
            snapshot = dict(metrics=normalized, source=str(data.get('source', 'UNKNOWN'))[:80],
                            model=str(data.get('model', 'UNKNOWN'))[:200], scope='managed_session',
                            coverage=str(data.get('coverage', 'unknown'))[:120])
            db.execute('INSERT INTO interactive_usage VALUES (?,?,?) ON CONFLICT(session) DO UPDATE '
                       'SET sequence=excluded.sequence,data=excluded.data WHERE excluded.sequence>interactive_usage.sequence',
                       (session_id, sequence, json.dumps(snapshot, allow_nan=False)))
    return dict(duplicate=False)


def delivery(path, owner, data):
    identifier(owner)
    if not isinstance(data, dict):
        raise ValueError('Invalid delivery')
    identifier(data.get('id'))
    if data.get('status') not in ('queued', 'sent', 'accepted', 'completed', 'uncertain', 'cancelled'):
        raise ValueError('Invalid delivery status')
    encoded = json.dumps(data, allow_nan=False)
    if len(encoded.encode()) > 224 * 1024:
        raise ValueError('Delivery too large')
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT owner,status FROM interactive_deliveries WHERE id=?', (data['id'],)).fetchone()
        if old and (old['owner'] != owner or old['status'] in ('completed', 'uncertain', 'cancelled') and old['status'] != data['status']):
            raise ValueError('Conflicting delivery transition')
        db.execute('INSERT INTO interactive_deliveries VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE '
                   'SET status=excluded.status,at=excluded.at,data=excluded.data',
                   (data['id'], owner, data['status'], time.time(), encoded))
    return dict(recorded=True)


def end_owner(path, owner):
    """Mark only this owner's unfinished records uncertain; never resend them."""
    identifier(owner)
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE interactive_sessions SET state='UNCERTAIN',finished=? WHERE owner=? AND finished IS NULL",
                   (time.time(), owner))
        rows = db.execute("SELECT id,data,status FROM interactive_deliveries WHERE owner=? AND status IN ('queued','sent','accepted')", (owner,)).fetchall()
        for row in rows:
            data = json.loads(row['data'])
            data.update(status='cancelled' if row['status'] == 'queued' else 'uncertain', reason='Owner ended; no automatic retry')
            db.execute('UPDATE interactive_deliveries SET status=?,data=?,at=? WHERE id=?',
                       (data['status'], json.dumps(data), time.time(), row['id']))
    return dict(closed_owner=owner)


def read_event(path, project, event_id):
    identifier(event_id)
    db = readonly_database(path)
    try:
        row = db.execute('SELECT e.* FROM interactive_events e JOIN interactive_sessions s ON e.session=s.id '
                         'WHERE e.id=? AND s.project=?', (event_id, str(Path(project).resolve()))).fetchone()
        if row is None:
            raise ValueError('Interactive event not found')
        return dict(row, data=json.loads(row['data']))
    finally:
        db.close()


def resume_info(path, project, session_id):
    """Return an exact recorded provider identity without scanning global history."""
    identifier(session_id)
    db = readonly_database(path)
    try:
        row = db.execute('SELECT * FROM interactive_sessions WHERE id=? AND project=?',
                         (session_id, str(Path(project).resolve()))).fetchone()
        if row is None or row['provider'] not in ('claude', 'codex'):
            raise ValueError('No resumable managed session')
        for event in db.execute('SELECT data FROM interactive_events WHERE session=? ORDER BY at DESC', (session_id,)):
            provider_session = json.loads(event['data']).get('provider_session')
            if isinstance(provider_session, str) and re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', provider_session):
                return dict(session_id=session_id, node=row['node'], provider=row['provider'], project=row['project'],
                            provider_session=provider_session, state=row['state'], automatic_resend=False)
        raise ValueError('Provider session identity was not recorded; start a new managed session')
    finally:
        db.close()


def report(path, project, days=None, limit=50, owner=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid report limit')
    if days is not None and (type(days) is not int or not 1 <= days <= 3650):
        raise ValueError('Invalid report period')
    records, events, deliveries, group_attempts = [], [], [], {}
    if Path(path).exists():
        db = readonly_database(path)
        try:
            db.execute('BEGIN')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='interactive_sessions'").fetchone():
                sessions = db.execute('SELECT s.*,u.data AS usage FROM interactive_sessions s '
                                      'LEFT JOIN interactive_usage u ON u.session=s.id '
                                      'WHERE s.project=? AND s.started>=? ORDER BY s.started DESC',
                                      (str(Path(project).resolve()), time.time() - days * 86400 if days else 0)).fetchall()
                for row in sessions:
                    snap = json.loads(row['usage']) if row['usage'] else {}
                    records.append(dict(id=row['id'], node=row['node'], provider=row['provider'], name=row['name'],
                                        state='UNCONFIRMED' if owner and owner != row['owner'] and row['finished'] is None else row['state'], started=row['started'], finished=row['finished'],
                                        model=snap.get('model', 'UNKNOWN'), source=snap.get('source', 'UNKNOWN'),
                                        coverage=snap.get('coverage', 'unknown'), **{key:(usage.number(row['finished']-row['started']) if row['finished'] is not None else None) if key == 'elapsed_seconds' else snap.get('metrics', {}).get(key) for key in usage.METRICS}))
                events = [dict(row, data=json.loads(row['data'])) for row in db.execute(
                    'SELECT e.* FROM interactive_events e JOIN interactive_sessions s ON s.id=e.session '
                    'WHERE s.project=? ORDER BY e.at DESC LIMIT ?', (str(Path(project).resolve()), limit))]
                all_deliveries = [json.loads(row['data']) for row in db.execute('SELECT data FROM interactive_deliveries ORDER BY at DESC')]
                project_deliveries = [item for item in all_deliveries if item.get('project') == str(Path(project).resolve())]
                deliveries = project_deliveries[:limit]
                for item in project_deliveries:
                    if item.get('status') not in ('queued', 'cancelled') and item.get('groupId'):
                        key = item['groupId'] + ':' + item.get('round', 'legacy')
                        group_attempts[key] = group_attempts.get(key, 0) + 1
        finally:
            db.close()
    billable = [row for row in records if row['provider'] != 'terminal']
    for event in events:
        event['preview'] = True
        event['data'] = {key:(value[:600] if isinstance(value, str) else value) for key,value in event['data'].items()}
    for item in deliveries:
        if isinstance(item.get('text'), str):
            item['text'] = item['text'][:600]
        item['preview'] = True
    return dict(scope='interactive_managed_sessions', period_basis='session_started', days=days,
                summary=usage.totals(billable), sessions=records[:limit], more=len(records)>limit,
                events=events, deliveries=deliveries, group_attempts=group_attempts, cost_basis='provider-reported USD; not subscription bill')
