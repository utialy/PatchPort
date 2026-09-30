# Changelog

## Unreleased

- Add `agent-bridge setup` for interactive project and CLI selection, starter files, and JSON previews without manually editing configuration.
- Preserve existing settings, recheck selected inputs before application, and report partial installation failures with backup evidence. Keep identical pre-existing user files outside new managed ownership.

- Add `agent-bridge connect` with wheel-bundled installation assets, preview by default, conflict checks, and explicit backup/replacement. Keep the standalone connector compatible.
- Add reviewed summary plans with frozen source/body/record hashes, explicit metadata inputs, and reference-only prompt rendering. Preserve full required rules and writable files.
- Keep summary tasks inaccessible to older runners through separate persisted queue states.
- Preserve exact UTF-8 prompt bytes on Windows so delivered input matches the budget.

These changes are available in source builds; the published 0.1.0a2 release assets are unchanged. No automatic summary generation, provider retries, session resumption, token/cost enforcement, or whole-flow deletion is added.

## 0.1.0a2

Alpha release for local CLI task orchestration. APIs and archive formats may change.

- Keep a flow-lifetime lock across development, host checks, and review.
- Preview whole-flow cleanup without creating queue sidecars or deleting files.
- Preserve exact answers, source copies, diffs, and test logs in a deduplicated archive.
- Continue interrupted archive I/O explicitly using the original operation and plan hash.
- Reject changed sources/queues, corrupt evidence, and active or protected work.
- Allow live role management after an archive-only snapshot is fully verified.
- Include all required project helpers and tests in the source distribution.

The runtime remains Python 3.11+ with no third-party runtime dependencies. Provider CLIs and authentication are configured separately. `python -m agent_bridge` and `agent-bridge` remain the public entry points.

Known limitations: no whole-flow deletion, automatic archive repair/abandon, token/cost budget enforcement, or automatic provider retries. Legacy archive journals without a persisted plan cannot resume. Partial corrupt files and changes to the owning queue require inspection rather than automatic repair. Runtime working copies do not replace provider sandbox controls.

See [validation](docs/VALIDATION.md) for measured platform coverage. Mock-provider tests do not establish real provider authentication or behavior on every platform.
