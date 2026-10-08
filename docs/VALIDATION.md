# Validation

## Current source: VS Code and guarded project workflows

On 2026-10-08 the independent public checkout was built and installed in a fresh
Windows x64 environment. Python 3.14.6 ran 427 tests: 419 passed and eight
platform/permission-specific checks were skipped. The suite includes nine public
content-policy checks. Unicode fixtures retain their original values through
source escapes; changed shared Python files were compared with the development
source's parsed syntax. Read-only, queue/claim, runner, interactive ledger,
input/provenance, promotion/recovery, role/test/cleanup/archive and service
simulation checks passed. A polling fixture waits for the durable role-job
completion receipt before reading its final result, avoiding Windows atomic-file
replacement/read races.

The English VS Code 0.3.0 extension passed type checking and 30 tests on Windows,
including actual mock PTYs, profile functions, routed responses, recording failure
protection, one-use core plans, first-queue ledger retention, Windows path casing,
board snapshots and runtime integrity. Node.js 24.14.0 built a Windows x64 VSIX
using VSCE 4.0.0. The locked dependency audit reported zero vulnerabilities after
updating this build-only tool. An isolated VS Code 1.140.0 installation was checked
for core access, unsaved-editor protection, selected promotion/recovery and its
English bundled guide.

Seven screenshots use the actual compiled English Webview rendered in Chrome
with isolated mock task/role/ledger data and generic paths. They are examples,
not live provider responses or account usage. Screenshot/link/content policy
checks passed. No real AI provider was called for this public update.

Other-OS/remote extension execution and current live provider formats/authentication
were not measured by these Windows/mock checks. The additional Windows extension
CI job is configured; hosted results must be inspected independently. Existing
0.1.0a2 release assets are unchanged. The historical results below describe their
original release/source revisions.

## Current source changes: guided project setup

On 2026-09-30, a wheel built from the public checkout was installed into an isolated Windows environment. Python 3.14.6 ran 269 tests: 266 passed and three permission-dependent symlink tests were skipped (83.947 seconds). Public release policy and local documentation link checks passed. Tests cover CLI discovery without execution, input/configuration changes after preview, ownership preservation, cancellation, partial writes, and canonical project paths.

Both the module and native console entry points connected temporary new and existing projects outside the checkout. Previews left target files unchanged, application created local connection files, and repeat application was unchanged. No queue or runner was created, and no real provider was called. Imported runtime files came from site-packages and matched the reviewed public source.

Linux and macOS were not rerun locally for this update. Parent-alias handling has a simulated regression check; it is not a direct macOS measurement. Remote CI results must be checked separately. The published 0.1.0a2 release assets remain unchanged; use a current source build for setup.

## Current source changes: connection CLI and reviewed summaries

On 2026-09-28, a wheel built from the updated public source was installed into a fresh Windows environment. Python 3.14.6 ran 245 tests: 243 passed and two permission-dependent symlink tests were skipped (88.262 seconds). The public policy checks passed. The native console preview and module-based connection installation also passed from a temporary project outside the checkout, without creating a queue or calling a provider.

The wheel contains the summary runtime and 12 connection assets. The source archive includes setup.py and the summary guide. Packaging checks found no private inquiry, handoff, or operational files. No real provider calls were made. Linux and macOS were not rerun locally for this public import; the previous release results below do not establish coverage of these changes. These source changes have not been published as replacement 0.1.0a2 release assets.

## Published 0.1.0a2

Version 0.1.0a2 was validated on 2026-09-27 using a wheel installed in fresh virtual environments. Test folders contained helpers, skills, scripts, and tests, but no runtime source tree. The imported runtime came from site-packages and matched the public source bytes.

| Environment | Unit tests | Entry points and console task |
| --- | --- | --- |
| Windows 11 / Python 3.14.6 | 219 passed, 2 skipped; 82.371 seconds | Module and native console help; mock submit/run/result passed |
| Ubuntu on WSL 2 / Python 3.14.4 | 221 passed, no skips; 34.723 seconds | Module and console help; mock submit/run/result passed |
| Ubuntu on WSL 2 / Python 3.11.16 | 221 passed, no skips; 34.362 seconds | Module and console help; mock submit/run/result passed |

Windows skips were permission-dependent symlink tests. Mock reparse-point coverage still ran. The suite includes seven public-release policy tests in addition to the runtime and helper regression tests. Archive coverage includes unchanged originals/queues, shared and private queues, retention and recovery protection, binary/empty evidence, publication failures, explicit continuation, and changed/corrupt evidence rejection.

The README's account-free example was executed on Windows. It creates a local command task without provider authentication or network calls. No real provider calls were made for this release validation.

The source distribution includes all project helpers, launcher templates, skills, and tests. The wheel includes the MIT license and both runtime entry points. Archive contents were checked for private state and handoff files. A documentation-only rebuild is checked against the tested runtime/helper/test bytes; final package installation and the documented example are checked separately.

The [first remote CI run](https://github.com/utialy/PatchPort/actions/runs/36303720601) passed all 12 jobs for commit `987d5c5`: Windows, Ubuntu, and macOS runners with Python 3.11, 3.12, 3.13, and 3.14. Each job installed the package, checked the public release policy, and ran the unit suite. This establishes hosted CI coverage, including macOS; it does not establish real provider authentication or behavior on those systems. Real provider calls remain separate from mock-provider unit tests.
