# Experimental macOS LaunchAgent

This backend is implemented but **has not been executed on macOS**. Windows/WSL
tests use synthetic launchctl responses and temporary files. Native output,
permissions, GUI sessions, login/logout, and process cleanup need Mac validation.

Use the installed current runtime as a non-root user in a macOS GUI session.
The service CLI selects `launchd-user` on macOS. Manual runners remain available
without registration. An SSH session without a GUI domain cannot register this
agent; a missing GUI domain is not treated as an absent job.

## Registration and immediate start

```sh
agent-bridge --config /project/bridge.json service register
agent-bridge --config /project/bridge.json service register --apply --expect-plan TOKEN
agent-bridge --config /project/bridge.json service status
agent-bridge --config /project/bridge.json runner start --apply
```

Use the exact preview token. Registration creates a private plist under
`~/Library/Application Support/PatchPort/Agents` and a project service.json record.
`RunAtLoad` and `KeepAlive` are false. Registration does not bootstrap or kickstart.
Start loads the manual plist if needed, then explicitly kickstarts `gui/UID/LABEL`.
The system and other users' domains are never targeted.

Program arguments are a plist array with fixed Python/config paths and working
directory, not a shell command. No authentication secrets are stored in the plist.
These keys follow Apple's [published plist reference](https://raw.githubusercontent.com/apple-oss-distributions/launchd/main/man/launchd.plist.5).
Modern launchctl command/output compatibility still requires native verification.

An unconfirmed start leaves a pending marker and cannot cause another kickstart
automatically. A matching observed run can be reused. Otherwise inspect and stop,
or explicitly unregister once inactive. Unknown print output and permission errors
never mean the job is absent. Environment dictionaries are omitted from results.

## Login automatic start

```sh
agent-bridge --config /project/bridge.json service enable
agent-bridge --config /project/bridge.json service enable --apply --expect-plan TOKEN
agent-bridge --config /project/bridge.json service disable
agent-bridge --config /project/bridge.json service disable --apply --expect-plan TOKEN
```

Enable creates an owned `RunAtLoad=true` copy under `~/Library/LaunchAgents` for a
future login. It never calls bootstrap/kickstart. Disable removes that copy while
leaving the loaded job alone. This file/runtime separation follows the documented
[user-agent login lifecycle](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
and remains subject to native verification on the target macOS release.

External launchctl disabled overrides are preserved and block start/enable.
The parser accepts explicit boolean and enabled/disabled spellings. The named
form appears in [first-hand output on Apple's developer forum](https://developer.apple.com/forums/thread/783433);
this reported example does not replace native validation of this implementation.
Identical pre-existing plists are not adopted. Modified, linked, foreign-owned,
or group/world-writable control files require inspection.

## Stop and unregister

```sh
agent-bridge --config /project/bridge.json runner stop --run-id RUN_ID --apply
agent-bridge --config /project/bridge.json runner stop --run-id RUN_ID --cancel-active --apply
agent-bridge --config /project/bridge.json service unregister
agent-bridge --config /project/bridge.json service unregister --apply --expect-plan TOKEN
```

Default stop drains through the existing queue; cancellation is explicit. Bootout
follows only after the matching run stops and its observed PID has not changed.
Stop leaves automatic-start configuration intact. Disable and stop before unregister.
Unregister removes only the owned manual plist and registration after unload is
confirmed. Runtime, project files, queue, answers, and cumulative usage remain.

The plist has a 30-second exit timeout. Logout or external bootout can force an
exit if graceful shutdown exceeds the OS deadline. Native signal and descendant
cleanup remain unverified. Partial changes retain records, and ambiguous/unowned
files are preserved for manual inspection rather than automatically adopted.

## Native acceptance procedure

Use a disposable project with a command-adapter mock and a small call cap, never
production files or real provider credentials. Record macOS version, CPU, Python,
and GUI UID. Check registration without execution; enable without immediate start;
start/reuse; pending-start refusal; drain/cancel; disable while running; real
logout/login; crash without restart; unregister; and preserved data hashes.
Capture sanitized print fixtures without environment values. Confirm both owned
plists and the test job are absent afterward. This procedure has not yet been run.
