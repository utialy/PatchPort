"""Provider-reported usage snapshots; unknown values are never invented as zero."""
from collections import Counter
from datetime import datetime, timezone
import json
import math
import time

METRICS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd", "elapsed_seconds")


def number(value, integer=False):
    if type(value) not in (int, float) or value < 0:
        return None
    if integer and (type(value) is not int or value > 2**63-1):
        return None
    try:
        if not math.isfinite(value): return None
    except OverflowError:
        return None
    return value


def snapshot(adapter, event=None, requested_model=None):
    event = event if isinstance(event, dict) else {}
    raw = event.get("usage")
    raw = raw if isinstance(raw, dict) else {}
    result = dict(adapter=adapter, requested_model=requested_model,
                  reported_model="UNKNOWN", source="UNKNOWN",
                  **{key: None for key in METRICS if key != "elapsed_seconds"})
    if adapter not in ("claude", "codex"):
        return result
    if adapter == "claude" and event.get("subtype") == "error_during_execution":
        return result  # Crash results may contain zeroed, unreliable totals.
    result["output_tokens"] = number(raw.get("output_tokens"), True)
    if adapter == "claude":
        uncached = number(raw.get("input_tokens"), True)
        read = number(raw.get("cache_read_input_tokens"), True)
        write = number(raw.get("cache_creation_input_tokens"), True)
        result.update(cache_read_tokens=read, cache_write_tokens=write,
                      input_tokens=sum((uncached, read, write)) if all(v is not None for v in (uncached, read, write)) else None,
                      cost_usd=number(event.get("total_cost_usd")))
        models = event.get("modelUsage")
        if isinstance(models, dict) and models:
            result["reported_model"] = next(iter(models)) if len(models) == 1 else "MULTIPLE"
            result["model_usage"] = {}
            for model, values in models.items():
                values = values if isinstance(values, dict) else {}
                model_event = dict(usage={"input_tokens":values.get("inputTokens"),
                                         "output_tokens":values.get("outputTokens"),
                                         "cache_read_input_tokens":values.get("cacheReadInputTokens"),
                                         "cache_creation_input_tokens":values.get("cacheCreationInputTokens")},
                                   total_cost_usd=values.get("costUSD"))
                result["model_usage"][model] = snapshot("claude", model_event)
            for metric in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                parts = [value[metric] for value in result["model_usage"].values()]
                result[metric] = sum(parts) if all(value is not None for value in parts) else None
    else:
        # Codex input_tokens includes cached_input_tokens; do not add them twice.
        result.update(input_tokens=number(raw.get("input_tokens"), True),
                      cache_read_tokens=number(raw.get("cached_input_tokens"), True))
    if any(result[key] is not None for key in METRICS if key != "elapsed_seconds"):
        result["source"] = "PROVIDER_REPORTED"
    return result


def totals(rows):
    result = {}
    for metric in METRICS:
        values = [row[metric] for row in rows if row[metric] is not None]
        subtotal = sum(values)
        overflow = isinstance(subtotal, float) and not math.isfinite(subtotal)
        result[metric] = dict(known_sum=subtotal if values and not overflow else None,
                              unknown_tasks=len(rows)-len(values),
                              complete=len(values) == len(rows) and not overflow)
    return dict(tasks=len(rows), states=dict(Counter(row["state"] for row in rows)), metrics=result)


def report(store, days=None, endpoint=None, batch=None, group_by="endpoint"):
    if days is not None and (type(days) is not int or not 1 <= days <= 3650):
        raise ValueError("days must be 1..3650")
    if group_by not in ("endpoint", "model", "batch", "day"):
        raise ValueError("Invalid usage grouping")
    cutoff = time.time() - days * 86400 if days is not None else None
    records = []
    # Read tasks and snapshots in one SQLite snapshot while the runner is writing.
    with store.connect() as db:
        db.execute("BEGIN")
        has_usage = db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_snapshots'").fetchone()
        query = ("SELECT t.*,u.data AS usage_data FROM tasks t LEFT JOIN usage_snapshots u ON t.id=u.id"
                 if has_usage else "SELECT t.*,NULL AS usage_data FROM tasks t")
        rows = list(db.execute(query + " WHERE t.started IS NOT NULL ORDER BY t.created,t.id"))
    for row in rows:
        if endpoint is not None and row["endpoint"] != endpoint: continue
        if batch is not None and row["batch"] != batch: continue
        if cutoff is not None and row["started"] < cutoff: continue
        if row["usage_data"]:
            usage = json.loads(row["usage_data"])
        else:
            # Older results retain terminal provider JSON even after artifact pruning.
            try: previous = json.loads(row["result"]) if row["result"] else {}
            except (TypeError, ValueError): previous = {}
            event = previous.get("provider_result") if isinstance(previous, dict) else None
            event = event if isinstance(event, dict) else {}
            adapter = "claude" if event.get("type") == "result" else "codex" if event.get("type") == "turn.completed" else "unknown"
            usage = snapshot(adapter, event)
        elapsed = number(row["finished"]-row["started"]) if row["finished"] is not None else None
        records.append(dict(usage, id=row["id"], endpoint=row["endpoint"], batch=row["batch"],
                            state="RUNNING" if row["state"] in ("PLAN_RUNNING", "ROLE_RUNNING", "SUMMARY_RUNNING") else row["state"], day=datetime.fromtimestamp(row["started"],timezone.utc).date().isoformat(),
                            elapsed_seconds=elapsed))
    groups = {}
    field = "reported_model" if group_by == "model" else group_by
    for row in records:
        if group_by == "model" and row.get("model_usage"):
            for model, values in row["model_usage"].items():
                groups.setdefault(model, []).append(dict(row, **{metric:values.get(metric) for metric in METRICS}))
        else:
            groups.setdefault(row[field], []).append(row)
    return dict(group_by=group_by, timezone="UTC", period_basis="task_started",
                unknown_representation="null", cost_basis="provider-reported USD, not subscription bill or remaining quota",
                summary=totals(records), groups={key:totals(value) for key,value in sorted(groups.items())}, tasks=records)
