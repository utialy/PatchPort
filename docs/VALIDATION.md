# Validation

The public checkout was validated on 2026-09-27 using a newly built wheel in separate virtual environments. The test folders contained helpers, skills, scripts, and tests, but no runtime source tree. The imported package came from site-packages and matched the public runtime bytes.

| Environment | Unit tests | Entry points and console task |
| --- | --- | --- |
| Windows 11 / Python 3.14.6 | 175 passed, 1 skipped; 42.383 seconds | Module and native console entry points passed; mock submit/run/result passed |
| Ubuntu on WSL 2 / Python 3.14.4 | 176 passed, no skips; 20.217 seconds | Module and console entry points passed; mock submit/run/result passed |

The Windows skip was the permission-dependent symlink test. Mock reparse-point coverage still ran. The suite includes seven public-release policy tests in addition to the runtime and helper regression tests.

The public preparation changed documentation, comments, packaging metadata, and release policy. Existing Python runtime/helper/test syntax trees were preserved; escaped Unicode fixtures retain their original values. No real provider calls were made for this public-checkout validation.

The source distribution includes all project helpers, launcher templates, skills, and tests. The wheel includes the MIT license and runtime entry point. Both archives were checked for private state and handoff files.

CI configures Windows, Ubuntu, and macOS with Python 3.11-3.14. These jobs have not been run on a remote service for this initial public commit. Configuring a job does not establish that it has passed. This public snapshot has not been separately tested on macOS, an independent Linux host, or Python 3.11-3.13. Real provider calls are separate from mock-provider unit tests.
