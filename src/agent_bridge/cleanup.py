"""Explicit, retention-based removal of terminal task artifacts, never queue rows."""
import hashlib
import json
import math
import re
from pathlib import Path
import shutil
import sqlite3
import stat
import time

from .storage import FileLock, Store, checked_local_path, identifier, probe_lock, readonly_database
from .answers import preserve_before_prune
from .workspace import safe, digest

TERMINAL = {"DONE", "ERROR", "TIMEOUT", "CANCELLED", "INTERRUPTED"}


def inspect_tree(root):
    """Include hidden files and excluded workspace names; reject all reparse points."""
    size = 0
    latest = 0
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("Links/reparse points prevent cleanup")
        latest = max(latest, info.st_mtime)
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
        else:
            raise ValueError("Unsupported artifact type")
    return size, latest


def candidate(config, row, cutoff):
    item = dict(id=row["id"], state=row["state"], eligible=False, bytes=0, deleted=False)
    if row["state"] not in TERMINAL:
        return dict(item, reason="TASK_NOT_TERMINAL")
    try:
        body = row.get('body_state')
        if body and body not in ('NORMAL','LEGACY'):
            return dict(item, reason='BODY_DELETED' if body=='DELETED' else 'BODY_PURGE_PENDING')
        if body:
            from .records_guard import require_artifacts, RecordsError
            try:
                require_artifacts(config,row['id'],write=True,live=False)
            except RecordsError as error:
                return dict(item,reason=error.code)
        home = safe(config["root"], (config["state"] / "workspaces" / identifier(row["id"])).relative_to(config["root"]).as_posix())
        item["path"] = str(home)
        # Path.is_junction is unavailable on Python 3.11. Check every ancestor too.
        ancestor = home
        while ancestor != config["root"]:
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    raise ValueError("Links/reparse points prevent cleanup")
            ancestor = ancestor.parent
        if not home.exists():
            return dict(item, reason="ARTIFACTS_MISSING")
        if not home.is_dir():
            raise ValueError("Task artifact root must be a directory")
        size, latest = inspect_tree(home)
        item["bytes"] = size
        finished = row["finished"]
        if not isinstance(finished, (int, float)) or not math.isfinite(finished) or finished <= 0:
            raise ValueError("Invalid completion timestamp")
        latest = max(latest, finished)
        for backup in home.iterdir():
            if not backup.name.startswith("backup-"):
                continue
            if not backup.is_dir():
                raise ValueError("Invalid backup directory")
            journal = json.loads((backup / "journal.json").read_text(encoding="utf-8"))
            # APPLIED still offers rollback of a live change: never expire that backup.
            if not isinstance(journal, dict) or journal.get("state") != "ROLLED_BACK":
                return dict(item, reason="RECOVERY_DATA_PROTECTED")
            if not isinstance(journal.get("changes"), list) or not journal["changes"]:
                raise ValueError("Invalid recovery journal")
            seen = set()
            for change in journal["changes"]:
                if not isinstance(change, dict) or not all(k in change for k in ("path", "before", "after")):
                    raise ValueError("Invalid recovery change")
                path = safe(backup, change["path"])
                if change["path"] in seen:
                    raise ValueError("Duplicate recovery path")
                seen.add(change["path"])
                for key in ("before", "after"):
                    value = change[key]
                    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
                        raise ValueError("Invalid recovery hash")
                if digest(path) != change["before"]:
                    raise ValueError("Damaged recovery backup")
            if "updated" in journal:
                updated = journal["updated"]
                if type(updated) not in (int, float) or not math.isfinite(updated) or updated <= 0:
                    raise ValueError("Invalid recovery timestamp")
                latest = max(latest, updated)
        item["latest_activity"] = latest
        if latest >= cutoff:
            return dict(item, reason="RETENTION_PERIOD")
        return dict(item, eligible=True, reason="ELIGIBLE")
    except (OSError, ValueError, TypeError) as exc:
        return dict(item, reason="UNSAFE_OR_INCOMPLETE", error=str(exc))


