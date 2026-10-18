# Changelog

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
