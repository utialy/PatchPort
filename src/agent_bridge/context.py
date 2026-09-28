"""Read-only input inventory and optional local input budgets."""
import json
import hashlib
from pathlib import PurePosixPath
from . import workspace

PREFIX = (
    "Work only inside this working copy. Do not modify the original project. "
    "Return your answer in the final message. Do not launch another bridge or install dependencies.\n"
    "The current working directory (.) is the task project copy. Resolve task file paths from ./, "
    "not from original project paths quoted in documents or history. Use the exact relative paths "
    "in the request: a root-level file such as ./regression_tests.py is not ./tests/regression_tests.py. "
    "If a requested file is missing, report its exact path instead of guessing another location.\n\n"
)
LIMITS = {"max_files": "file_count", "max_bytes": "total_bytes", "max_estimated_tokens": "estimated_tokens"}


def validate(limits):
    if not isinstance(limits, dict) or set(limits) - LIMITS.keys():
        raise ValueError("context_budget must contain only max_files, max_bytes, max_estimated_tokens")
    if any(type(value) is not int or not 0 <= value <= 2**63-1 for value in limits.values()):
        raise ValueError("Context limits must be nonnegative integers")


def _path(value):
    if (not isinstance(value, str) or not value or value == "."
            or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or any(part in {"..", *workspace.EXCLUDED} for part in value.split("/"))
            or any(char in value for char in "\\:*?[]")):
        raise ValueError("Context paths must be canonical relative POSIX file paths")
    return value


def validate_required(required):
    if not isinstance(required, list):
        raise ValueError("context_required must be a list of file paths")
    seen = set()
    for value in required:
        value = _path(value)
        if value in seen:
            raise ValueError("Duplicate context_required path: " + value)
        seen.add(value)


def _required(config, items, automatic=False):
    values = config.get("context_required", [])
    validate_required(values)
    candidates = {item["path"]: item for item in items}
    reasons = {}
    for value in values:
        if value not in candidates:
            raise ValueError("Required file missing or outside include: " + value)
        reasons.setdefault(value, []).append("context_required")
    if automatic:
        for value in candidates:
            if PurePosixPath(value).name.casefold() == "agents.md":
                reasons.setdefault(value, []).append("project_rules")
    return [dict(candidates[value], required_by=reasons[value]) for value in sorted(reasons)]


def preview(config, prompt="", copied=None):
    selected = {}
    missing = []
    root = copied if copied is not None else config["root"]
    if copied is not None:
        selected = workspace.files(root)
    else:
        for rel in config["include"]:
            source = workspace.safe(root, rel)
            if not source.exists():
                missing.append(rel)
            elif source.is_dir():
                for child, digest in workspace.files(source).items():
                    selected[source.relative_to(root).as_posix() + "/" + child] = digest
            else:
                selected[source.relative_to(root).as_posix()] = workspace.digest(source)
    items = [dict(path=rel, bytes=workspace.safe(root, rel).stat().st_size, sha256=digest)
             for rel, digest in sorted(selected.items())]
    required = _required(config, items)
    report = _report(config, prompt, items, missing)
    if "context_required" in config:
        report["required"] = required
    return report


