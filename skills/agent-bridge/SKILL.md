---
name: agent-bridge
description: Submit and inspect project-local tasks through the Agent Bridge package, including parallel Claude and Codex CLI calls and reviewed workspace promotion. Use when a user asks to coordinate external AI CLI workers through this bridge.
---

# Agent Bridge

Use the installed `agent-bridge` CLI and the target project's explicit `bridge.json`. For installation, commands and result paths read [references/usage.md](references/usage.md).

- Inspect configuration before submitting. Preserve the selected project, endpoints, include/writable paths, concurrency and existing CLI authentication.
- Fan-out creates one billable/provider invocation per target. Use only requested targets; do not add recursive calls or retries.
- Publish with a new batch ID and a prompt file. Submission does not start a runner. Use an existing runner or start one when the user's task authorizes it; never create a duplicate runner for the same state directory.
- Read each target's terminal state, final answer and changes separately. ERROR, TIMEOUT and INTERRUPTED are not successful answers. An interrupted ID is not automatically retried.
- Review actual changed files and relevant tests before explicit path selection with `apply`. Multiple target proposals may conflict. Do not automatically combine them or overwrite a later edit.
- Recovery is preview by default. Use the reported backup name and inspect conflicts before `--apply`.
- The working copy is not a security sandbox. Retain provider permission controls. Do not copy credentials, environments or dependency caches into a task to make it run.
- Distinguish tested OS/provider behavior from planned support. See docs/VALIDATION.md for recorded validation.
