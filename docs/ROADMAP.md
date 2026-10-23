# Roadmap

## Current: VS Code extension 0.6.0

Terminal board, saved jobs/usage, selected-input submission, copy comparison and
promotion/recovery, role/test/archive workflows, explicit session recovery, runtime
integrity checks and project data management are implemented. Guided cleanup
reviews each stage separately and preserves IDs, usage, originals and backups.
See the [walkthrough](VSCODE_GUIDE.md) and [data guide](PROJECT_DATA.md).

Native packages cover Windows x64, glibc Linux x64/ARM64 and macOS Intel/Apple
Silicon, with platform-specific terminal payloads and isolated installed-host
checks. See [platform builds](PLATFORMS.md). Batch/role body and connection-file
removal retain their Windows-only guards.

Ollama and LM Studio support loopback text conversations, exact model selection,
read-only connection checks, optional secret storage, bounded generation,
cancellation and completed-reply routes. Local models have no file/command tools.
Installed-host tests use mock servers; real-model compatibility depends on the
runtime, model and settings actually tested.

## Future work

- External-process session handoff and additional provider adapters.
- Validate real local runtime/model combinations with an explicit request limit.
- Add an explicitly scoped file-analysis workflow, then separately design local
  coding-agent tools and batch/role integration with permissions and review.
- Persistent local conversation resume, remote endpoints and richer input formats.
- Remote extension hosts and native provider/clean-machine first-use checks.
- Better coverage of provider-reported usage; actual charge enforcement and
  subscription quota reporting are not implemented.
- Separate policy and implementation for removing control ledgers or forensic
  erasure. Selected body cleanup does not reset lifetime claims or deduplication.

Historical/private/shared records and unknown writers remain protected. Do not
interpret a CI matrix as a native provider/UI/service result. Standalone desktop
applications and OS installers are outside this public extension repository.
