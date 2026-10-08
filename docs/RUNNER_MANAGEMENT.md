# Runner management

Submission and execution are separate. A runner owns one state directory's
queue and atomically claims work under the existing pause/lifetime call cap.
Inspection does not create a queue or start a provider.

## Observe, preview and start

Place --config before the command when the configuration is not bridge.json:

```sh
python -m agent_bridge --config /path/to/project/bridge.json runner status
python -m agent_bridge --config /path/to/project/bridge.json runner start
python -m agent_bridge --config /path/to/project/bridge.json runner start --apply
```

Without --apply, start and stop are previews. Explicit start may execute pending
tasks. It reuses a confirmed compatible runner instead of creating a duplicate.
Unknown launch receipts, mismatched runtime/configuration, stale/unmanaged
ownership or a busy state require inspection instead of another automatic start.

## Drain or cancel

Read the current run ID from status. Stop targets that exact ID:

```sh
python -m agent_bridge --config /path/to/project/bridge.json runner stop --run-id RUN_ID
python -m agent_bridge --config /path/to/project/bridge.json runner stop --run-id RUN_ID --apply
python -m agent_bridge --config /path/to/project/bridge.json runner stop --run-id RUN_ID --cancel-active --apply
```

Drain stops new claims and waits for active tasks. Cancel requests interruption
through the owned process supervisor. A bounded response may report STOP_PENDING;
inspect the same run ID instead of treating a timeout as a retry request.

Pause affects new claims, not already-running work. Lifetime claims remain consumed
after failures, interruption, stop and restart. Do not delete the queue, launch
receipt or recovery evidence to reset a limit or hide an unconfirmed outcome.

## VS Code and optional services

The [VS Code guide](VSCODE_GUIDE.md) explains Start queue runner, Drain & stop runner,
Cancel active work & stop, and batch controls. These are distinct from terminal-card
stop and route pause. Closing a management window/core connection does not stop a
confirmed queue runner.

An explicitly registered user service uses its backend through the same identity
checks. Registration/start/login auto-start remain separate choices. See
[service sources](../packaging/services/README.md) for systemd and the experimental
macOS LaunchAgent backend. Native macOS login/lifetime checks remain outstanding;
WSL/systemd and simulated LaunchAgent checks are not substitutes.
