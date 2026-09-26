# Local Bridge workflow

Run from the current project root containing bridge.py and bridge.json. The project wrapper already points to its installed Bridge Python. Do not use a launcher from the Agent_Bridge implementation checkout or another project.

1. Inspect bridge.json and run `python bridge.py status`. Confirm the named endpoint and input paths, a held runner lock with FRESH heartbeat, restart_required=false and control.dispatch=READY. If any are unavailable, explain the exact blocker. `doctor` resolves commands only; it does not test authentication.
2. Generate a unique batch ID such as `peer-` plus a UUID. Write the delegated question as UTF-8 to `.bridge-requests/<batch-id>.md` using the host file-writing tool or correctly quoted file APIs. Use exclusive creation. Do not overwrite tasks/request.md. Do not interpolate the question into a shell command, command substitution, or a provider flag.
3. Preview the input using `python bridge.py context --prompt-file .bridge-requests/<batch-id>.md`. If a reviewed selection plan is used, add the same `--context-plan <path>` to both preview and submit. Explain an exceeded budget or missing input instead of trimming instructions automatically.
4. Submit once: `python bridge.py submit --id <batch-id> --targets <named-endpoint> --prompt-file .bridge-requests/<batch-id>.md`. Record the returned IDs before waiting. This normally creates a billable provider invocation when the runner claims it.
5. Retrieve: `python bridge.py wait --id <batch-id> --timeout 240 --brief`. If the tool cannot wait that long, wait in shorter intervals using the same ID. Inspect `python bridge.py result --id <batch-id> --brief` later; never create another ID merely because waiting timed out.
6. Read answer.text, state, error and changes from the JSON and answer the user in the current chat. Include the ID when useful for follow-up. The full detailed result and workspaces/<task>/answer.txt are optional diagnostics, not something the user must open to receive the reply.

Use `python bridge.py review --task <task-id>` for a changed-file manifest. Changes remain in the working copy until a separately authorized `apply --task <task-id> --paths <explicit paths>` is performed. Delegated workers must not call the Bridge again.

local-check is only a non-AI transport test. Never label its output as a Claude or Codex answer. Discovery depends on the host CLI, project trust and shell permissions; installation does not grant new permissions. If automatic selection is missed, explicitly invoke the installed peer-consult skill.

For a headless Claude main session, path-scoped file creation permissions use Edit(path), not Write(path). If a request-file write is denied, report the blocked action and retain the attempted ID; do not widen permissions automatically.
