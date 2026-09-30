# Project setup

Current source builds provide `agent-bridge setup` and `python -m agent_bridge setup`. The previously published 0.1.0a2 release assets do not include the wizard; install the current source version.

## Interactive setup

```sh
agent-bridge setup
agent-bridge setup --project /path/to/project
```

Choose a project, select installed provider CLIs, and choose project-relative input paths. Review the file actions and confirm before applying. Cancellation before application leaves the target unchanged. The wizard displays selected AGENTS.md and CLAUDE.md files so you can review which project rules will be shared.

For an empty project, choose a small PATCHPORT_START.md starter file. It is written only when you apply. A missing project directory can be created explicitly if its parent already exists. Existing bridge.json files are reused without changing their endpoints, permissions, or selected paths.

Interactive path lists are comma-separated. For filenames containing commas, use repeated --include options in non-interactive mode.

## Non-interactive preview and application

```sh
agent-bridge setup --non-interactive --project /path/to/project --provider codex --include src
agent-bridge setup --non-interactive --project /path/to/project --provider codex --include src --apply
agent-bridge setup --non-interactive --project /path/to/new-project --create-project --starter --provider claude
```

Without --apply, these commands print a JSON preview. Repeat --provider to select both codex and claude, and repeat --include or --writable for multiple paths. If --writable is omitted, it uses the input scope. This controls only explicitly requested promotion to the original project; it is not a provider sandbox or permission to apply changes automatically.

Use --codex PATH or --claude PATH to choose a specific native executable. Multiple detected installations require a selection rather than silently choosing one. Windows shell shims (.cmd, .bat, .ps1) are not executed. A CLI .js entry point may be selected when a native Node executable is available. Candidate discovery does not verify publisher identity, authentication, or version compatibility. --python selects an interpreter with the current package installed.

When bridge.json already exists, omit provider, input, writable, and starter options. Run setup with only the project and any runtime/application options; the existing configuration is preserved. To resolve a conflicting installation deliberately, review the separate [connect workflow](ROLES.md#connect-another-project); setup does not adopt conflicting files automatically.

## Results and boundaries

- PREVIEW means the plan has not been applied. READY means local connection files are ready, not that authentication or a model request succeeded.
- NEEDS_CLI reports missing or unsupported command resolution. CONFLICT preserves existing files. Errors before writing leave the target unchanged.
- PARTIAL means application started but failed or was interrupted. Inspect the reported backup directory before making another plan. Files already written are not automatically deleted or retried.
- Before writing, setup rechecks input hashes, configuration, the installation manifest, and CLI resolution. Changed inputs require a new preview. Identical pre-existing user files are not automatically claimed as managed installation files.
- Known credential filenames, authentication directories, links, integration files, and runtime state are not eligible inputs. This is a path check, not a general secret-content scanner or ongoing monitor of later files.
- New configurations use read-only review settings and the provider's default model. Existing endpoint settings are not changed.

Setup does not log in, read provider credential stores, submit tasks, call a provider, or start a runner. Applying a connection does run a local import check with the selected Python interpreter. Authentication remains NOT_CHECKED and invocation remains NOT_RUN. Authenticate through the official provider CLI and start the runner separately. Provider usage may incur charges.

Both setup and connect use --project rather than an alternative global --config. The existing doctor command still checks only command resolution. See [validation](VALIDATION.md) for tested environments and [roles](ROLES.md) for consulting another agent after connection.
