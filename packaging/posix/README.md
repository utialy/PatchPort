# Offline Linux and macOS CLI installation

This distribution uses an explicitly selected Python 3.11+ with `venv` and
`ensurepip`. It does not install Python, use sudo, contact a package index, edit
shell configuration, register a service, authenticate, or start a runner.
The system Python's standard library remains a runtime dependency. A copied venv
interpreter does not make this a self-contained Python distribution.

Extract the archive, inspect the scripts and checksums, then preview:

```sh
sh install.sh /absolute/path/to/python3 --root "$HOME/.local/share/patchport"
```

Copy the `plan_token` from the JSON response and apply that exact plan:

```sh
sh install.sh /absolute/path/to/python3 --root "$HOME/.local/share/patchport" \
  --apply --expect-plan TOKEN_FROM_PREVIEW
```

If inputs, Python, or destination ownership changed, application is refused.
Preview again and review the new result. Preview does not create the install root.
The wheel is installed offline into a final version-specific venv using copies
of its interpreter. A version-specific shell launcher is placed in `ROOT/bin`.
No global command or shell startup file is changed. Use the exact launcher path
printed in the result; paths with spaces and apostrophes are quoted by the launcher.

Use the installed launcher for the common CLI:

```sh
"/install/root/bin/patchport-BUILD-HASH" setup --project "/path/to/project"
"/install/root/bin/patchport-BUILD-HASH" --config "/path/to/project/bridge.json" runner status
```

Follow setup's preview/apply instructions. CLI discovery does not verify login.
Authenticate through each provider's official CLI separately. Only an explicit
runner start can execute queued provider work; installation and setup do not.

Reinstalling the same build with the same Python is unchanged after content checks.
An upgrade installs alongside earlier builds. Stop each selected project's runner,
run setup using the new launcher, review the connection changes, and apply. The
existing project's backup records the replaced integration. The old venv remains.
Do not move a venv: install at a fresh location and reconnect projects instead.

List version records, then preview removal of a selected external launcher:

```sh
sh install.sh /absolute/path/to/python3 --root "$HOME/.local/share/patchport" --list
sh install.sh /absolute/path/to/python3 --root "$HOME/.local/share/patchport" --remove-launcher BUILD-HASH
sh install.sh /absolute/path/to/python3 --root "$HOME/.local/share/patchport" \
  --remove-launcher BUILD-HASH --apply --expect-plan REMOVAL_TOKEN
```

Only the owned, unchanged launcher is removed. Venvs, project code, queues,
answers, backups, receipts, and running processes remain. Unknown projects may
still reference that venv; full runtime reclamation is not provided.
An interrupted removal can be inspected and resumed through a new explicit preview.
Failed installs are retained with `INSTALL_FAILED`/`INSTALLING` records and are
not automatically reused or deleted. There is no automatic retry.

The selected project/install directory itself must not be a symlink. POSIX parent
aliases are normalized to a fixed canonical path; links inside managed state or
installation control paths are rejected. Internally created venv aliases may only
point inside that venv. The install root must be dedicated and empty or already owned.

The archive and wheel SHA-256 values check integrity; they are not signatures.
Build with `packaging/posix/build_bundle.py --wheel WHEEL --build-id ID --output NEW_ARCHIVE`.
Linux/WSL and macOS validation must be reported separately. WSL results do not
establish independent Linux or macOS support. Optional Linux user services are
available through the separate [explicit service workflow](../services/README.md);
installation still never registers or starts one.
