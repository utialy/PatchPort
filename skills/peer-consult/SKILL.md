---
name: peer-consult
description: "Delegate an explicitly requested question or review to a configured Claude or Codex CLI and bring the answer back into this conversation. Use for explicit requests to ask Claude or ask Codex to review. Also handles explicit developer-then-reviewer delegation using a configured role workflow. Do not trigger for ordinary local work, feature explanations, or inside a delegated Bridge worker."
---

# Peer consultation

Use this only as the interactive main agent in the project root containing both ./bridge.py and ./bridge.json. A delegated task copy has neither; do not search its parents for a launcher or re-delegate from it. The user's request authorizes only the explicitly requested consultations or roles, not recursive calls, extra targets, permission changes or original-file promotion.

For an explicit developer-and-reviewer request, read [references/roles.md](references/roles.md) and use that sequence. For a single consultation, read [references/usage.md](references/usage.md). Execute the workflow yourself; do not tell the user to write a request document or copy commands for routine consultation.

- Use exactly the peer the user named. Ask only if the target or question cannot be inferred. Keep the configured endpoint, model, authentication and permission settings.
- Prepare only the question, relevant current facts/decisions, requested output language and exact project-relative source paths. Explain whether the request is advice/review or an authorized edit. For advice/review explicitly prohibit changes. Do not forward the entire conversation, raw logs, secrets or obsolete handoffs. The peer does not automatically know this conversation.
- Confirm that the necessary saved files are available under include (and any selection plan). Do not silently expand include/writable or tool permissions. If context is missing, explain what cannot be provided under the current configuration.
- Reuse the existing healthy runner. If it is stopped, stale, paused, or at its call limit, report the state and retain any submitted ID; do not bypass controls or launch duplicate runners. Runner control changes require task/session authorization.
- Submit once with a unique ID and use wait/result on that ID. A timeout is not permission to resubmit. Failures require inspection and a newly authorized request with a new ID.
- Bring the peer's actual answer back into the current conversation, identify which peer answered, and separate its findings from your assessment. Preserve requested language. If wait/result reports ERROR, TIMEOUT, INTERRUPTED, or an unavailable/partial answer, stop and report consultation failure. Do not mine raw logs for a substitute successful review or present your own analysis as the peer answer. DONE is not proof of correctness. Preserve the reviewer's caveats and unverified assumptions, including observations labeled non-blocking. If a caveat contradicts the requested contract, keep it open rather than summarizing the result as fully correct.
- If files changed, inspect the manifest and relevant tests. A consultation does not authorize apply; use only separately authorized, reviewed original paths. Do not automatically merge different peers' changes.
