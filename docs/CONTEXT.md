# Input selection and context budgets

```sh
agent-bridge context --prompt-file request.md
```

The preview reads include paths and reports file paths, sizes, SHA256 hashes, missing inputs, and estimated input tokens. It does not create a queue or call a provider. Overlapping includes are counted once.

An optional configuration budget limits input files and the UTF-8 prompt, including Bridge instructions:

```json
{
  "context_budget": {
    "max_files": 200,
    "max_bytes": 1000000,
    "max_estimated_tokens": 250000
  }
}
```

Omitted limits are unlimited; zero is valid. Negative numbers, floats, booleans, and unknown keys are rejected. Token estimates are ceil(total_bytes / 4), including binary bytes. They are a heuristic, not a model tokenizer or a safe context-window bound.

Submission rejects exceeded budgets before publishing tasks. The runner checks the original input again after claim and checks the completed copy. A rejection after claim consumes that claim but does not launch the provider. It records the context result and does not retry.

The budget does not include everything a provider may later read, its system instructions, tool results, or previous conversation. It is not a cost budget.

## Frozen selection plans

```json
{
  "schema": 1,
  "files": [
    {"path": "AGENTS.md", "mode": "full", "reason": "Project rules"},
    {"path": "src/example.py", "mode": "full", "reason": "Requested change"},
    {"path": "docs/history.md", "mode": "omit", "reason": "Unrelated history"}
  ]
}
```

Every candidate file from include must appear exactly once. In schema=1, use only full or omit and provide a nonempty reason. Duplicate JSON keys, missing or extra paths, links, aliases, and summary mode in v1 are rejected. Reviewed summaries use a separate [schema=2 plan](CONTEXT_SUMMARIES.md).

Pass the plan to both preview and submission:

```sh
agent-bridge context --prompt-file request.md --context-plan plan.json
agent-bridge submit --id selected-001 --targets claude --prompt-file request.md --context-plan plan.json
```

Submission stores the selection in the same transaction as the batch. Later changes to the plan file do not change that record. Execution checks selected bytes, candidate paths, required input, prompt, and relevant configuration before calling the provider. Only selected files are copied. Changes to omitted file contents are allowed; adding or removing candidates is not.

`context_required` lists required input files. They must exist inside include. In plan mode, included AGENTS.md files are also required. Files outside include are not pulled in automatically. Ordinary submissions without a plan copy the current included files at execution time; they do not freeze those file versions at submission.

The plan states PLAN_QUEUED/PLAN_RUNNING keep older runners from claiming unsupported work. Update and restart an old runner before using selection plans. There is no automatic plan migration, truncation, summary generation, or retry.
