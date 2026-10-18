"""Schema-two writer leases and database-enforced interactive write boundaries."""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .storage import FileLock, Transaction, checked_local_path, identifier, path_identity, probe_lock

SCHEMA = 2


class LedgerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def database_path(path):
    path = checked_local_path(path)
    if path.name not in ('queue.sqlite3', 'interactive.sqlite3'):
        raise LedgerError('INVALID_LEDGER')
    return path


def lock_path(path, project):
    digest = hashlib.sha256(path_identity(project).encode()).hexdigest()
    return checked_local_path(Path(str(database_path(path)) + '.owners') / ('project-' + digest + '.lock'))


def owner_path(path, owner):
    return checked_local_path(Path(str(database_path(path)) + '.owners') / (identifier(owner) + '.lock'))


def version(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='interactive_meta'").fetchone():
        return 0
    row = db.execute('SELECT version FROM interactive_meta WHERE id=1').fetchone()
    return row[0] if row else 0


def connection(path, actor=None):
    db = sqlite3.connect(database_path(path), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    from .connection_guard import functions
    functions(db)
    db.create_function('bridge_owner', 0, (lambda: '@admin') if actor == '@admin' else (lambda: actor.identity() if actor else ''))
    db.create_function('bridge_path_key', 1, path_identity, deterministic=True)
    return Transaction(db)


def trigger_sql():
    statements = []
    own_session = "EXISTS(SELECT 1 FROM interactive_owners o WHERE o.owner=bridge_owner() AND o.state='ACTIVE' AND o.owner=NEW.owner AND bridge_path_key(o.project)=bridge_path_key(NEW.project))"
    rules = {
        'interactive_sessions': {'INSERT': own_session, 'UPDATE': own_session + ' AND NEW.id=OLD.id AND NEW.owner=OLD.owner AND NEW.project=OLD.project'},
        'interactive_events': {'INSERT': "EXISTS(SELECT 1 FROM interactive_sessions s JOIN interactive_owners o ON s.owner=o.owner JOIN interactive_session_control c ON s.id=c.session WHERE s.id=NEW.session AND o.owner=bridge_owner() AND o.state='ACTIVE' AND c.redacted=0 AND c.stopped=0)"},
        'interactive_usage': {op: "EXISTS(SELECT 1 FROM interactive_sessions s JOIN interactive_owners o ON s.owner=o.owner JOIN interactive_session_control c ON s.id=c.session WHERE s.id=NEW.session AND o.owner=bridge_owner() AND o.state='ACTIVE' AND c.redacted=0 AND c.stopped=0)" for op in ('INSERT', 'UPDATE')},
        'interactive_deliveries': {op: "NEW.owner=bridge_owner() AND EXISTS(SELECT 1 FROM interactive_owners o JOIN interactive_delivery_control c ON c.delivery=NEW.id WHERE o.owner=bridge_owner() AND o.state='ACTIVE' AND c.redacted=0)" + (' AND NEW.id=OLD.id AND NEW.owner=OLD.owner' if op == 'UPDATE' else '') for op in ('INSERT', 'UPDATE')},
        'interactive_session_control': {op: "NEW.owner=bridge_owner() AND NEW.redacted=0 AND NEW.operation IS NULL AND EXISTS(SELECT 1 FROM interactive_owners o WHERE o.owner=bridge_owner() AND o.state='ACTIVE')" + (' AND NEW.session=OLD.session AND NEW.owner=OLD.owner AND OLD.redacted=0' if op == 'UPDATE' else '') for op in ('INSERT', 'UPDATE')},
        'interactive_delivery_control': {'INSERT': "NEW.redacted=0 AND NEW.operation IS NULL AND EXISTS(SELECT 1 FROM interactive_sessions a JOIN interactive_sessions b ON a.owner=b.owner JOIN interactive_owners o ON a.owner=o.owner WHERE a.id=NEW.source_session AND b.id=NEW.target_session AND o.owner=bridge_owner() AND o.state='ACTIVE' AND bridge_path_key(a.project)=bridge_path_key(NEW.project) AND bridge_path_key(b.project)=bridge_path_key(NEW.project))"},
    }
    for table in ('interactive_meta', 'interactive_owners', 'interactive_session_control', 'interactive_delivery_control', 'interactive_redactions', 'interactive_sessions', 'interactive_events', 'interactive_usage', 'interactive_deliveries'):
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            allowed = rules.get(table, {}).get(operation, '0')
            name = 'bridge_guard_' + table + '_' + operation.lower()
            statements.append(f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT CASE WHEN bridge_owner()='@admin' OR ({allowed}) THEN 1 ELSE RAISE(ABORT,'INTERACTIVE_WRITER_REQUIRED') END; END")
    return statements


def validate_schema(db):
    if version(db) != SCHEMA:
        raise LedgerError('UPGRADE_REQUIRED')
    from . import connection_guard
    connection_guard.validate(db)
    actual = {row[0]: row[1] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name GLOB 'interactive_*'") if not row[0].startswith(connection_guard.PREFIX)}
    if len(actual) != len(trigger_sql()):
        raise LedgerError('LEDGER_GUARD_DAMAGED')
    for sql in trigger_sql():
        name = sql.split()[2]
        if actual.get(name) != sql:
            raise LedgerError('LEDGER_GUARD_DAMAGED')


def upgrade(db):
    """Called only inside an explicit immediate transaction."""
    if version(db) != 1:
        raise LedgerError('UPGRADE_REQUIRED')
    if db.execute("SELECT 1 FROM interactive_sessions WHERE finished IS NULL OR state!='STOPPED' LIMIT 1").fetchone() or db.execute("SELECT 1 FROM interactive_deliveries WHERE status IN ('queued','sent','accepted') LIMIT 1").fetchone():
        raise LedgerError('LEGACY_WRITER_UNCONFIRMED')
    db.execute('CREATE TABLE interactive_owners (owner TEXT PRIMARY KEY, project TEXT NOT NULL, state TEXT NOT NULL, opened REAL NOT NULL, closed REAL)')
    db.execute('CREATE TABLE interactive_session_control (session TEXT PRIMARY KEY, owner TEXT NOT NULL, stopped INTEGER NOT NULL DEFAULT 0, redacted INTEGER NOT NULL DEFAULT 0, operation TEXT)')
    db.execute('CREATE TABLE interactive_delivery_control (delivery TEXT PRIMARY KEY, project TEXT NOT NULL, source_session TEXT NOT NULL, target_session TEXT NOT NULL, redacted INTEGER NOT NULL DEFAULT 0, operation TEXT)')
    db.execute('CREATE TABLE interactive_redactions (operation TEXT PRIMARY KEY, project TEXT NOT NULL, result TEXT NOT NULL)')
    db.execute('UPDATE interactive_meta SET version=2 WHERE id=1')
    for sql in trigger_sql():
        db.execute(sql)


def require_session(db, actor, session):
    validate_schema(db)
    if actor is None:
        raise LedgerError('WRITER_LEASE_REQUIRED')
    owner = actor.identity()
    row = db.execute('SELECT s.*,c.stopped,c.redacted FROM interactive_sessions s LEFT JOIN interactive_session_control c ON c.session=s.id WHERE s.id=?', (session,)).fetchone()
    if row is None or row['owner'] != owner or path_identity(row['project']) != path_identity(actor.project):
        raise LedgerError('SESSION_OWNER_MISMATCH')
    if row['redacted']:
        raise LedgerError('BODY_DELETED')
    if not db.execute("SELECT 1 FROM interactive_owners WHERE owner=? AND state='ACTIVE'", (owner,)).fetchone():
        raise LedgerError('WRITER_LEASE_LOST')
    return row


class WriterLease:
    def __init__(self, path, project, owner):
        self.path = database_path(path)
        self.project = checked_local_path(project)
        self.owner = identifier(owner)
        self.lock = None
        self.closed = True
        self.db_identity = None
        self.lock_identity = None

    def identity(self):
        if self.closed or self.lock is None:
            raise LedgerError('WRITER_LEASE_LOST')
        info = database_path(self.path).stat()
        lock = owner_path(self.path, self.owner).stat()
        if (info.st_dev, info.st_ino) != self.db_identity or (lock.st_dev, lock.st_ino) != self.lock_identity:
            raise LedgerError('WRITER_LEASE_LOST')
        return self.owner

    def release(self):
        if not self.closed and self.lock is not None:
            self.lock.__exit__(None, None, None)
        self.closed = True
        self.lock = None

    def open(self):
        from .connection_guard import gate, require_project
        with FileLock(gate(self.project)):
            require_project(self.project)
            return self._open()

    def _open(self):
        from . import interactive
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(lock_path(self.path, self.project)):
            lock = FileLock(owner_path(self.path, self.owner))
            lock.__enter__()
            try:
                with connection(self.path, '@admin') as db:
                    db.execute('BEGIN IMMEDIATE')
                    if version(db) == 0:
                        interactive.create_tables(db)
                        upgrade(db)
                    validate_schema(db)
                    db.execute("INSERT INTO interactive_owners VALUES (?,?,'ACTIVE',?,NULL)", (self.owner, str(self.project), time.time()))
                info = self.path.stat()
                lock_info = owner_path(self.path, self.owner).stat()
                self.db_identity = (info.st_dev, info.st_ino)
                self.lock_identity = (lock_info.st_dev, lock_info.st_ino)
                self.lock = lock
                self.closed = False
            except BaseException:
                lock.__exit__(None, None, None)
                raise
        return dict(owner=self.owner, writer_protocol=2, schema=2)

    def close(self, *, abandon=False):
        if self.closed:
            return dict(owner=self.owner, closed=True)
        self.identity()
        try:
            with FileLock(lock_path(self.path, self.project)), connection(self.path, '@admin') as db:
                db.execute('BEGIN IMMEDIATE')
                validate_schema(db)
                unfinished = db.execute('SELECT 1 FROM interactive_sessions s LEFT JOIN interactive_session_control c ON s.id=c.session WHERE s.owner=? AND (s.finished IS NULL OR c.stopped IS NOT 1) LIMIT 1', (self.owner,)).fetchone()
                state = 'LOST' if unfinished else 'CLOSED'
                if unfinished:
                    db.execute("UPDATE interactive_sessions SET state='UNCERTAIN',finished=COALESCE(finished,?) WHERE owner=? AND id IN (SELECT session FROM interactive_session_control WHERE stopped=0)", (time.time(), self.owner))
                for row in db.execute("SELECT id,data,status FROM interactive_deliveries WHERE owner=? AND status IN ('queued','sent','accepted')", (self.owner,)).fetchall():
                    data = json.loads(row['data'])
                    if not isinstance(data, dict):
                        raise LedgerError('DELIVERY_SCOPE_UNKNOWN')
                    status = 'cancelled' if row['status'] == 'queued' else 'uncertain'
                    data.update(status=status, reason='Owner ended; no automatic retry')
                    db.execute('UPDATE interactive_deliveries SET status=?,data=? WHERE id=?', (status, json.dumps(data), row['id']))
                db.execute('UPDATE interactive_owners SET state=?,closed=? WHERE owner=?', (state, time.time(), self.owner))
            return dict(owner=self.owner, state=state, closed=True)
        finally:
            if abandon or 'state' in locals():
                self.release()


def project_blockers(db, path, project):
    blockers = []
    for row in db.execute('SELECT * FROM interactive_owners'):
        if path_identity(row['project']) != path_identity(project):
            continue
        if row['state'] != 'CLOSED':
            blockers.append('WRITER_ACTIVE' if row['state'] == 'ACTIVE' else 'WRITER_LOST')
        elif probe_lock(owner_path(path, row['owner'])) != 'FREE':
            blockers.append('OWNER_CLOSE_UNCONFIRMED')
    return sorted(set(blockers))
