# Installation and commands

Install from the repository root with `python -m pip install .` in an isolated Python 3.11+ environment. Check `agent-bridge --help`. Install and authenticate provider CLIs separately.

```sh
agent-bridge init
agent-bridge doctor
agent-bridge context --prompt-file request.md
agent-bridge submit --id review-001 --targets claude codex --prompt-file request.md
agent-bridge run --once
agent-bridge result --id review-001 --brief
```

Review configuration first. Place --config PATH before the subcommand. Submission does not execute tasks. Reuse a running worker for the same state and query the existing ID after a wait timeout.

Batch IDs identify result/wait queries. The returned task IDs identify review/apply/recover operations. Task artifacts include answer.txt, changes.json, events.jsonl, and stderr.log under state/workspaces/TASK_ID.

Keep include/writable paths explicit and relative. Do not include authentication files, virtual environments, or caches. On Windows use a native executable or an explicit node executable plus CLI script; shell shims are rejected. Retain read-only sandboxing for review and use workspace-write only for an authorized edit.

Review the actual proposal and tests before applying selected paths. recover previews the reported backup ID; --apply performs recovery. Preserve permissions and never retry failed provider work automatically.
