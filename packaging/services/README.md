# Optional user services

The Linux workflow below has WSL validation. The separate
[macOS LaunchAgent workflow](MACOS.md) is experimental and has only mocked tests.
The same service CLI and runner controls route through the registered backend.

For Linux `systemd --user`, use a non-root user session
with a working user manager, GNU `/usr/bin/env` supporting `--chdir`, and the
installed current PatchPort runtime. Windows automatic startup is separate work.
Manual runner commands remain available without a service registration.

Registration, immediate start, automatic start, stopping, and unregistering are
independent explicit actions. Installation never registers a service. The unit
contains fixed Python/config/project paths and no credentials. Select absolute
provider executables first; use each official CLI's existing authentication.

## Register without running work

Stop the existing runner, then use the installed runtime that will own the project:

```sh
agent-bridge --config /project/bridge.json service register
agent-bridge --config /project/bridge.json service register --apply --expect-plan TOKEN
agent-bridge --config /project/bridge.json service status
```

Copy `plan_token` from the preview. It covers the definition, ownership, config,
and manager state. Changed inputs require a new preview. Registration creates
`STATE/service.json` and a unique user unit under `$XDG_CONFIG_HOME/systemd/user`
or `~/.config/systemd/user`, then reloads definitions. It neither starts a runner
nor enables automatic start. Pre-existing units/activation links, edited files,
drop-ins, and unknown prior launches are refused.

The generated unit runs foreground Python through fixed GNU `env`, supporting
spaces and literal percent/dollar characters without a shell. The service entry
checks registration, runtime identity, and systemd main PID before recovery or
claim. `Restart=no` prevents automatic runner or provider retries.

## Start and stop explicitly

```sh
agent-bridge --config /project/bridge.json runner start --apply
agent-bridge --config /project/bridge.json runner status
agent-bridge --config /project/bridge.json runner stop --run-id RUN_ID --apply
agent-bridge --config /project/bridge.json runner stop --run-id RUN_ID --cancel-active --apply
```

The existing runner commands and management API route registered projects to the
service backend. `service start` and `service stop` also have preview/apply forms.
Starting can execute queued provider work and consume usage. Repeated start only
reuses the observed healthy service run; an unknown start is never retried
automatically. Direct foreground `run` cannot bypass a registration in this runtime.

Default stop drains claimed work and leaves queued work queued. Explicit cancel
stops active owned work. If waiting expires, inspect the pending stop instead of
retrying tasks. Pause, call limits, and cumulative usage are preserved.

External `systemctl --user stop UNIT` sends SIGTERM to the main runner, which drains
active work. `KillMode=mixed` lets the provider finish during that drain; after
`TimeoutStopSec=30`, systemd may kill remaining processes in the unit's cgroup.
This follows the [systemd kill contract](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.kill.xml).
Forced interruption is recovered as interrupted on an explicit subsequent run;
it is never requeued automatically. PatchPort's stop requests drain/cancel first
and issues the OS stop only after the matching run acknowledges completion.

## Automatic start and unregister

```sh
agent-bridge --config /project/bridge.json service enable
agent-bridge --config /project/bridge.json service enable --apply --expect-plan TOKEN
agent-bridge --config /project/bridge.json service disable
agent-bridge --config /project/bridge.json service disable --apply --expect-plan TOKEN
```

Enable does not start now. It links the unit to the user manager's `default.target`,
so queued work may run at a subsequent user-manager startup, typically login.
Disable does not stop running work. Existing system/session and lingering policies
still apply; PatchPort never changes them. Enable and start are distinct in the
[systemctl contract](https://raw.githubusercontent.com/systemd/systemd/main/man/systemctl.xml).

Stop and disable before removing registration:

```sh
agent-bridge --config /project/bridge.json service unregister
agent-bridge --config /project/bridge.json service unregister --apply --expect-plan TOKEN
```

Unregister removes only the verified owned unit and project registration. Runtime,
project files, queue, answers, backups, and usage remain. Edited files are preserved.
Registration/removal failures leave a partial record blocking manual fallback;
inspect it and explicitly preview unregister to continue. Failures never retry
tasks or trigger automatic cleanup.

Before reconnecting to a different runtime, use the registering runtime to stop,
disable, and unregister. Reconnect with the new runtime, then register again.
Runtime/configuration drift blocks service execution. Known prior launch receipts
are preserved by carrying their confirmed hash in the lifecycle record; unknown
receipts remain blocked.

## Validation boundary

Ubuntu WSL2 with systemd 259 and Python 3.14.4 was tested as uid 1000. Dedicated
mock units covered registration without execution, enable/disable, actual SIGTERM
drain, explicit cancellation, crash handling without automatic restart, backend
transitions, queue/answer/usage preservation, and final unit/link removal.
Windows tests simulate the manager and do not claim Windows service support.
Real login/logout, the full 30-second forced-stop timeout, macOS, and independent
Linux hosts remain unverified. Actual provider calls were zero.
