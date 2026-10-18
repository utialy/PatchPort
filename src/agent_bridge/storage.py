"""Durable local queue. SQLite transactions are the publication/claim boundary."""
import json
import errno
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid
import contextlib
import hashlib

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def configure_output():
    """Keep CLI JSON and runner progress usable under legacy console encodings."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try: reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError): pass

def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid identifier")
    return value


def path_identity(path):
    """Compare already selected paths using the platform's path case rules."""
    return os.path.normcase(os.path.normpath(str(path)))

def sync_directory(path):
    """Flush directory entries on POSIX; Windows has no portable directory fsync."""
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def durable_write(path, data):
    """Create a new regular file exclusively and flush its bytes before returning."""
    with Path(path).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def private_write(path, data):
    """Create a new private file without a permissive intermediate mode."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def check_private_owner(path):
    """Refuse foreign or group/world-writable control files on POSIX."""
    path = checked_local_path(path)
    info = path.stat()
    if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError('Control file ownership or permissions need inspection')
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def checked_local_path(path, root=None):
    """Reject link aliases before installation reads, writes, or explicit deletion."""
    path = Path(path)
    if not path.is_absolute() or (os.name == 'nt' and str(path).startswith('\\\\')):
        raise ValueError('An absolute local path is required')
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise ValueError('Linked installation path is not supported: ' + str(item))
    path = path.resolve()
    if root is not None and (path == root or not path.is_relative_to(root)):
        raise ValueError('Installation path escapes its owning root')
    return path


def canonical_selected_path(path):
    """Freeze POSIX parent aliases while rejecting a linked selected directory."""
    path = Path(os.path.abspath(path))
    if os.name == 'nt':
        return checked_local_path(path)
    if path.is_symlink():
        raise ValueError('The selected path must not be a symbolic link')
    return checked_local_path(path.parent.resolve() / path.name)


def write_executable(path, data):
    """Create a new POSIX launcher without changing shell configuration."""
    if os.name != 'posix':
        raise RuntimeError('POSIX launcher creation requires a POSIX host')
    durable_write(path, data)
    Path(path).chmod(0o755)


def remove_owned_launcher(path, expected, root):
    """Delete one hash-matching launcher under the caller's installation lock."""
    path = checked_local_path(path, root)
    before = path.stat()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or hashlib.file_digest(stream, 'sha256').hexdigest() != expected):
            raise ValueError('Launcher changed before removal')
        checked_local_path(path, root)
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise ValueError('Launcher changed during removal')
        path.unlink()
    sync_directory(path.parent)


def relocate_owned_directory(source, target, root, expected):
    """Move one verified artifact tree inside its selected state, without copying it."""
    root = checked_local_path(root)
    source, target = checked_local_path(source, root), checked_local_path(target, root)
    if source == root or target == root or target.exists():
        raise ValueError('Invalid artifact relocation')
    info = source.stat()
    if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != expected:
        raise ValueError('Artifact identity changed')
    target.parent.mkdir(parents=True, exist_ok=True)
    checked_local_path(target, root)
    source.rename(target)
    sync_directory(source.parent); sync_directory(target.parent)


def remove_empty_owned_directory(path, root, expected):
    """Remove an empty verified directory; never recurse through new entries."""
    root = checked_local_path(root)
    path = checked_local_path(path, root)
    if path == root:
        raise ValueError('Cannot remove the state root')
    if not path.exists():
        return
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != expected:
        raise ValueError('Artifact directory changed')
    path.rmdir()
    sync_directory(path.parent)


