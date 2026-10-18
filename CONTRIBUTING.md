# Contributing

Use an isolated environment with the package installed:

```sh
python -m pip install .
python scripts/run_tests.py
python scripts/check_public_release.py --tree
```

The tests use temporary projects and mock providers. They do not require a provider account. Real provider calls require explicit authorization and a stated call limit.

Keep changes focused. Preserve public commands, the SQLite claim boundary, recovery data, and provider permissions. Add regression tests for behavior changes. Record the operating system and Python version used for validation; do not report unexecuted CI jobs as passing.

Write English prose. Comments should explain a constraint or decision that is not clear from the code. Unicode behavior tests may use escaped literals with the original runtime payload. Do not translate test data into ASCII if that removes the behavior under test.

Use an English Conventional Commit subject, such as `fix: preserve cleanup receipts`.
Describe the problem, resulting behavior and checks in pull requests. Run the
relevant tests and review `git diff --check` before submitting.

For each native extension target, use Node.js 24+ and an installed Python 3.11+ runtime
in the checkout's .venv. From extensions/vscode run `npm ci`, `npm run check`,
`npm test` and `npm run package`. Generated core copies/manifests, bundles and
VSIX artifacts are excluded from commits. Screenshots use controlled mock data,
generic paths and reviewed PNGs under docs/images; never capture live credentials
or user conversations. See the [extension guide](docs/VSCODE_GUIDE.md).

CI runs the content policy and tests. Keep credentials, provider logs, operational
state, private development notes and generated artifacts out of contributions.

The Python package is the extension's internal management backend. Keep its
entry points and helper assets working, but distribute the product as a VSIX.
The public tree does not contain a separate desktop app or OS installer.

Build on the target OS/CPU. `npm run test:host` checks the installed VSIX with
the actual VS Code Electron runtime and an isolated extension host; Linux uses
`xvfb-run -a npm run test:host`. See [platform builds](docs/PLATFORMS.md).
Native/core manifests are generated inputs and must remain out of commits.
