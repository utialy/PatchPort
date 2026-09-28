# Project connection and roles

## Connect Codex and Claude Code

Current source builds provide `agent-bridge connect` and bundle its installation assets in the wheel. The published 0.1.0a2 wheel predates this command; install the current source version before using it.

1. Install PatchPort as described in the [README](../README.md#install), and install and authenticate the provider CLIs you want to use.
2. In the project you want to work on, run `agent-bridge init`. Edit `bridge.json` to select existing input files, writable paths, and the Claude/Codex endpoint commands. Run `agent-bridge doctor` to check command resolution; it does not verify authentication. On Windows, follow the executable guidance in the README.
3. With the installed environment active, preview `agent-bridge connect --project /path/to/project`. Inspect the proposed actions, then repeat with `--apply`. Replace the example path with your project directory. A current wheel installation does not require the source checkout at runtime.
4. In the connected project, start `python bridge.py run` in a separate terminal and leave it running. Use only one runner for this project's state. `python bridge.py status` should report a fresh runner and dispatch ready.
5. Open Codex or Claude Code in that project and explicitly ask it to consult the configured peer. The connector installs `peer-consult` under both `.agents/skills` and `.claude/skills`. If discovery misses it, invoke the skill explicitly.

For example, ask Codex: "Ask Claude to review the selected implementation for edge cases without editing files, and bring its answer back here." The main agent sends the question and relevant project context, waits for the result, and returns the peer's answer with its own assessment kept separate. You can request the reverse direction from Claude Code.

The peer does not inherit your entire conversation. A consultation starts a separate provider CLI invocation and may consume account usage. Delegated workers do not recursively consult other agents. Any proposed changes remain in their working copy until you explicitly review and apply them.

## Connect another project

The connector installs a project-local bridge.py launcher, role helpers, and peer-consult skills. Use an interpreter with the updated agent_bridge package installed:

```sh
agent-bridge connect --project /path/to/project
agent-bridge connect --project /path/to/project --apply
```

The first command previews create/update/keep/conflict actions. Existing configuration, rules, and request files are preserved. New projects can supply reviewed --config-template and --roles-template paths. Use --python to choose an installed runtime. The connector does not create the target project, log in, call a provider, or start a runner. Target configuration comes from --project; do not specify an alternative global --config. The repository script tools/connect_project.py remains a compatible entry point to the same logic.

Managed file hashes live in .bridge-integration.json. Changed local files cause a conflict. --adopt-existing --apply explicitly backs up and replaces conflicting managed integration files. Multi-file installation is not atomic; inspect a partial-install error before retrying.

## Develop, test, review

role_workflow.json defines developer and reviewer endpoints and a nonempty checks array. The reviewer must use Codex read-only sandboxing or Claude with only the Read tool. Keep the selected provider's existing authentication and permissions.

Start from [the role configuration example](../examples/role_workflow.json) and adapt commands, permissions, and checks to the target project. Its test command assumes an activated Python environment and tests under tests/. On Windows, replace CLI shims with native executable paths as described in the command reference.

```sh
python bridge.py develop-review --id change-001 --prompt-file request.md
python bridge.py develop-review --id change-001 --result
python bridge.py overview
```

The helper snapshots input, runs the developer, executes host checks in the developed copy, and prepares separate reviewer input. The reviewer receives the request, answer, diff, test exit codes, and exact test logs referenced by checks[].log. Test failure prevents review. Reviewer edits are reported as a failure condition.

New roles use the parent SQLite queue and share pause and cumulative claims with ordinary tasks. Each flow/role pair is unique. A matching resident runner executes submitted roles; otherwise the helper runs only its explicitly submitted task under the parent lock. Historical private queues stay separate.

Read saved results after a tool timeout. Do not repeat the start command. Queued work may be cancelled when dispatch is blocked; claimed work continues and retains its consumed count. REVIEW_DONE means review completed without detected reviewer changes, not approval or proof of correctness.

## Inspect and manage copies

```sh
python bridge.py role-review --id change-001 --role developer
python bridge.py role-manage promote --id change-001 --role developer --task TASK_ID --paths src/file.py
python bridge.py role-manage promote --id change-001 --role developer --task TASK_ID --paths src/file.py --apply
python bridge.py role-manage recover --id change-001 --role developer --task TASK_ID --backup BACKUP_ID
python bridge.py role-manage prune --id change-001 --role developer --task TASK_ID --days 30
```

Management previews by default. Recovery and prune require --apply to modify files. Only completed developer proposals can be promoted. Current and saved writable policies, original/proposal hashes, task metadata, runner locks, and recovery protection all apply. Never promote a reviewer workspace.

Role prune removes the selected workspace, not the whole flow. Applied, incomplete, or damaged backups remain protected. Stop the runner first. Management previews may create lock files; role-review and overview do not dispatch work. After workspace deletion, ordinary role inspection reports missing artifacts.

overview reads existing queues, keeps shared and private task identities distinct, and counts usage once. Snapshots are consistent per database, not across every database and file. Missing/corrupt data remains visible. Archived views follow [the archive reader contract](ARCHIVES.md).
