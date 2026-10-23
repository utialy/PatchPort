# Native platform builds

PatchPort 0.6.0 builds separate VSIX packages on the target operating system and
CPU. A Windows package cannot be installed as a Linux or macOS package.

| OS | CPU | VSIX target | Native terminal payload |
| --- | --- | --- | --- |
| Windows | x64 | win32-x64 | node-pty Windows binaries and ConPTY helpers |
| glibc Linux | x64 | linux-x64 | ELF64 pty.node |
| glibc Linux | ARM64 | linux-arm64 | ARM64 ELF pty.node |
| macOS | Intel | darwin-x64 | Mach-O pty.node and executable spawn-helper |
| macOS | Apple Silicon | darwin-arm64 | ARM64 Mach-O pty.node and executable spawn-helper |

Use local VS Code and Python 3.11+. Default provider shells are Windows PowerShell,
Linux Bash and macOS Zsh. Leave patchport.corePython empty for python on Windows
or python3 on Linux/macOS, or choose an absolute interpreter path. Explicit
profiles, authentication and permissions are preserved.

Remote extension hosts, Remote-WSL, Alpine/musl, Windows ARM64 and 32-bit targets
are outside this build scope. Running Linux tests inside WSL is recorded as WSL,
not as a standalone Linux desktop measurement. The Linux CI baselines are Ubuntu
22.04 x64 and Ubuntu 24.04 ARM64; other distributions need compatible libraries.

Batch/role body removal, connection-file removal and their guided cleanup stages
remain Windows-only. Other systems retain the existing platform guards. This
does not disable terminal routing, saved usage, conversation records, selected-copy
review/promotion/recovery or the separate job/role/archive workflows.

## Build and verify

Install the internal Python backend into the checkout's isolated .venv, then use
Node.js 24+ in extensions/vscode:

```sh
npm ci
npm test
npm run package
npm run test:host
```

On Linux, install the compiler, desktop runtime libraries, zsh and Xvfb; use
`xvfb-run -a npm run test:host` for a headless host. The package command selects
the current native target. An explicit mismatched target is rejected. Build
on each target host; renaming a Windows VSIX does not port its native modules.

The verifier checks ZIP integrity, target/CPU, executable macOS helper, native
and Python inventories/hashes, the separate local-helper inventory, and the exact reviewed source icon. The host
check installs the package into an isolated profile, runs its native PTY inside
the actual VS Code Electron runtime, activates the installed extension and opens
the board. It also runs the installed local helper with the bundled Electron
runtime through eight Ollama/LM Studio mock scenarios, verifies actual helper
termination, and checks local profile discovery without starting a model session.
Windows uses a fixed system console parent and explicit console TTY handles;
users do not need an external Node installation for local chat.
Tests use controlled mock providers; no live AI invocation is required.

See [validation](VALIDATION.md) for measured results. A configured CI matrix alone
is not evidence that its native jobs passed. Build outputs and test profiles are
excluded from commits; the workflow publishes generated VSIX files as CI artifacts.

All five native jobs passed their actual installed-host checks in the
[measured 0.5.0 CI run](https://github.com/utialy/PatchPort/actions/runs/39257939497).
Tests used controlled mock providers, not live CLI authentication or billable calls.
