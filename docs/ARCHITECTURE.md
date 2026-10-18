# Architecture

## VS Code and the management core

The extension owns interactive PTYs, a Webview/xterm board, directed routes and
provider adapters. Direct terminals use the original project; batch/role workflows
use selected copies. A bundled standard-library Python core exposes sequential
versioned JSON-lines management requests. Read-only inspection creates no queue,
runner or AI. Submission, runner start and promotion are separate explicit actions.

One-use previews freeze input and recheck configuration, provenance, original/
proposal hashes and allowed paths. The extension checks unsaved editors and
invalidates in-flight previews when input changes. Unconfirmed mutations are not
retried automatically. interactive_* tables retain managed session/event/delivery/
usage separately from batch claims. Delivery is recorded before PTY input.

Role jobs consume durable one-use launch receipts and share the existing parent
queue/runner contract. Fixed helpers perform development, host checks, independent
read-only review, explicit promotion/recovery and deletion-free archive operations.
Windows launcher PID and actual Python job PID are distinguished.

An accepted child waits up to 15 seconds for the parent's start-lock handoff
before reading its launch receipt, then verifies the one-use nonce/configuration.
This coordinates one already launched process; it never relaunches a provider or
failed workflow. A real-process regression forces lock contention before the
parent publishes acceptance and verifies completion/cleanup with the same ID.

Atomic workspace snapshots preserve board/config, paused groups, round/counts and
history with corruption/other-writer protection. Restart neither starts an AI nor
resends uncertain input. Exact recorded provider UUID resume and runtime integrity
checks are explicit. See the [walkthrough](VSCODE_GUIDE.md) and
[runner guide](RUNNER_MANAGEMENT.md).

## Runtime

| Module | Responsibility |
| --- | --- |
| cli.py | Configuration, submission, execution, result queries, and explicit artifact operations |
| storage.py | SQLite transactions, atomic claims, file locks, and atomic JSON replacement |
| runner.py | Concurrency limits, adapter commands, events, and final results |
| child.py | Process ownership and cleanup: Windows Job Objects and POSIX process groups |
| workspace.py | Selected input copies, change hashes, promotion, backup journals, and recovery |
| context.py | Input inventories, budgets, required files, and frozen full/omit plans |
| summaries.py | Reviewed reference summaries, provenance validation, and exact prompt rendering |
| connect.py / role_policy.py | Local integration installation and shared reviewer policy validation |
| setup_project.py | CLI discovery, explicit input selection, setup previews, and interactive confirmation |
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

Setup dispatches before queue initialization and builds on the connector's installation plan. Generated configuration and starter files remain in memory until explicit application. Missing project roots use a temporary validation root. Setup checks selected input paths and hashes, then rechecks inputs and CLI resolution after the runtime import probe. The connector also guards configuration and manifest hashes and preserves ownership of identical pre-existing user files. Partial writes report backup evidence without automatic retry or deletion. Authentication and provider invocation are not part of setup.

The connect command dispatches before configuration/queue initialization. Its default is a read-only preview; explicit application preserves conflicts and backups. setup.py bundles only the reviewed helper, template, and peer-consult files inside the package. Installed code reads those assets without finding another checkout. The source checkout uses its fixed source layout.

Summary plans use schema=2 and SUMMARY_QUEUED/SUMMARY_RUNNING states so older runners cannot claim or recover them. The new Store normalizes these for callers. Summary bodies and provenance are saved in the submission transaction. Execution rechecks source, metadata, configuration, and copied full files before launching a provider. Prompt files preserve the UTF-8 bytes counted by the budget. Role helpers retain full/omit plans; they do not submit summary plans. See [summary inputs](CONTEXT_SUMMARIES.md).

The tools/ helpers connect another project to the installed runtime. role_flow runs development, host tests, and independent review in order. New roles share the parent queue and its pause/call cap. Historical private queues remain readable and are not migrated automatically.

The flow lock covers host checks as well as provider stages. flow_cleanup builds a read-only plan using existing-lock probes and stable rollback-mode SQLite images deserialized into memory. Archive writes acquire the same flow/runner/promotion locks and bind progress to the original inventory and queue/configuration fingerprints. Explicit continuation fills missing evidence or finishes publication; it never reruns providers or deletes live artifacts. See [archive operations](ARCHIVES.md).

role_review and project_overview provide read-only observations. role_manage performs explicitly selected promotion, recovery, or role-workspace cleanup. flow_archive reads verified historical evidence; it does not create archives or delete flows.

Files and multiple databases do not form a globally atomic snapshot. A heartbeat or lock observation does not prove provider responsiveness. A working copy does not replace the provider sandbox.

## Project data and cleanup

Data inspection reads bounded metadata without creating state. Workspace
configuration changes use atomic snapshots and explicit mirror finalization.
Selected-copy cleanup preserves answers, claim counts and recovery evidence.
Conversation writers hold verified leases; body deletion requires confirmed exit
and explicit ledger preparation. Historical or uncertain writers are not adopted.

Protected task/flow records fence older writers. Body deletion keeps IDs, usage,
limits and deduplication metadata, while file/DB partial work has durable receipts.
Connection removal publishes execution protection before deleting selected files,
keeps settings last, and remains queryable/resumable without the configuration.
Explicit reconnection does not restore files or start providers.

Guided cleanup uses a separate body-free project-cleanup.sqlite3 coordinator.
Each conversation/task/flow/connection stage is previewed and applied separately.
Persisted child IDs resolve response loss before another action. Partial I/O
resumes the same child operation; receipt-only finalization never repeats deletion.
Residual counts retain unknown values and distinguish preserved controls/backups
from forensic erasure. See [data management](PROJECT_DATA.md).

Receipt queries validate links and canonicalize selected project/database paths
before matching stored ownership, including Windows 8.3 aliases. A managed PTY
freezes the selected physical cwd at start and accepts its reported canonical or
original path without reading arbitrary provider-supplied locations. Another cwd
still stops automatic delivery.

The public product is the VS Code extension. The Python package and fixed helpers
are its internal backend/build inputs. Standalone desktop applications and OS
installers are not included. Service registration detection remains in the backend
to preserve ownership and stop removal/execution from bypassing existing services.

## Native platform packaging

platform.ts chooses supported local OS/CPU pairs and default shells/interpreters.
POSIX paths retain case. Managed usage compares the frozen physical cwd; only
the fixed macOS /var and /tmp aliases may occur in otherwise link-free usage paths.
Remote extension hosts remain outside this contract.

Native packaging accepts only the current host target, stages the pinned
node-pty assets and records their hashes. Windows uses seven native files,
Linux one pty.node, and macOS pty.node plus an executable spawn-helper. The VSIX
verifier checks target, binary CPU, required assets, executable mode and hashes.
Host smoke installs the package into an isolated profile, loads its PTY through
VS Code's actual Electron runtime, activates the installed extension and opens
the board. POSIX profiles use short temporary paths for UNIX socket limits.
Windows-only task/role-body and connection-file removal guards are preserved.
