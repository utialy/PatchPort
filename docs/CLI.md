# Command reference

Place `--config PATH` before the subcommand. Configuration paths are relative to the configuration file's project root. Input and writable entries use relative POSIX paths; a whole-project `.` entry is rejected.

## Project setup

`agent-bridge setup` guides project, CLI, and input selection without editing JSON. `setup --non-interactive --project PATH` previews an existing configuration; add --apply to install it. New configurations also require explicit provider and input or starter selections. See [setup](SETUP.md). Setup and connect use --project, not an alternative global --config, and never start a runner or make a provider request.

## Submission and results

```sh
agent-bridge doctor
agent-bridge submit --id task-001 --targets claude --prompt-file request.md
agent-bridge run --once
agent-bridge status
agent-bridge wait --id task-001 --timeout 600 --brief
agent-bridge result --id task-001 --brief
```

Use `run` without `--once` for a resident runner. Reuse an existing runner for the same state. `wait` does not execute queued tasks. A wait timeout does not cancel or retry them.

`result` and `wait` take a batch ID. `review`, `apply`, and `recover` take the returned task ID, usually BATCH--ENDPOINT. Inspect each target's answer and changes separately. The brief view retains the full preserved answer but omits detailed context plans and provider metadata.

## Execution controls

Background lifecycle commands are separate from foreground `run`:

```sh
agent-bridge runner status
agent-bridge runner start
agent-bridge runner start --apply
agent-bridge runner stop --run-id RUN_ID --apply
agent-bridge runner stop --run-id RUN_ID --cancel-active --apply
```

Without --apply, start/stop only preview. Stop uses the observed exact run ID.
Read the [runner guide](RUNNER_MANAGEMENT.md) for drain/cancel, unknown receipts
and optional service boundaries. The VS Code workflow is described in the
[screenshot walkthrough](VSCODE_GUIDE.md).

```sh
agent-bridge pause
agent-bridge limit --max-calls 10
agent-bridge resume
agent-bridge limit --unlimited
```

The limit counts cumulative claims in one state directory, not provider API requests or tokens. If eight calls have started, a cap of ten allows two more. A failure after claim still consumes one. Zero prevents additional claims. Changing the cap does not cancel running tasks; resume does not reset the count or launch a runner.

A resident runner waits for dispatch to become available. `run --once` finishes active tasks and returns 2 if queued tasks remain blocked by pause or the limit. `status.control` reports paused, max_calls, calls_started, remaining, and dispatch.

## Cleanup

```sh
agent-bridge prune --days 30
agent-bridge prune --days 30 --ids task-001--claude --apply
```

The default is a preview. Deletion requires explicit IDs and a stopped runner; pause alone is insufficient. Retention is 1-3650 days and uses the newest completion, artifact modification, or recovery-journal timestamp.

Only terminal task artifacts are candidates. Applied or incomplete recovery backups, damaged metadata, links, and reparse points block cleanup. Valid rolled-back backups must also meet retention. Deletion is not atomic across tasks. Failures remain visible as DELETE_FAILED or an interrupted DELETING record; there is no automatic retry.

Cleanup removes task files, not task rows, usage, preserved answers, or cumulative counts. It does not compact SQLite. Role copies use the separate [role management](ROLES.md) helper.

## Runner observations

`runner=LOCKED` means a lock was observed, not that the provider is responsive. Health includes the captured runtime version/code fingerprint, PID, and heartbeat. A heartbeat may be FRESH, STALE, FUTURE, or UNKNOWN. `restart_required` is true/false only when the held runner and current runtime can be compared; otherwise it is null. Restart is always explicit.
