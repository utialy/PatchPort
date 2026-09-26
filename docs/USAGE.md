# Usage and answers

```sh
agent-bridge usage --days 7 --group-by endpoint
agent-bridge usage --endpoint codex --batch review-001 --group-by model
```

Groups are endpoint, model, batch, or UTC day. The period uses task start time. Queued tasks are excluded; started, failed, interrupted, and running tasks are included.

Each metric has known_sum, unknown_tasks, and complete. Missing values are null, not zero. The recorded amount is provider-reported USD, not a subscription bill, remaining quota, or price-table estimate.

Codex input tokens already include cache reads. Claude input, cache-read, and cache-write categories are combined into the normalized input total. Model-level statistics take precedence when present. An unreported actual model remains UNKNOWN; the requested model or alias is recorded separately.

One cumulative usage snapshot is stored per task. Repeated final events replace rather than add to it. A crash before the final report can leave actual usage unrecorded. Claude error events containing placeholder zero statistics do not overwrite a valid previous report. Multimodel task counts cannot be summed to derive the total number of tasks.

## Preserved answers

Results preserve answer text in SQLite. COMPLETE is a saved answer from a DONE task, PARTIAL is output from another terminal state, UNAVAILABLE means no preserved body, and LEGACY_REPORTED identifies older provider-result text. Empty text and missing text differ. Text reading may normalize line endings; the DB answer is not an exact byte archive.

Before deleting a legacy workspace, cleanup attempts to preserve any answer still present only in the file. Failure blocks deletion. Already missing output cannot be reconstructed. Pruning keeps usage and answers in the database, so it does not bound database size.

`task_success=NOT_EVALUATED` is independent of answer status. Inspect the requested behavior, actual changes, and tests before treating work as correct.
