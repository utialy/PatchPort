"""Explicit, retention-based removal of terminal task artifacts, never queue rows."""
import json
import math
import re
from pathlib import Path
import shutil
import stat
import time

from .storage import FileLock, Store, identifier
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


def prune(config, days=30, ids=None, apply=False):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("days must be 1..3650")
    if ids is not None:
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Select unique task IDs")
        for id_ in ids:
            identifier(id_)
    if apply and not ids:
        raise ValueError("Deletion requires explicit --ids from a reviewed preview")
    store = Store(config["state"])
    # Stop the runner first, even if paused. Serialize against apply/recover too.
    with FileLock(config["state"] / "runner.lock"), FileLock(config["state"] / "promotion.lock"):
        rows = store.rows()
        if ids is not None:
            if set(ids) - {row["id"] for row in rows}:
                raise ValueError("Unknown task ID")
            rows = [row for row in rows if row["id"] in ids]
        cutoff = time.time() - days * 86400
        items = [candidate(config, row, cutoff) for row in rows]
        if apply:
            for row, item in zip(rows, items):
                if not item["eligible"]:
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
                store.mark_artifacts(row["id"], "DELETING")
                try:
                    preserve_before_prune(store, row, home)
                    shutil.rmtree(home)
                except (OSError, ValueError, TypeError) as exc:
                    store.mark_artifacts(row["id"], "DELETE_FAILED")
                    item.update(reason="DELETE_FAILED", error=str(exc))
                else:
                    store.mark_artifacts(row["id"], "DELETED")
                    item.update(deleted=True, reason="DELETED")
        return dict(apply=apply, days=days, tasks=items,
                    eligible_bytes=sum(item["bytes"] for item in items if item["eligible"]),
                    deleted_bytes=sum(item["bytes"] for item in items if item["deleted"]))
