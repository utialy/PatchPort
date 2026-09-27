# Agent Bridge

Agent Bridge runs Claude, Codex, and command-line workers from a project-local SQLite queue. Each task receives an explicit working copy. Changes stay in that copy until you review and apply selected paths.

Version 0.1.0a2 is an alpha release. The runtime requires Python 3.11 or later and uses only the standard library. Provider CLIs must be installed and authenticated separately.

## Install

From this checkout:

```sh
python -m venv .venv
```

Activate the environment with `.venv\Scripts\Activate.ps1` on PowerShell or `source .venv/bin/activate` on Linux/macOS, then run:

```sh
python -m pip install .
agent-bridge --help
```

`python -m agent_bridge` exposes the same commands. See [validation](docs/VALIDATION.md) for the environments actually tested.

## Try it without a provider account

In an empty directory, save this as `bridge.json`:

```json
{
  "project": ".",
  "state": ".agent-bridge",
  "include": ["request.md"],
  "writable": ["request.md"],
  "endpoints": {
    "mock": {"adapter": "command", "command": ["python", "-c", "print('BRIDGE_OK')"]}
  }
}
```

Create `request.md` with the text `Return the marker`. With the installed virtual environment active, run:

```sh
agent-bridge submit --id first-run --targets mock --prompt-file request.md
agent-bridge run --once
agent-bridge result --id first-run --brief
```

The result should contain `DONE` and `BRIDGE_OK`. This example runs a local Python command and makes no provider calls. Submission queues the task; `run` executes it. Use a new ID to try again.

## Run a provider task

Run these commands from the project you want the workers to inspect:

```sh
agent-bridge init
```

Edit `bridge.json` before submitting. Choose the input paths, writable paths, and provider commands. Write the request to `request.md`.

```sh
agent-bridge doctor
agent-bridge context --prompt-file request.md
agent-bridge submit --id review-001 --targets claude codex --prompt-file request.md
agent-bridge run --once
agent-bridge result --id review-001 --brief
```

Submission only queues work. `run` starts it. Each target is a separate CLI invocation and may consume account usage. Do not start two runners against the same state directory. Use a new ID for a rerun.

`doctor` checks command resolution, not authentication or sandbox startup. On Windows, use a native executable or an explicit `node.exe` plus CLI script command; `.cmd`, `.bat`, and `.ps1` shims are rejected.

## Review and apply changes

```sh
agent-bridge review --task review-001--claude
agent-bridge apply --task review-001--claude --paths src/example.py
```

`apply` writes the selected paths after checking the original and proposed hashes. Use the returned backup ID to preview or perform recovery:

```sh
agent-bridge recover --task review-001--claude --backup BACKUP_ID
agent-bridge recover --task review-001--claude --backup BACKUP_ID --apply
```

Replacement is atomic per file, not across a group of files. A working copy is not a security sandbox. `writable` limits promotion to the original project; it does not enforce provider file access. Keep the provider's permission controls enabled.

`DONE` means the process completed according to its adapter. It does not mean the requested task is correct; results retain `task_success=NOT_EVALUATED`.

## Reference

- [Commands, execution limits, and cleanup](docs/CLI.md)
- [Input selection and context budgets](docs/CONTEXT.md)
- [Usage and preserved answers](docs/USAGE.md)
- [Project connection and development/review roles](docs/ROLES.md)
- [Archived result and evidence readers](docs/ARCHIVES.md)
- [Release notes and known limitations](CHANGELOG.md)
- [Architecture](docs/ARCHITECTURE.md) and [remaining work](docs/ROADMAP.md)
- [Contributing](CONTRIBUTING.md) and [commit rules](docs/COMMIT_RULES.md)

Repository helpers and peer-consult skills ship in the source distribution. They are not additional subcommands in the runtime wheel.

## License

Licensed under the [MIT License](LICENSE).
