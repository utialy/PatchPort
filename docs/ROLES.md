# Project connection and roles

## Connect another project

The repository helper installs a project-local bridge.py launcher, role helpers, and peer-consult skills. Use an interpreter with agent_bridge installed:

```sh
python tools/connect_project.py --project /path/to/project
python tools/connect_project.py --project /path/to/project --apply
```

The first command previews create/update/keep/conflict actions. Existing configuration, rules, and request files are preserved. New projects can supply reviewed --config-template and --roles-template paths. The helper does not create the target project or start a runner.

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