@contextlib.contextmanager
def guarded_removal(root, entries):
    """Pin every Windows deletion candidate exclusively before deleting any file."""
    if os.name != 'nt':
        raise RuntimeError('Safe GUI removal is currently verified only on Windows')
    import ctypes
    from ctypes import wintypes as w
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetFileInformationByHandle.restype = w.BOOL
    handles = {}
    root = checked_local_path(root)
    try:
        for path, digest in entries:
            path = checked_local_path(path, root)
            # GENERIC_READ | GENERIC_WRITE | DELETE also rejects mapped executable images.
            # No sharing, OPEN_EXISTING, OPEN_REPARSE_POINT; all handles stay pinned.
            handle = kernel.CreateFileW(str(path), 0xC0010000, 0, None, 3, 0x00200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except Exception:
                kernel.CloseHandle(handle)
                raise
            stream = os.fdopen(fd, 'rb')
            handles[path] = stream
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or hashlib.file_digest(stream, 'sha256').hexdigest() != digest):
                raise ValueError('Owned file changed; preserve it: ' + str(path))
        def remove():
            for path, stream in handles.items():
                checked_local_path(path, root)
                disposition = ctypes.c_ubyte(1)
                handle = msvcrt.get_osfhandle(stream.fileno())
                if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                stream.close()
                yield path
        yield remove
    finally:
        for stream in handles.values():
            stream.close()

def load_config(path, *, data=None):
    path = Path(path).resolve()
    c = json.loads(path.read_text(encoding="utf-8-sig")) if data is None else dict(data)
    root = (path.parent / c.get("project", ".")).resolve()
    if not root.is_dir():
        raise ValueError("Project directory does not exist")
    state = (root / c.get("state", ".agent-bridge")).resolve()
    state.relative_to(root)
    if state == root:
        raise ValueError("State must be a subdirectory")
    c.update(root=root, state=state, config_path=str(path))
    from .context import validate, validate_required
    validate(c.get("context_budget", {}))
    validate_required(c.get("context_required", []))
    for field in ("include", "writable"):
        if not isinstance(c.get(field), list) or not c[field]:
            raise ValueError(f"Explicit nonempty {field} paths required")
        for item in c[field]:
            if not isinstance(item, str) or not item or Path(item).is_absolute() or ".." in Path(item).parts or item == "." or "\\" in item or ":" in item:
                raise ValueError(f"Unsafe {field} path")
            target = root / item
            # State, VCS, environments and authentication must never be copied implicitly.
            if any(x in {".git", ".venv", "node_modules", ".agent-bridge", ".bridge"} for x in Path(item).parts):
                raise ValueError("Excluded data cannot be included")
            if target.resolve() == state or state.is_relative_to(target.resolve()):
                raise ValueError("Include path contains state")
    for key, endpoint in c.get("endpoints", {}).items():
        if endpoint.get("sandbox", "read-only") not in {"read-only", "workspace-write"}:
            raise ValueError("Only read-only/workspace-write sandbox modes supported")
        identifier(key)
        if endpoint.get("adapter") not in {"claude", "codex", "command"}:
            raise ValueError("Adapter must be claude, codex or command")
        argv = endpoint.get("command")
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and s for s in argv):
            raise ValueError("command must be a nonempty argument array")
        from .child import validate_launch
        validate_launch(endpoint.get('launch'))
        if endpoint.get("parallel", 1) not in range(1, 33):
            raise ValueError("Endpoint parallel must be 1..32")
    if not c.get("endpoints") or c.get("parallel", 2) not in range(1, 33):
        raise ValueError("Endpoints and parallel=1..32 required")
    if not 1 <= c.get("timeout", 600) <= 86400:
        raise ValueError("timeout must be 1..86400")
    return c

