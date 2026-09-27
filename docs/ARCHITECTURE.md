# Architecture

## Runtime

| Module | Responsibility |
| --- | --- |
| cli.py | Configuration, submission, execution, result queries, and explicit artifact operations |
| storage.py | SQLite transactions, atomic claims, file locks, and atomic JSON replacement |
| runner.py | Concurrency limits, adapter commands, events, and final results |
| child.py | Process ownership and cleanup: Windows Job Objects and POSIX process groups |
| workspace.py | Selected input copies, change hashes, promotion, backup journals, and recovery |
| context.py | Input inventories, budgets, required files, and frozen full/omit plans |
| usage.py | Provider-reported usage snapshots and aggregation |
| answers.py | Durable answer bodies and brief result views |
| cleanup.py | Retention and recovery checks before explicit artifact removal |
| health.py | Runner heartbeat and code fingerprint observations |

Submission registers a batch and its targets in one SQLite transaction. A runner owns runner.lock and atomically claims queued tasks. Each task runs in its selected copy. Global and per-endpoint parallelism both apply.

Claude's result event and Codex's completed turn, along with process status and errors, determine adapter completion. Codex's final-message file takes precedence over its agent-message event. Adapter completion does not evaluate task correctness.

The process supervisor owns its child before the provider starts. When the supervising connection closes, descendant cleanup follows the platform implementation. Interrupted work is not automatically retried.

## State and recovery

Queue state, usage, context plans, and answers live in SQLite. Task artifacts live under state/workspaces/TASK_ID. Configuration and selected input paths are validated before work is copied.

Promotion checks original and proposed hashes and the writable policy. Each file is replaced atomically, but multiple files are not one transaction. A journal preserves partial progress. Recovery refuses to overwrite an original that changed after promotion.

The queue's cumulative claim count includes failures after claim. Pause stops new claims, not running tasks. Restarting a runner marks leftover running work interrupted without replaying it.

## Project helpers

The tools/ helpers connect another project to the installed runtime. role_flow runs development, host tests, and independent review in order. New roles share the parent queue and its pause/call cap. Historical private queues remain readable and are not migrated automatically.

The flow lock covers host checks as well as provider stages. flow_cleanup builds a read-only plan using existing-lock probes and stable rollback-mode SQLite images deserialized into memory. Archive writes acquire the same flow/runner/promotion locks and bind progress to the original inventory and queue/configuration fingerprints. Explicit continuation fills missing evidence or finishes publication; it never reruns providers or deletes live artifacts. See [archive operations](ARCHIVES.md).

role_review and project_overview provide read-only observations. role_manage performs explicitly selected promotion, recovery, or role-workspace cleanup. flow_archive reads verified historical evidence; it does not create archives or delete flows.

Files and multiple databases do not form a globally atomic snapshot. A heartbeat or lock observation does not prove provider responsiveness. A working copy does not replace the provider sandbox.
