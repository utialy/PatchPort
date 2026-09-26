"""Explicit allowlisted copies and journaled, reviewed promotion."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from .storage import atomic_json, identifier, FileLock

EXCLUDED = {".git", ".venv", "node_modules", ".agent-bridge", ".bridge", "__pycache__"}

def safe(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if not relative or rel.is_absolute() or ".." in rel.parts or ":" in str(relative) or "\\" in str(relative):
        raise ValueError("Use relative POSIX paths without traversal")
    path = root / rel
    walk = path
    while walk != root:
        if walk.is_symlink() or (hasattr(walk, "is_junction") and walk.is_junction()):
            raise ValueError("Links/junctions are not supported")
        walk = walk.parent
    path.resolve().relative_to(root)
    if path == root: raise ValueError("Root is not a file")
    return path

def digest(path):
    path = Path(path)
    if not path.exists(): return None
    if not path.is_file(): raise ValueError("Expected a regular file")
    with path.open("rb") as f: return hashlib.file_digest(f, "sha256").hexdigest()

def files(root):
    root = Path(root)
    result = {}
    def visit(directory):
        for path in directory.iterdir():
            if path.name in EXCLUDED: continue
            rel = path.relative_to(root).as_posix()
            safe(root, rel)
            if path.is_dir(): visit(path)
            elif path.is_file(): result[rel] = digest(path)
            else: raise ValueError("Unsupported file type")
    visit(root)
    return result

def create(config, id_, selected=None):
    home = config["state"] / "workspaces" / identifier(id_)
    home.mkdir(parents=True, exist_ok=False)
    project = home / "project"
    project.mkdir()
    for rel in config["include"] if selected is None else selected:
        source = safe(config["root"], rel)
        dest = safe(project, rel)
        if not source.exists(): continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for child in files(source):
                target = safe(dest, child)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(safe(source, child), target)
        else: shutil.copy2(source, dest)
    atomic_json(home / "baseline.json", files(project))
    return home

def changes(config, home):
    before = json.loads((home / "baseline.json").read_text(encoding="utf-8"))
    after = files(home / "project")
    result = []
    for rel in sorted(before.keys() | after.keys()):
        if before.get(rel) != after.get(rel):
            allowed = any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in config["writable"])
            result.append(dict(path=rel, before=before.get(rel), after=after.get(rel), allowed=allowed))
    atomic_json(home / "changes.json", result)
    return result

def replace(source, dest, expected):
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = dest.with_name(".bridge-stage-" + uuid.uuid4().hex)
    try:
        with source.open("rb") as src, stage.open("xb") as out:
            shutil.copyfileobj(src, out); out.flush(); os.fsync(out.fileno())
        if digest(stage) != expected: raise ValueError("Source hash changed")
        shutil.copymode(source, stage)
        os.replace(stage, dest)
    finally: stage.unlink(missing_ok=True)

def review(config, id_, selected=None):
    home = config["state"] / "workspaces" / identifier(id_)
    manifest = json.loads((home / "changes.json").read_text(encoding="utf-8"))
    if selected is None: return manifest
    if not selected or len(set(selected)) != len(selected): raise ValueError("Select unique reviewed paths")
    with FileLock(config["state"] / "promotion.lock"):
        plan = []
        for rel in selected:
            matches = [c for c in manifest if c["path"] == rel and c["allowed"]]
            if len(matches) != 1: raise ValueError("Not an allowed manifest path")
            c = matches[0]
            if not any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in config["writable"]): raise ValueError("Not writable")
            if digest(safe(config["root"], rel)) != c["before"]: raise ValueError("Original changed")
            if digest(safe(home / "project", rel)) != c["after"]: raise ValueError("Proposal changed")
            plan.append(c)
        backup = home / ("backup-" + uuid.uuid4().hex)
        backup.mkdir()
        for c in plan:
            if c["before"]: replace(safe(config["root"], c["path"]), safe(backup, c["path"]), c["before"])
        journal = dict(state="APPLYING", changes=plan, updated=time.time())
        atomic_json(backup / "journal.json", journal)
        # On failure retain APPLYING. Explicit recover handles the known before/after states.
        for c in plan:
            live = safe(config["root"], c["path"])
            if digest(live) != c["before"]: raise ValueError("Original changed during promotion")
            if c["after"]: replace(safe(home / "project", c["path"]), live, c["after"])
            else: live.unlink()
        journal["state"] = "APPLIED"
        journal["updated"] = time.time()
        atomic_json(backup / "journal.json", journal)
        return {"backup": backup.name, "state": "APPLIED"}

def recover(config, id_, backup_name, apply=False):
    identifier(backup_name)
    if not backup_name.startswith("backup-"): raise ValueError("Invalid backup")
    home = config["state"] / "workspaces" / identifier(id_)
    backup = safe(home, backup_name)
    with FileLock(config["state"] / "promotion.lock"):
        journal = json.loads((backup / "journal.json").read_text(encoding="utf-8"))
        for c in journal["changes"]:
            if digest(safe(backup, c["path"])) != c["before"]: raise ValueError("Damaged backup")
            if digest(safe(config["root"], c["path"])) not in (c["before"], c["after"]): raise ValueError("Later edit conflict")
        if apply:
            journal["state"] = "RECOVERING"; journal["updated"] = time.time(); atomic_json(backup / "journal.json", journal)
            for c in journal["changes"]:
                live = safe(config["root"], c["path"])
                if digest(live) not in (c["before"], c["after"]): raise ValueError("Later edit conflict")
                if c["before"]: replace(safe(backup, c["path"]), live, c["before"])
                else: live.unlink(missing_ok=True)
            journal["state"] = "ROLLED_BACK"; journal["updated"] = time.time(); atomic_json(backup / "journal.json", journal)
        return journal