def read_batch_rows(config, ids=None, *, after='', limit=25):
    """Read ordinary tasks only; never initialize or migrate a database."""
    path = checked_local_path(config['state'] / 'queue.sqlite3')
    if not path.exists():
        if ids:
            raise ValueError('Unknown task IDs')
        return []
    db = readonly_database(path)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sql = 'SELECT t.*'
        sql += ',a.state AS cleanup_state' if 'artifact_cleanup' in tables else ',NULL AS cleanup_state'
        sql += ' FROM tasks t'
        if 'artifact_cleanup' in tables:
            sql += ' LEFT JOIN artifact_cleanup a ON a.id=t.id'
        sql += ' WHERE 1=1'
        if 'role_tasks' in tables:
            sql += ' AND NOT EXISTS (SELECT 1 FROM role_tasks r WHERE r.id=t.id)'
        params = []
        if ids is not None:
            sql += ' AND t.id IN (' + ','.join('?' for _ in ids) + ')'
            params.extend(ids)
        else:
            sql += ' AND t.id>?'
            params.append(after)
        sql += ' ORDER BY t.id'
        if ids is None:
            sql += ' LIMIT ?'
            params.append(limit)
        rows = [dict(row) for row in db.execute(sql, params)]
        from .records_guard import decorate
        for row in rows:
            decorate(db,row)
        if ids is not None and set(ids) != {row['id'] for row in rows}:
            raise ValueError('Unknown task or role task selected as an ordinary task')
        return rows
    finally:
        db.close()


def copy_guard(config, row):
    """Freeze every artifact, including hidden files and empty directories."""
    if not {'id', 'batch', 'endpoint', 'prompt', 'state', 'created', 'started', 'finished', 'result'} <= row.keys():
        raise ValueError('Unsupported task schema; do not initialize it during cleanup')
    home = checked_local_path(config['state'] / 'workspaces' / identifier(row['id']), config['root'])
    if not home.is_dir():
        raise ValueError('Artifacts are missing')
    result = json.loads(row.get('result') or '{}')
    if not isinstance(result, dict) or result.get('workspace') is not None and Path(result['workspace']) != home:
        raise ValueError('Recorded workspace differs from the selected task')
    digest_ = hashlib.sha256(json.dumps(dict(row=row, root=str(config['root']), state=str(config['state'])),
                                        sort_keys=True, default=str, allow_nan=False).encode())
    pending = [home]
    device = home.stat().st_dev
    while pending:
        path = checked_local_path(pending.pop())
        if path != home and not path.is_relative_to(home):
            raise ValueError('Artifact path escaped its selected root')
        before = path.stat()
        if before.st_dev != device:
            raise ValueError('Artifact mount boundary')
        info = (path.relative_to(home).as_posix(), before.st_dev, before.st_ino,
                before.st_mode, before.st_size, before.st_mtime_ns)
        digest_.update(json.dumps(info, separators=(',', ':')).encode())
        if stat.S_ISDIR(before.st_mode):
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(before.st_mode):
            content = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    content.update(chunk)
            digest_.update(content.digest())
        else:
            raise ValueError('Unsupported artifact type')
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('Artifacts changed while observing')
    return digest_.hexdigest()


def observe_batch(config, days=30, ids=None, *, after='', limit=25, freeze=False, cutoff=None):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('days must be 1..3650')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be 1..50')
    if after:
        identifier(after)
    if ids is not None:
        if not ids or len(ids) > 25 or len(set(ids)) != len(ids):
            raise ValueError('Select 1..25 unique task IDs')
        for task in ids:
            identifier(task)
    cutoff = time.time() - days * 86400 if cutoff is None else cutoff
    rows = read_batch_rows(config, ids, after=after, limit=limit + 1)
    more = ids is None and len(rows) > limit
    if ids is None:
        rows = rows[:limit]
    locks = [probe_lock(checked_local_path(config['state'] / name)) for name in ('runner.lock', 'promotion.lock')]
    items = []
    for row in rows:
        artifact = candidate(config, row, cutoff)
        artifact['cleanup_state'] = row['cleanup_state']
        if artifact['reason'] == 'ARTIFACTS_MISSING' and row['cleanup_state'] == 'DELETED':
            artifact['reason'] = 'ALREADY_DELETED'
        if 'LOCKED' in locks:
            artifact.update(eligible=False, reason='RUNNER_OR_MANAGEMENT_ACTIVE')
        item = dict(kind='batch', task_id=row['id'], state=row['state'], artifact=artifact, archive='NOT_APPLICABLE')
        if freeze and artifact['eligible']:
            try:
                item['guard'] = copy_guard(config, row)
            except (OSError, ValueError, TypeError):
                artifact.update(eligible=False, reason='UNSAFE_OR_CHANGED')
        artifact.pop('error', None)
        items.append(item)
    return dict(kind='batch', days=days, cutoff=cutoff, items=items, more=more,
                next_cursor=rows[-1]['id'] if more else None, scope='ordinary_task_copies',
                read_only=True, eligible=bool(items) and all(i['artifact']['eligible'] for i in items))