def _report(config, prompt, items, missing, *, rendered=None):
    prompt_bytes = len((PREFIX + prompt if rendered is None else rendered).encode("utf-8"))
    total_bytes = sum(item["bytes"] for item in items) + prompt_bytes
    summary = dict(file_count=len(items), file_bytes=total_bytes-prompt_bytes,
                   prompt_bytes=prompt_bytes, total_bytes=total_bytes,
                   estimated_tokens=(total_bytes+3)//4)
    limits = config.get("context_budget", {})
    validate(limits)
    exceeded = [dict(limit=key, maximum=value, observed=summary[LIMITS[key]])
                for key, value in limits.items() if summary[LIMITS[key]] > value]
    return dict(files=items, missing=missing, summary=summary, limits=limits,
                exceeded=exceeded, ok=not exceeded,
                estimate_method="ceil((UTF-8 prompt bytes + all file bytes) / 4); heuristic, not tokenizer output",
                scope="Available local input per task; provider instructions, additional reads and repeated context are not measured")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate context plan JSON key: " + key)
        result[key] = value
    return result


def plan_preview(config, prompt, path=None, *, plan=None):
    """Preview only: no plan is stored or passed to submit/runner."""
    def invalid_constant(value):
        raise ValueError("Invalid JSON constant: " + value)
    if path is not None:
        with open(path, encoding="utf-8-sig") as source:
            plan = json.load(source, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    if isinstance(plan, dict) and plan.get("schema") == 2:
        from .summaries import preview as summary_preview
        return summary_preview(config, prompt, plan)
    if (not isinstance(plan, dict) or set(plan) != {"schema", "files"}
            or type(plan["schema"]) is not int or plan["schema"] != 1
            or not isinstance(plan["files"], list)):
        raise ValueError("Context plan requires schema=1 and files array")
    entries = {}
    for entry in plan["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode", "reason"}:
            raise ValueError("Plan entries require path, mode and reason")
        rel = _path(entry["path"])
        if (entry["mode"] not in ("full", "omit") or not isinstance(entry["reason"], str)
                or not entry["reason"].strip() or rel in entries):
            raise ValueError("Invalid or duplicate plan entry: " + rel)
        entries[rel] = entry
    inventory = preview(config, prompt)
    if inventory["missing"]:
        raise ValueError("Context plan has missing include paths: " + ", ".join(inventory["missing"]))
    items = inventory["files"]
    if set(entries) != {item["path"] for item in items}:
        raise ValueError("Context plan must list every candidate file exactly once")
    # Reject aliases (including hard links) rather than counting one file twice.
    identities = set()
    for item in items:
        _path(item["path"])
        stat = workspace.safe(config["root"], item["path"]).stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in identities:
            raise ValueError("Context candidate paths refer to the same file")
        identities.add(identity)
    required = _required(config, items, automatic=True)
    for item in required:
        if entries[item["path"]]["mode"] != "full":
            raise ValueError("Cannot omit required file: " + item["path"])
    selected, omitted = [], []
    for item in items:
        entry = entries[item["path"]]
        (selected if entry["mode"] == "full" else omitted).append(dict(item, reason=entry["reason"]))
    report = _report(config, prompt, selected, [])
    report.update(scope="preview_only", selected=selected, omitted=omitted, required=required,
                  candidate_count=len(items), selection_applied_to_execution=False)
    return report


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _settings(config):
    return dict(root=str(config["root"]), include=sorted(set(config["include"])),
                writable=sorted(set(config["writable"])),
                context_required=sorted(config.get("context_required", [])),
                context_budget=config.get("context_budget", {}))


def freeze_plan(config, prompt, inventory, targets):
    if inventory.get("plan", {}).get("schema") == 2:
        from .summaries import freeze
        return freeze(config, prompt, inventory, targets)
    plan = dict(schema=1, files=sorted(
        [dict(path=i["path"], mode=mode, reason=i["reason"])
         for mode, group in (("full", "selected"), ("omit", "omitted")) for i in inventory[group]],
        key=lambda i: i["path"]))
    selected = []
    for item in inventory["selected"]:
        # Size and digest come from the same stream; a later copy is checked again.
        digest = hashlib.sha256()
        size = 0
        with workspace.safe(config["root"], item["path"]).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
            raise ValueError("Selected input changed during submission: " + item["path"])
        selected.append(dict(path=item["path"], bytes=size, sha256=digest.hexdigest()))
    payload = dict(schema=1, plan=plan, selected=selected,
                   settings=_settings(config), endpoints={name: config["endpoints"][name] for name in targets},
                   prompt_sha256=hashlib.sha256((PREFIX + prompt).encode("utf-8")).hexdigest())
    return dict(payload=payload, plan_sha256=_hash(payload))


def check_plan(config, prompt, saved, endpoint, copied=None):
    if not isinstance(saved, dict) or set(saved) != {"payload", "plan_sha256"}:
        raise ValueError("Missing or invalid stored context plan")
    payload = saved["payload"]
    if not isinstance(payload, dict) or _hash(payload) != saved["plan_sha256"] or type(payload.get("schema")) is not int or payload.get("schema") not in (1, 2):
        raise ValueError("Stored context plan integrity failure")
    if payload["schema"] == 2:
        from .summaries import check
        return check(config, prompt, saved, endpoint, copied)
    if (payload.get("settings") != _settings(config)
            or payload.get("endpoints", {}).get(endpoint) != config["endpoints"].get(endpoint)
            or payload.get("prompt_sha256") != hashlib.sha256((PREFIX + prompt).encode("utf-8")).hexdigest()):
        raise ValueError("Context plan settings, endpoint or prompt changed")
    if copied is None:
        report = plan_preview(config, prompt, plan=payload["plan"])
        actual = report["selected"]
    else:
        report = preview(config, prompt, copied=copied)
        actual = report["files"]
    actual = [dict(path=i["path"], bytes=i["bytes"], sha256=i["sha256"]) for i in actual]
    if actual != payload["selected"]:
        raise ValueError("Selected context files changed")
    enforce(report)
    report.update(scope="execution_plan", selection_applied_to_execution=True,
                  plan_sha256=saved["plan_sha256"], plan=payload["plan"], validation="VALID")
    return report


def enforce(report):
    if not report["ok"]:
        raise ValueError("Context budget exceeded: " + ", ".join(item["limit"] for item in report["exceeded"]))


def render_prompt(prompt, saved=None):
    if saved is not None and saved["payload"]["schema"] == 2:
        from .summaries import render
        return render(prompt, saved["payload"]["summaries"])
    return PREFIX + prompt
