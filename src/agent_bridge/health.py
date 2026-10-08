"""Observational runner status; never restarts or retries work."""
import hashlib
import json
import math
from pathlib import Path
import re
import time

from . import __version__
from .storage import FileLock, probe_lock

STALE_AFTER_SECONDS = 10


def code_identity(directory=None):
    """Hash package source names and bytes, excluding caches and installation paths."""
    directory = Path(directory) if directory is not None else Path(__file__).parent
    try:
        files = sorted(directory.rglob("*.py"))
        if not files:
            raise OSError("No package source")
        digest = hashlib.sha256()
        for path in files:
            name = path.relative_to(directory).as_posix().encode("utf-8")
            data = path.read_bytes()
            digest.update(len(name).to_bytes(8, "big") + name)
            digest.update(len(data).to_bytes(8, "big") + data)
        fingerprint = digest.hexdigest()
    except OSError:
        fingerprint = None
    return {"version": __version__, "code_sha256": fingerprint}


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def inspect(state, now=None, *, readonly=False):
    state = Path(state)
    if readonly:
        held = probe_lock(state / 'runner.lock') == 'LOCKED'
    else:
        try:
            with FileLock(state / "runner.lock"):
                held = False
        except RuntimeError:
            held = True
    current = code_identity()
    result = {"runner": "LOCKED" if held else "STOPPED", "lock_held": held,
              "health": "UNKNOWN", "heartbeat_status": "UNKNOWN",
              "heartbeat_age_seconds": None, "stale_after_seconds": STALE_AFTER_SECONDS,
              "current_code": current, "restart_required": None}
    try:
        record = json.loads((state / "health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result
    if not isinstance(record, dict):
        return result
    beat = record.get("heartbeat")
    if _number(beat) and beat > 0:
        age = (time.time() if now is None else now) - beat
        result["heartbeat_age_seconds"] = age
        result["heartbeat_status"] = "FUTURE" if age < 0 else "STALE" if age > STALE_AFTER_SECONDS else "FRESH"
    identity = record.get("code")
    valid = (type(record.get("schema")) is int and record["schema"] == 1
             and type(record.get("pid")) is int and record["pid"] > 0
             and type(record.get("active")) is int and record["active"] >= 0
             and _number(record.get("started")) and record["started"] > 0
             and _number(beat) and beat >= record["started"]
             and isinstance(identity, dict) and isinstance(identity.get("version"), str)
             and bool(identity["version"]) and isinstance(identity.get("code_sha256"), str)
             and re.fullmatch(r"[0-9a-f]{64}", identity["code_sha256"]) is not None)
    if valid:
        result["health"] = record
        if held and current["code_sha256"] is not None:
            result["restart_required"] = identity != current
    return result
