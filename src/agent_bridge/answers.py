"""Durable answer bodies and concise views without model calls."""
import json
from . import workspace


def capture(home, state):
    path = workspace.safe(home, "answer.txt")
    if not path.exists():
        return {"text": None, "status": "UNAVAILABLE"}
    return {"text": path.read_text(encoding="utf-8"),
            "status": "COMPLETE" if state == "DONE" else "PARTIAL"}


def answer_for(result):
    result = result or {}
    if isinstance(result.get("answer"), dict):
        return result["answer"]
    provider = result.get("provider_result")
    if isinstance(provider, dict) and isinstance(provider.get("result"), str):
        return {"text": provider["result"], "status": "LEGACY_REPORTED"}
    return {"text": None, "status": "UNAVAILABLE"}


def preserve_before_prune(store, row, home):
    """Keep legacy answer files before deleting artifacts, without changing timestamps."""
    with store.connect() as db:
        current = db.execute("SELECT result FROM tasks WHERE id=?", (row["id"],)).fetchone()
        result = json.loads(current["result"]) if current["result"] else {}
        if answer_for(result).get("text") is not None:
            return
        captured = capture(home, row["state"])
        if captured["text"] is not None:
            result["answer"] = captured
            db.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), row["id"]))


def concise(row):
    result = row.get("result") or {}
    return dict(id=row["id"], endpoint=row["endpoint"], state=row["state"],
                task_success=row["task_success"], answer=answer_for(result),
                error=result.get("error") or result.get("manifest_error"),
                exit_code=result.get("exit_code"),
                changes=[{key: item.get(key) for key in ("path", "allowed")}
                         for item in result.get("changes", [])],
                artifact_cleanup=row["artifact_cleanup"])
