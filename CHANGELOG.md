# Changelog

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
