# Validation

## Current extension 0.4.0

The product is the English Windows x64 VS Code extension. Tests use new temporary
projects and controlled mock providers; no real provider call is part of the
release checks. Backend tests require an isolated installed package as well as
the source tree. Generated runtimes, virtual environments, logs and VSIX artifacts
are excluded from commits.

Run the independent checks described in [CONTRIBUTING](../CONTRIBUTING.md): Python
unittest discovery, public tree/staged policy, extension type checking/tests, and
VSIX packaging. The Windows extension requires Python 3.11+ and Node.js 24+ for
source builds. Its VSIX includes the core/helpers, not the interpreter.

Coverage includes explicit project selection, copy and body cleanup, writer/SQL
guards, response-loss receipt lookup, same-operation I/O resume, config-free
connection results, usage/ID/original/backup retention and host restoration.
Release-local measurements are recorded after execution below.

## Measured on Windows, 2026-10-18

- Python 3.14.6: 535 backend/policy tests, 534 passed and one platform skip.
- Node.js 24.14.0: extension type checking/build and all 59 tests passed.
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

## Platform boundaries

The extension currently supports validated local Windows x64 use. Remote/WSL/macOS
extension sessions and native provider/service/first-use checks on other systems
are not implied by portable core tests or a CI matrix. Windows-specific deletion
tests skip on other platforms. Missing usage remains unknown.

No cleanup check establishes forensic disk erasure or deletion of provider-side
history. Historical/private/shared data, unknown writers, recovery evidence and
service registrations remain protected. Provider authentication and operational
runtime/service installations are not changed by release validation.
