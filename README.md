# PatchPort

An MIT-licensed, open-source bridge that lets Codex and Claude Code consult each other and review each other's work.

Ask Claude Code for a second opinion while working with Codex, or ask Codex to review a proposal from Claude Code. PatchPort passes your question and selected project context to the other CLI and brings its answer back into your current conversation.

It also supports a developer/test/reviewer workflow: one agent proposes changes, host checks run, and another agent reviews the results. Conversations are not automatically synchronized; each consultation receives the question and context prepared for it.

- **Consult another agent.** Request a second opinion or an independent review from your current session.
- **Choose the context.** Select the input files and inspect context budgets before submitting work.
- **Control execution.** Queue tasks separately from running them, using your existing provider CLIs and authentication.
- **Review before applying.** Inspect proposed changes, apply explicit paths, and use backups for recovery.
- **Keep the evidence.** Preserve answers, diffs, and test logs for later review.

Version 0.1.0a2 is an alpha release. The runtime requires Python 3.11 or later and uses only the standard library. Provider CLIs must be installed and authenticated separately.

PatchPort is the project name. The current package is `local-agent-bridge`; its CLI remains `agent-bridge`, with `python -m agent_bridge` as an equivalent entry point. Provider usage may incur costs.

Repository: [utialy/PatchPort](https://github.com/utialy/PatchPort).

## Install

Clone the repository (or unpack the source distribution) to get the project connector and peer-consult skills:

```sh
git clone https://github.com/utialy/PatchPort.git
cd PatchPort
```

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

## Connect Codex and Claude Code

Follow the [project connection and peer consultation guide](docs/ROLES.md#connect-codex-and-claude-code) to configure your project, install the peer-consult skills, and start a runner. Then, from the connected project, ask either agent something like:

> Ask Claude to review this implementation for edge cases. Do not change files.

> Ask Codex for a second opinion on this design and bring its answer back here.

The installed skill prepares the consultation and retrieves the other agent's actual response. Skill discovery depends on your CLI and project permissions; explicitly invoke `peer-consult` if needed. Consultations require the requested provider to be configured and authenticated.

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