class Store:
    def __init__(self, state, *, config=None, writer=None):
        self.state = Path(state)
        self.config, self.writer = config, writer
        self.state.mkdir(parents=True, exist_ok=True)
        self.db = self.state / "queue.sqlite3"
        with self.connect() as db:
            from . import records_guard
            self.protected = bool(records_guard.meta(db))
            if self.protected:
                if config is not None:
                    saved = records_guard.meta(db)
                    if path_identity(saved['project']) != path_identity(config['root']) or path_identity(saved['config']) != path_identity(config['config_path']):
                        raise records_guard.RecordsError('RECORDS_PROJECT_MISMATCH')
                return
            self.create_tables(db)
    @staticmethod
    def create_tables(db):
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, batch TEXT, endpoint TEXT, prompt TEXT, state TEXT, created REAL, started REAL, finished REAL, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL, max_calls INTEGER, calls_started INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO control SELECT 1,0,NULL,COUNT(*) FROM tasks WHERE started IS NOT NULL")
            db.execute("CREATE TABLE IF NOT EXISTS artifact_cleanup (id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS usage_snapshots (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS context_plans (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS role_tasks (id TEXT PRIMARY KEY, flow TEXT NOT NULL, role TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(flow,role))")
            db.execute("CREATE TABLE IF NOT EXISTS runner_lifecycle (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
    def connect(self, actor='@runtime'):
        from . import records_guard
        transaction = records_guard.connect(self.state, self.writer, actor)
        saved = records_guard.meta(transaction.db)
        if saved and self.config is not None and (path_identity(saved['project']) != path_identity(self.config['root']) or path_identity(saved['config']) != path_identity(self.config['config_path'])):
            transaction.db.close()
            raise records_guard.RecordsError('RECORDS_PROJECT_MISMATCH')
        return transaction
    def submit(self, batch, targets, prompt, endpoints, context_plan=None, *, role=None, flow_writer=None):
        identifier(batch)
        if not prompt.strip() or len(prompt.encode()) > 1_000_000:
            raise ValueError("Prompt must be nonempty and <=1 MB")
        if not targets or len(set(targets)) != len(targets) or any(t not in endpoints for t in targets):
            raise ValueError("Targets must be unique configured endpoints")
        ids = [identifier(batch + "--" + t) for t in targets]
        plan_json = json.dumps(context_plan, ensure_ascii=False, allow_nan=False) if context_plan is not None else None
        summary_plan = (isinstance(context_plan, dict)
                        and isinstance(context_plan.get('payload'), dict)
                        and context_plan['payload'].get('schema') == 2)
        if role is not None:
            if summary_plan:
                raise ValueError('Role workflows require full/omit plans; summary plans are separate tasks')
            identifier(role['flow'])
            if len(targets) != 1 or role['role'] not in ('developer', 'reviewer') or targets != [role['role']] or plan_json is None:
                raise ValueError('Role submission requires one role and a frozen context plan')
            role_json = json.dumps(role, ensure_ascii=False, allow_nan=False)
        with self.connect('@submit') as db:
            db.execute("BEGIN IMMEDIATE")
            from . import records_guard
            if records_guard.meta(db):
                if self.config is not None:
                    from .connection_guard import require_project
                    require_project(self.config['root'])
                if records_guard.meta(db)['version']==3:
                    raise records_guard.RecordsError('CONNECTION_REMOVED')
                if role is not None:
                    if records_guard.meta(db)['version']!=2:
                        raise records_guard.RecordsError('ROLE_QUEUE_UPGRADE_REQUIRED')
                    if flow_writer is None or flow_writer.flow!=role['flow']:
                        raise records_guard.RecordsError('FLOW_WRITER_REQUIRED')
                    flow_writer.check(db)
                if self.config is None:
                    raise records_guard.RecordsError('RECORDS_PROJECT_REQUIRED')
                records_guard.check_input_scope(self.config)
                for summary in (context_plan or {}).get('payload', {}).get('summaries', []):
                    source = summary.get('record', {}).get('provenance', {}).get('source_task_id')
                    if source:
                        item = db.execute('SELECT body_state FROM records_tasks WHERE task=?',(source,)).fetchone()
                        if item and item[0] != 'NORMAL':
                            raise records_guard.RecordsError('BODY_DELETED')
            for id_, target in zip(ids, targets):
                if records_guard.meta(db):
                    db.execute("INSERT INTO records_tasks VALUES (?,?,?,NULL,'NORMAL',NULL)",(id_,str(self.config['root']),str(self.config['config_path'])))
                db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", (id_, batch, target, prompt, "ROLE_QUEUED" if role is not None else "SUMMARY_QUEUED" if summary_plan else "PLAN_QUEUED" if plan_json else "QUEUED", time.time(), None, None, None))
                if plan_json:
                    db.execute("INSERT INTO context_plans VALUES (?,?)", (id_, plan_json))
                if role is not None:
                    if role['role'] == 'reviewer':
                        predecessor = db.execute("SELECT t.state FROM role_tasks r JOIN tasks t ON t.id=r.id WHERE r.flow=? AND r.role='developer'", (role['flow'],)).fetchone()
                        if not predecessor or predecessor['state'] != 'DONE':
                            raise ValueError('Reviewer requires a completed developer task')
                    db.execute('INSERT INTO role_tasks VALUES (?,?,?,?)', (id_, role['flow'], role['role'], role_json))
        return ids
    def rows(self, batch=None):
        with self.connect() as db:
            sql = "SELECT * FROM tasks" + (" WHERE batch=?" if batch else "") + " ORDER BY created,id"
            rows = [dict(r) for r in db.execute(sql, (batch,) if batch else ())]
            for row in rows:
                from .records_guard import decorate
                decorate(db,row)
                if row["state"] in ("ROLE_QUEUED", "ROLE_RUNNING"):
                    row['role_task_required'] = True
                if row["state"] in ("PLAN_QUEUED", "PLAN_RUNNING", "ROLE_QUEUED", "ROLE_RUNNING", "SUMMARY_QUEUED", "SUMMARY_RUNNING"):
                    row["context_plan_required"] = True
                    row["state"] = row["state"].split('_', 1)[1]
            return rows
    def context_plan(self, id_):
        with self.connect() as db:
            row = db.execute("SELECT data FROM context_plans WHERE id=?", (id_,)).fetchone()
            return json.loads(row["data"]) if row else None
    def role_task(self, id_):
        with self.connect() as db:
            row = db.execute('SELECT data FROM role_tasks WHERE id=?', (id_,)).fetchone()
            return json.loads(row['data']) if row else None
    def cancel_queued_role(self, id_, reason):
        with self.connect() as db:
            return db.execute("UPDATE tasks SET state='CANCELLED',finished=?,result=? WHERE id=? AND state='ROLE_QUEUED'", (time.time(), json.dumps(dict(error=reason)), id_)).rowcount == 1
    def lifecycle(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM runner_lifecycle WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def register_runner(self, record):
        # Caller holds runner.lock for the complete registered lifetime.
        if self.protected and self.writer is None:
            from .records_guard import RecordsError
            raise RecordsError('RECORDS_WRITER_REQUIRED')
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runner_lifecycle VALUES (1,?)", (json.dumps(record),))

    def runner_transition(self, run_id, *, mode=None, acknowledge=False, reason=None):
        if mode not in (None, 'DRAINING', 'CANCELLING', 'STOPPED'):
            raise ValueError('Invalid runner transition')
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runner_lifecycle WHERE id=1").fetchone()
            record = json.loads(row[0]) if row else None
            if not record or record.get('protocol') != 1 or record.get('run_id') != run_id:
                raise ValueError('Runner identity changed; inspect status')
            if mode and record['mode'] != 'STOPPED':
                if mode == 'STOPPED' or record['mode'] != 'CANCELLING':
                    record['mode'] = mode
                if mode == 'STOPPED': record.update(ended=time.time(), reason=reason)
            if acknowledge: record['acknowledged'] = record['mode']
            encoded = json.dumps(record)
            if encoded != row[0]:
                db.execute("UPDATE runner_lifecycle SET data=? WHERE id=1", (encoded,))
            return record

    def claim(self, id_, run_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lifecycle = db.execute("SELECT data FROM runner_lifecycle WHERE id=1").fetchone()
            if lifecycle:
                record = json.loads(lifecycle[0])
                if record.get('mode') != 'STOPPED' or run_id is not None:
                    if (record.get('protocol') != 1 or record.get('run_id') != run_id
                            or record.get('mode') != 'RUNNING'):
                        return False
            control = db.execute("SELECT * FROM control WHERE id=1").fetchone()
            if control["paused"] or (control["max_calls"] is not None and control["calls_started"] >= control["max_calls"]):
                return False
            claimed = db.execute("UPDATE tasks SET state=CASE state WHEN 'PLAN_QUEUED' THEN 'PLAN_RUNNING' WHEN 'ROLE_QUEUED' THEN 'ROLE_RUNNING' WHEN 'SUMMARY_QUEUED' THEN 'SUMMARY_RUNNING' ELSE 'RUNNING' END,started=? WHERE id=? AND state IN ('QUEUED','PLAN_QUEUED','ROLE_QUEUED','SUMMARY_QUEUED')", (time.time(), id_)).rowcount == 1
            if claimed:
                if self.protected:
                    db.execute('UPDATE records_tasks SET writer=? WHERE task=?',(self.writer.identity(),id_))
                db.execute("UPDATE control SET calls_started=calls_started+1 WHERE id=1")
            return claimed
    def control(self):
        with self.connect() as db:
            row = dict(db.execute("SELECT paused,max_calls,calls_started FROM control WHERE id=1").fetchone())
        row["paused"] = bool(row["paused"])
        row["remaining"] = None if row["max_calls"] is None else max(0, row["max_calls"] - row["calls_started"])
        row["dispatch"] = "PAUSED" if row["paused"] else "LIMIT_REACHED" if row["remaining"] == 0 else "READY"
        return row
    def pause(self, paused=True):
        with self.connect() as db:
            db.execute("UPDATE control SET paused=? WHERE id=1", (int(paused),))
    def set_limit(self, max_calls):
        if max_calls is not None and (type(max_calls) is not int or not 0 <= max_calls <= 2**63-1):
            raise ValueError("max_calls must be a nonnegative SQLite integer or null")
        with self.connect() as db:
            db.execute("UPDATE control SET max_calls=? WHERE id=1", (max_calls,))
    def finish(self, id_, status, result):
        with self.connect() as db:
            if self.protected:
                if self.writer is None:
                    from .records_guard import RecordsError
                    raise RecordsError('RECORDS_WRITER_REQUIRED')
                db.execute('UPDATE records_tasks SET writer=? WHERE task=?',(self.writer.identity(),id_))
            db.execute("UPDATE tasks SET state=?,finished=?,result=? WHERE id=?", (status, time.time(), json.dumps(result, ensure_ascii=False), id_))
    def mark_artifacts(self, id_, state):
        with self.connect() as db:
            db.execute("INSERT INTO artifact_cleanup VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated=excluded.updated", (id_, state, time.time()))
    def record_usage(self, id_, data):
        with self.connect() as db:
            db.execute("INSERT INTO usage_snapshots VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (id_, json.dumps(data, ensure_ascii=False, allow_nan=False)))
    def artifact_status(self, id_):
        with self.connect() as db:
            row = db.execute("SELECT state,updated FROM artifact_cleanup WHERE id=?", (id_,)).fetchone()
            return dict(row) if row else None
    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE tasks SET state='INTERRUPTED',finished=?,result=? WHERE state IN ('RUNNING','PLAN_RUNNING','ROLE_RUNNING','SUMMARY_RUNNING')", (time.time(), json.dumps({"error":"Runner interrupted; inspect artifacts; retry only with a new ID"})))

class Transaction:
    def __init__(self, db): self.db = db
    def __enter__(self): return self.db
    def __exit__(self, typ, value, tb):
        try:
            self.db.rollback() if typ else self.db.commit()
        finally:
            self.db.close()


def probe_lock(path):
    """Observe an existing lock without creating or modifying its file."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 'MISSING'
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Unsafe lock file')
    with path.open('rb') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBRLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return 'LOCKED'
            raise
        else:
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return 'FREE'


def readonly_database(path):
    """Read a stable rollback-mode image into memory; never open SQLite on the source."""
    path = Path(path)
    def checked_stat():
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                raise ValueError('SQLITE_SIDECAR: inspect journal/WAL before preview')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Unsafe database file')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    before = checked_stat()
    data = path.read_bytes()
    if (checked_stat() != before or data != path.read_bytes() or checked_stat() != before):
        raise ValueError('Database changed during preview')
    if data[:16] != b'SQLite format 3\x00' or data[18:20] != b'\x01\x01':
        raise ValueError('Only a complete rollback-mode SQLite database can be previewed')
    db = sqlite3.connect(':memory:')
    try:
        if not hasattr(db, 'deserialize'):
            raise RuntimeError('SQLite deserialize support is required for a non-writing preview')
        db.deserialize(data)
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Damaged database image')
        db.row_factory = sqlite3.Row
        return db
    except Exception:
        db.close()
        raise

class FileLock:
    def __init__(self, path): self.path, self.file = Path(path), None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1): self.file.write(b"0"); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Bridge lock is busy") from None
        return self
    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
