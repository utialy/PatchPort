# Roadmap

## Current: VS Code extension 0.5.0

Terminal board, saved jobs/usage, selected-input submission, copy comparison and
promotion/recovery, role/test/archive workflows, explicit session recovery, runtime
integrity checks and project data management are implemented. Guided cleanup
reviews each stage separately and preserves IDs, usage, originals and backups.
See the [walkthrough](VSCODE_GUIDE.md) and [data guide](PROJECT_DATA.md).

Native packages cover Windows x64, glibc Linux x64/ARM64 and macOS Intel/Apple
Silicon, with platform-specific terminal payloads and isolated installed-host
checks. See [platform builds](PLATFORMS.md). Batch/role body and connection-file
removal retain their Windows-only guards.

## Future work

- External-process session handoff and additional provider adapters.
- Local LLM provider support: local runtime/endpoint and model selection,
  completion/cancellation signals, and explicit usage coverage. This is a planned
  item only; no local-model adapter or model download is implemented here.
- Remote extension hosts and native provider/clean-machine first-use checks.
- Better coverage of provider-reported usage; actual charge enforcement and
  subscription quota reporting are not implemented.
- Separate policy and implementation for removing control ledgers or forensic
  erasure. Selected body cleanup does not reset lifetime claims or deduplication.

Historical/private/shared records and unknown writers remain protected. Do not
interpret a CI matrix as a native provider/UI/service result. Standalone desktop
applications and OS installers are outside this public extension repository.
