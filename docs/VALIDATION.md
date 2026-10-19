# Validation

## Current extension 0.5.0

The product is the English native VS Code extension. Tests use new temporary
projects and controlled mock providers; no real provider call is part of the
release checks. Backend tests require an isolated installed package as well as
the source tree. Generated runtimes, virtual environments, logs and VSIX artifacts
are excluded from commits.

Run the independent checks described in [CONTRIBUTING](../CONTRIBUTING.md): Python
unittest discovery, public tree/staged policy, extension type checking/tests, and
VSIX packaging. The native extension requires Python 3.11+ and Node.js 24+ for
source builds. Its VSIX includes the core/helpers, not the interpreter.

Coverage includes explicit project selection, copy and body cleanup, writer/SQL
guards, response-loss receipt lookup, same-operation I/O resume, config-free
connection results, usage/ID/original/backup retention and host restoration.
Release-local measurements are recorded after execution below.

## Native measurements, 2026-10-19

[CI for the measured source](https://github.com/utialy/PatchPort/actions/runs/39257939497)
passed all 17 jobs: 12 core OS/Python combinations and five native extension jobs.
Each native job built its own VSIX, checked binary architecture and hashes,
installed it into an isolated profile, ran its PTY inside VS Code Electron,
activated the installed extension and opened the board.

| Native target | Extension tests | Installed host |
| --- | --- | --- |
| win32-x64 | 63 passed, 1 POSIX-only skip | VS Code 1.142.0, Electron 43.7.7 |
| linux-x64 | 54 passed, 10 Windows-only skips | VS Code 1.142.0, Electron 43.7.7 |
| linux-arm64 | 54 passed, 10 Windows-only skips | VS Code 1.142.0, Electron 43.7.7 |
| darwin-x64 | 54 passed, 10 Windows-only skips | VS Code 1.142.0, Electron 43.7.7 |
| darwin-arm64 | 54 passed, 10 Windows-only skips | VS Code 1.142.0, Electron 43.7.7 |

Native jobs also passed three binary-architecture regressions. All five VSIX
files and host receipts were downloaded and rechecked against the measured Git
source: 42 Python files each, target-specific native inventory, executable macOS
helper and the exact 1024x1024 source icon. No retired installer modules occur.
Windows and WSL Linux x64 also passed local isolated-host checks. WSL results are
separate from the independent Linux CI jobs. All providers in these checks were
controlled mocks; real AI calls were zero.

Earlier macOS candidate runs exposed helper executable-mode preparation and a
test tool's assumed app executable name. Build-time native preparation and app
metadata resolution fixed those issues before the measured successful run.

## Earlier 0.4.0 Windows measurements, 2026-10-18

- Python 3.14.6: 536 backend/policy tests, 535 passed and one platform skip.
- Node.js 24.14.0: extension type checking/build and all 60 tests passed.
- VS Code 1.142.0 with a fresh isolated profile: actual controller and bundled
  core, English Chrome views at 1500x1000 and 1000x760, four separately reviewed
  cleanup stages, partial metadata failure after configuration removal,
  receipt rediscovery/same-operation resume, and fresh-host restoration passed.
- Original files, another workspace, connection backups, positive claim use
  (3 of 9) and group counts/rounds were preserved. No real AI call was made.
- English Windows x64 VSIX: 123 entries, 42 Python runtime files; manifest hashes,
  exact source icon bytes, absence of retired installer modules, isolated install,
  activation and configuration-free cleanup result lookup passed.
- Public tree/encoding/local links and nine focused policy tests passed. These
  local results do not claim unexecuted remote CI jobs or native provider checks.
- Follow-up regressions use a real Windows 8.3 path for receipt lookup and a
  selected-folder alias for PTY cwd validation, while rejecting other folders.
  Writer/runner tests wait for actual shutdown; native integration test workers
  run sequentially without removing their explicit concurrency checks.
- A real role-job regression forces parent/child start-lock contention and checks
  one-use acceptance, both mock roles, final writer closure and payload removal.

## Platform boundaries

Native build and installed-host checks cover the five local targets above.
Remote extension hosts, Remote-WSL, Alpine/musl and native provider/service/
clean-machine first-use checks remain outside these measurements. Windows-only
batch/role body removal, connection-file removal and their guided stages retain
their platform guards. Missing usage remains unknown. See [platforms](PLATFORMS.md).

No cleanup check establishes forensic disk erasure or deletion of provider-side
history. Historical/private/shared data, unknown writers, recovery evidence and
service registrations remain protected. Provider authentication and operational
runtime/service installations are not changed by release validation.
