# Changelog

## 0.6.0

- Add Ollama and LM Studio text conversations and completed-reply routes to Codex, Claude Code and other local cards.
- Configure loopback server origins, exact model IDs, context/output limits and request deadlines; list models without inference or downloads.
- Store optional LM Studio tokens in VS Code SecretStorage, bound to the saved profile and exact server origin.
- Record one-use request authorization before generation, serialize requests per endpoint within one extension host, and retain explicit cancelled/uncertain states without automatic retry.
- Start a new local conversation with empty context while retaining prior records, usage and group delivery counts; preserve newer boards against legacy fallback.
- Support multiline paste, Unicode input, cancellation and fixed Windows Electron console startup using bundled helpers, with package integrity and installed-host checks on native targets.
- Keep local conversations text-only: file/command tools, batch/role integration, persistent conversation resume and remote endpoints are outside this release.

## 0.5.0

- Build native VSIX packages for Windows x64, glibc Linux x64/ARM64 and macOS Intel/Apple Silicon.
- Select platform-appropriate default shells and Python commands while preserving explicit profiles.
- Preserve POSIX path case and compare managed usage against the frozen physical working directory.
- Verify native binary architecture, required executable helpers, package hashes and isolated Electron PTYs/extension hosts.
- Keep existing Windows-only batch/role body and connection-file removal guards on other platforms.


## 0.4.0

- Coordinate role launch receipt publication before child consumption while retaining one-use nonce checks.
- Inspect saved project data and explicitly reset workspace boards or detach/reconnect configurations.
- Preview and remove selected copies, stopped-session bodies, and protected task/flow bodies while retaining IDs, usage, limits, originals, and backups.
- Remove owned connection files and explicitly selected settings last; query and resume interrupted file work without the configuration file.
- Guide project cleanup through separately reviewed stages with durable receipts, residual counts, and no automatic retry or next-stage apply.
- Restore cleanup receipts across host restarts, protect active/legacy/shared data, and include the project symbol as the extension icon.
