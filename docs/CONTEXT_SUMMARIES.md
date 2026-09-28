# Reviewed reference summaries

Use a version 2 context plan to reuse a summary you have reviewed. PatchPort does not generate the summary, search previous conversations, or resume a provider session automatically. This feature is in current source builds, after the published 0.1.0a2 assets.

## Prepare the inputs

Place the source text, reviewed UTF-8 summary body, and a separate JSON record inside the configured include paths. Keep required rules and files to be edited as full input. Review the summary's purpose, facts, decisions, open questions, omitted scope, and source references; software cannot establish its semantic accuracy.

Example record at `docs/history-summary.json`:

```json
{
  "schema": 1,
  "summary_id": "history-1",
  "created_at": "2026-09-28T00:00:00Z",
  "body_path": "docs/history-summary.md",
  "body_sha256": "<actual lowercase SHA256 of the summary file bytes>",
  "sources": [
    {"path": "docs/history.md", "sha256": "<actual lowercase SHA256 of the entire source file bytes>"}
  ],
  "reviewed": true,
  "provenance": {"tool": "manual", "source_task_id": "previous-review"}
}
```

Replace each placeholder with its actual 64-character lowercase hash. Set reviewed=true only after reviewing the body. Use the actual UTC creation time, containing T and ending in Z; future timestamps are rejected. summary_id is at most 128 ASCII letters, digits, underscores, dots, or hyphens, starting with a letter or digit. Optional provenance allows only tool and source_task_id, with nonempty string values. Use UTF-8 JSON without a BOM. Unknown fields, duplicate keys, non-finite numbers, and invalid types are rejected.

Compute hashes from file bytes, for example:

```sh
python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('docs/history.md').read_bytes()).hexdigest())"
python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('docs/history-summary.md').read_bytes()).hexdigest())"
```

Example plan when these are the only five include candidates:

```json
{
  "schema": 2,
  "summaries": ["docs/history-summary.json"],
  "files": [
    {"path": "AGENTS.md", "mode": "full", "reason": "Current project rules"},
    {"path": "src/example.py", "mode": "full", "reason": "Editable source"},
    {"path": "docs/history.md", "mode": "summary", "summary_id": "history-1", "reason": "Reviewed historical reference"},
    {"path": "docs/history-summary.json", "mode": "metadata", "reason": "Summary record"},
    {"path": "docs/history-summary.md", "mode": "metadata", "reason": "Reviewed summary body"}
  ]
}
```

List every include candidate exactly once. Other files can use full or omit. Every source of a multi-source summary must select the same summary_id. Sources cannot belong to multiple summaries. Body and record files must use metadata; they cannot also be copied or omitted. Unreferenced metadata is rejected.

```sh
agent-bridge context --prompt-file request.md --context-plan context-plan.json
agent-bridge submit --id reviewed-context-001 --targets claude --prompt-file request.md --context-plan context-plan.json
```

Use your configured endpoint name. Preview and submission do not call providers; execution requires the separate runner. Provider authentication, permissions, terms, and account limits still apply.

## Validation and execution

- context_required and included AGENTS.md files must remain full. As a conservative editing safeguard, every path covered by writable is ineligible for summary replacement. Permissions are never adjusted automatically.
- Sources, bodies, and records must be explicit regular files under include. Traversal, excluded paths, links, and aliases are rejected. Sources and bodies must be valid UTF-8 with no NUL. Empty source text is allowed; empty summary text is not.
- Submission freezes summary bodies, records, provenance, source hashes, relevant settings, and the final prompt hash in the same transaction as all targets. Changing the plan file afterward does not alter the saved plan.
- The runner rechecks inputs before and after copying full files. A source, body, or record change, including record whitespace or creation time, rejects execution. Omitted file contents may change; the candidate path set must stay the same.
- Only full files enter the working copy and baseline. Summaries are appended as JSON reference data after the complete Bridge instructions and user request. They never overwrite source paths. The reference label is not a security sandbox or a guarantee against misleading content.
- VALID denotes successful checks; STALE denotes changed sources or frozen input; MISSING denotes missing inputs or candidate mismatch; INVALID denotes malformed/unreviewed records, invalid timestamps, or body hash failure. Existing path/required-file errors can use their original messages.
- Failure before submission creates no task. Failure after claim records ERROR and retains the consumed claim. Use a new ID after review; no retry, refresh, or full-source fallback happens automatically.

Checks are not a globally atomic filesystem snapshot. External changes after the final check remain outside this guarantee.

## Budgets and retained evidence

max_files counts full files. max_bytes counts their bytes plus the exact rendered UTF-8 prompt, including summary framing and JSON escaping. Prompt bytes are preserved on Windows as well as POSIX. Estimated tokens remain ceil(total_bytes / 4), not measured model usage. source_bytes is an audit figure and is not counted again as delivered input. Actual token or cost savings have not been measured.

Preview exposes selected, omitted, summarized, metadata, summaries, summary_status, summary_age_seconds, and rendered_prompt_sha256. Summary age is informational; age alone does not invalidate a record. Hashes and a review declaration do not prove semantic accuracy.

Detailed result/wait context_plan records retain bodies and provenance after task artifact pruning. Only include authorized material; do not put credentials or other secrets in summary files.

## Compatibility

Summary tasks persist as SUMMARY_QUEUED/SUMMARY_RUNNING; the new Store presents QUEUED/RUNNING. Earlier runners cannot claim or recover them. Update and explicitly restart a runner before using summary plans. An old run --once may return zero while leaving unsupported work untouched.

Version 1 full/omit plans remain supported. Developer/test/reviewer helpers still use v1; role submissions with v2 summaries are rejected. Role-specific summary handoffs and native provider-session resumption remain outside this feature.
