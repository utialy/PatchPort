# Contributing

Use an isolated environment with the package installed:

```sh
python -m pip install .
python -m unittest discover -s tests -v
python scripts/check_public_release.py --tree
```

The tests use temporary projects and mock providers. They do not require a provider account. The smoke scripts in tools/ make real provider calls; run them only with explicit authorization and a stated call limit.

Keep changes focused. Preserve public commands, the SQLite claim boundary, recovery data, and provider permissions. Add regression tests for behavior changes. Record the operating system and Python version used for validation; do not report unexecuted CI jobs as passing.

Write English prose. Comments should explain a constraint or decision that is not clear from the code. Unicode behavior tests may use escaped literals with the original runtime payload. Do not translate test data into ASCII if that removes the behavior under test.

Follow [commit rules](docs/COMMIT_RULES.md). To enable the repository hooks for this checkout:

```sh
git config --local core.hooksPath .githooks
```

The hooks use Python 3.11+ and Git. They inspect content and commit messages; they do not run providers, install packages, or push commits. Run the relevant tests before committing. Hooks are local checks, not a security boundary; CI runs the content policy again.

To check packaging, install the development build tool in your environment and run:

```sh
python -m pip install build
python -m build
```

The source distribution must include the project connection helpers, their launcher template, and both skill directories. The wheel contains the runtime and the agent-bridge skill.

Maintainers importing reviewed patches from a separate checkout should follow [the update workflow](docs/MAINTAINER_UPDATES.md). This repository remains independently buildable; private development records are not required.
