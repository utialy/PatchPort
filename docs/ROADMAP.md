# Roadmap

## Current: VS Code extension 0.4.0

Terminal board, saved jobs/usage, selected-input submission, copy comparison and
promotion/recovery, role/test/archive workflows, explicit session recovery, runtime
integrity checks and project data management are implemented. Guided cleanup
reviews each stage separately and preserves IDs, usage, originals and backups.
See the [walkthrough](VSCODE_GUIDE.md) and [data guide](PROJECT_DATA.md).

## Future work

- External-process session handoff and additional provider adapters.
- Remote/other-OS extension validation and clean-machine/first-use checks.
- Better coverage of provider-reported usage; actual charge enforcement and
  subscription quota reporting are not implemented.
- Separate policy and implementation for removing control ledgers or forensic
  erasure. Selected body cleanup does not reset lifetime claims or deduplication.

Historical/private/shared records and unknown writers remain protected. Do not
interpret a CI matrix as a native provider/UI/service result. Standalone desktop
applications and OS installers are outside this public extension repository.
