# Validation

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