def delete_artifacts(store, row, home, item, *, preserve=None):
    """Delete an already validated copy and retain partial or uncertain outcomes."""
    try:
        store.mark_artifacts(row['id'], 'DELETING')
    except (OSError, sqlite3.Error):
        item.update(reason='CLEANUP_RECORD_FAILED')
        return
    try:
        (preserve or preserve_before_prune)(store, row, home)
        shutil.rmtree(home)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        try:
            store.mark_artifacts(row['id'], 'DELETE_FAILED')
        except (OSError, sqlite3.Error):
            pass
        item.update(reason='DELETE_FAILED', error=str(exc))
    else:
        try:
            store.mark_artifacts(row['id'], 'DELETED')
        except (OSError, sqlite3.Error):
            item.update(deleted=None, reason='DELETE_UNCONFIRMED')
        else:
            item.update(deleted=True, reason='DELETED')


def prune(config, days=30, ids=None, apply=False, *, expected=None, cutoff=None):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("days must be 1..3650")
    if ids is not None:
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Select unique task IDs")
        for id_ in ids:
            identifier(id_)
    if apply and not ids:
        raise ValueError("Deletion requires explicit --ids from a reviewed preview")
    if expected is not None and (not apply or not ids or set(expected) != set(ids)):
        raise ValueError('A cleanup plan must cover exactly the selected tasks')
    store = Store(config["state"], config=config) if expected is None else None
    # Stop the runner first, even if paused. Serialize against apply/recover too.
    with FileLock(config["state"] / "runner.lock"), FileLock(config["state"] / "promotion.lock"):
        rows = store.rows() if store else read_batch_rows(config, ids)
        if ids is not None:
            if set(ids) - {row["id"] for row in rows}:
                raise ValueError("Unknown task ID")
            rows = [row for row in rows if row["id"] in ids]
        cutoff = time.time() - days * 86400 if cutoff is None else cutoff
        items = [candidate(config, row, cutoff) for row in rows]
        if expected is not None:
            for row, item in zip(rows, items):
                if not item['eligible'] or copy_guard(config, row) != expected[row['id']]:
                    raise ValueError('Artifacts changed after cleanup preview')
            store = Store(config['state'], config=config)
        if apply:
            for row, item in zip(rows, items):
                if not item["eligible"]:
                    continue
                if expected is not None:
                    try:
                        fresh = read_batch_rows(config, [row['id']])[0]
                        unchanged = copy_guard(config, fresh) == expected[row['id']]
                    except (OSError, ValueError, TypeError, sqlite3.Error):
                        unchanged = False
                    if not unchanged:
                        item.update(eligible=False, reason='PLAN_CHANGED')
                        continue
                # Recheck immediately before mutation, under both locks.
                checked = candidate(config, row, cutoff)
                item.update(checked)
                if not item["eligible"]:
                    continue
                home = Path(item["path"])
                # Only a validated absolute state/workspaces/<id> tree reaches rmtree.
                if home != config["state"] / "workspaces" / row["id"]:
                    raise ValueError("Artifact path changed")
                delete_artifacts(store, row, home, item)
        return dict(apply=apply, days=days, tasks=items,
                    eligible_bytes=sum(item["bytes"] for item in items if item["eligible"]),
                    deleted_bytes=None if any(item['deleted'] is None for item in items) else sum(item["bytes"] for item in items if item["deleted"]))
