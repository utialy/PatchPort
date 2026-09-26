# Maintainer update workflow

## Development and public changes

Maintainers may develop changes in a separate checkout and bring reviewed patches into this repository. This checkout must remain independently buildable and testable. Contributors do not need access to private planning files or another repository.

- Import a selected source revision and an explicit set of paths. Do not recursively copy another checkout or its Git history.
- Compare changed files against the last reviewed import. Preserve existing public edits; report conflicts rather than overwriting them.
- Treat source, shared tests, helpers, skills, and examples as candidates, not an unconditional mirror. Keep public-only tests and scripts.
- Maintain this repository's README, documentation, license, agent instructions, contributor rules, hooks, CI, and package metadata here. Merge relevant behavior changes into them instead of replacing them with private files.
- Never import session notes, private handoffs, local paths, credentials, queues, provider logs, task copies, caches, or virtual environments.
- Write new public prose in English. Preserve Unicode test behavior with equivalent source escapes. Keep required attribution and license notices.

## Import review

1. Inspect the selected source diff and this checkout's pending changes. Do not create commits or stash changes just to make the import possible.
2. Apply the reviewed changes and update only the affected documentation. Do not regenerate every document or remove files merely because they are absent from another checkout.
3. Run release policy and relevant tests. Record actual platform coverage without claiming unexecuted CI results.
4. Commit one coherent public change using [the commit rules](COMMIT_RULES.md). Several development commits may be represented by one public commit; a development commit does not require an immediate public commit.
5. Record completed and outstanding paths in the maintainer's external import ledger. Advance a complete-import baseline only after all selected changes are accounted for. Keep private development notes outside this repository.

Public documentation or release-policy changes may be maintained here directly. Product fixes should be reconciled with the maintainer's development source before the next import. Do not silently erase a public-only fix during synchronization.

No automatic importer is provided. These rules do not authorize a push, release publication, live provider call, or deployment. Local hooks and CI enforce the content checks described in CONTRIBUTING.md; they do not implement synchronization.
