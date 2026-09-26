# Developer then reviewer

Use this route only when the user explicitly requests both development and review. It authorizes these two roles; do not add a third model or iterative retry loop. If the user explicitly asks for simultaneous review of the developer output, explain that the output must exist first. Independent review of the original code is a different task and this command does not implement it.

The project role_workflow.json selects the developer, the read-only reviewer, and the test commands between them. Inspect it before calling. Do not silently substitute providers: if the user names another combination, configure that separately within the authorized scope before use.

Create a unique UTF-8 .bridge-requests/<flow-id>.md containing the actual request, minimal current context, constraints, exact paths and output language. Preserve tasks/request.md. Generate a unique flow ID and invoke once:

`python bridge.py develop-review --id <flow-id> --prompt-file .bridge-requests/<flow-id>.md`

New flows store both role tasks and usage in the parent queue. Each stage can be claimed once and shares the parent's cumulative call cap and pause control with ordinary tasks. The working copies stay under .role-flows/<flow-id>. A matching resident runner executes the tasks; if no runner owns the parent lock, the helper runs only its submitted task under that same lock. It never starts a duplicate runner or drains unrelated queued tasks. Historical private-state flows remain readable and are not migrated or replayed.

If the cap is exhausted or pause is set before a stage, that stage does not start. A queued role blocked during its wait is cancelled atomically; if claim won the race, the running task continues and its call stays consumed. A wait timeout does not retry or start the next stage: queued work is cancelled, and already running work is reported as pending. Recover a dead runner only through the normal runner lifecycle; interrupted calls remain consumed. Inspect the actual result before submitting a new flow ID.

The helper captures original input, runs the developer, runs the configured tests in the developed copy, and copies the exact developed files into an isolated reviewer source. The reviewer receives __bridge_review__/context.json, changes.diff, and the test logs referenced by checks[].log, along with the original request and developer answer. The helper checks snapshot hashes and rejects reviewer file changes.

On a tool timeout, use `python bridge.py develop-review --result --id <flow-id>` to retrieve the saved combined result without any new agent call. Use raw logs only to diagnose a specific failure; do not load the entire developer/reviewer logs just to obtain their answers. Never repeat the start command. States DEVELOPER_FAILED, TESTS_FAILED, REVIEWER_FAILED, REVIEWER_MODIFIED_FILES or ERROR require reporting and inspection, not retries. A process interruption can leave an in-progress record that must be inspected before any new request.

Return both role outcomes, test results and review findings in the current conversation. REVIEW_DONE means the reviewer returned without changing files; it does not mean the reviewer approved the implementation or the task is correct. Read the actual review.

No originals are auto-applied. Inspect with `python bridge.py role-review --id <flow-id> --role developer`. For authorized promotion, preview `python bridge.py role-manage promote --id <flow-id> --role developer --task <task-id> --paths <reviewed-paths>` and add `--apply` only for explicitly selected paths within the user's authorization. Never promote the reviewer workspace. The helper checks current/original proposal hashes, current and saved writable rules, terminal task metadata, and runner/promotion locks.

Use `role-manage recover` with the same identity and `--backup <backup-id>` to preview rollback; `--apply` performs the authorized rollback. Use `role-manage prune` with an explicit flow/role/task and `--days 30` to preview cleanup, then `--apply` only for authorized deletion. Applied or damaged recovery backups are protected. Stop the runner before management operations; pause alone is insufficient. Do not pass a shared role's private config to ordinary prune: its records belong to the parent queue. Cleanup preserves queue records, answers and usage. These management commands never call AI or retry a task.

Preserve every material reviewer caveat in the user-facing result. Passing the configured examples does not establish correctness for all allowed inputs. Check a concrete counterexample when a reviewer mentions a risk; do not dismiss it using an input limit absent from the request.
