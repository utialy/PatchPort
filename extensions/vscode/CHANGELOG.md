# Changelog

## 0.3.0

- Add reviewed input/summary plans and explicit batch submission with separate runner control.
- Inspect copies and compare changed files, promote selected paths, and recover backups with unsaved-edit protection.
- Run durable development/test/review jobs on the existing queue and inspect role results and test logs.
- Preview and explicitly apply role cleanup, deletion-free archives, and interrupted archive resume.
- Restore paused boards, round limits and durable history; explicitly resume exact recorded provider sessions without replay.
- Bundle fixed workflow tools and validate runtime integrity before managed starts and reload.

## 0.2.0

- Connect existing configuration, jobs, retained results, usage, and batch controls through a bundled Python core.
- Persist managed sessions, prompt/response events, deliveries, and deduplicated usage snapshots in SQLite.
- Record automatic input attempts before PTY delivery and retain uncertain outcomes without automatic retry.
- Collect supported Claude/Codex usage signals with explicit scope, missing values, and partial sums.
- Separate lifetime batch claims, group delivery attempts, input budgets, and reported consumption in the UI.
- Require Python 3.11+ through the configurable corePython executable; retain existing CLI authentication and profiles.

## 0.1.2

- Explain current external peers and automatic delivery to Claude on each prompt, including connections made after startup.
- Clear stale input blocking when the provider acknowledges a submitted prompt, while preserving a newer unsent draft.
- Distinguish active arrows from paused, offline, focused, busy, and exhausted routes in the board.
- Validate a same-provider round trip using separate `claude-2` and `claude` launch profiles without native session discovery.

## 0.1.1

- Restore terminal screen snapshots and synchronize dimensions on restart.
- Keep automatic submission outside the CLI paste detection interval.
- Preserve existing-terminal identity and limit explicit paste to one line.
- Select newly saved launch profiles immediately.
- Include reproducible Windows PTY cleanup fixes and complete license notices.

## 0.1.0

- Add a VS Code workspace board with real interactive terminal cards.
- Support custom commands, fixed arguments, and explicit shell profiles.
- Connect Claude Code and Codex using completed-response events.
- Add directed routes, collaboration groups, bounded delivery, pause, and direct input protection.
- Save board layout without automatically restarting AI sessions.
- Package a Windows x64 VSIX with terminal runtime dependencies.
- Provide limited existing-terminal focus and explicit paste actions.
