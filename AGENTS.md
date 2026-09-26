# Repository instructions

Read README.md, docs/ARCHITECTURE.md, and CONTRIBUTING.md before changing code.

- Treat this checkout as an independent repository. Do not read or change a parent development checkout to complete a task here.
- Use English for comments, docstrings, documentation, CLI help, and commit messages. Keep prose concise and factual. Keep comments that explain invariants, compatibility, or a non-obvious decision; remove comments that only repeat the code.
- Preserve copyright notices, license text, and required attribution. Do not invent authorship or test results.
- Keep Python 3.11+ and a standard-library-only runtime. Preserve `python -m agent_bridge` and `agent-bridge`.
- Keep submission separate from execution. Never retry failed or interrupted provider tasks automatically; use a new ID for an authorized rerun.
- Preserve SQLite submission transactions, atomic claims, and one runner per state directory.
- Work in explicitly selected copies. Apply proposals only to reviewed, explicitly selected original paths. Keep provider authentication and permission settings intact.
- Do not copy credentials, virtual environments, caches, or operational state into tasks or commits.
- Report Windows, Linux/WSL, and macOS validation separately. A CI matrix is not evidence that each job passed.
- Follow docs/COMMIT_RULES.md when committing. An authorized commit does not require another confirmation if its scope remains unchanged. Do not push or publish without authorization.
- Update the relevant public documentation when behavior changes. Do not add session transcripts or personal handoff files.
- For maintainer imports, follow docs/MAINTAINER_UPDATES.md. Accept reviewed changes only; preserve public-only files and keep this checkout independent of private development records.

Install the package into an isolated environment, then run:

```sh
python -m unittest discover -s tests -v
python scripts/check_public_release.py --tree
```

Source-only tests use PYTHONPATH=src. Project connection tests also require an installed package in the selected interpreter.
