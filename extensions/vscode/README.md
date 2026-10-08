# PatchPort for VS Code

Create real AI terminals, route completed replies, inspect durable jobs/usage,
and submit selected working-copy tasks with explicit promotion and recovery.

This is the local Windows x64 0.3.0 extension. It requires a Python 3.11+
executable configured through patchport.corePython. The VSIX includes core
source/workflow helpers; provider CLIs and authentication remain yours.

From the repository root, install the Python source in .venv. Then, from this
directory, run npm ci and npm run package with Node.js 24+. The Windows VSIX
is created in .bridge/artifacts at the repository root. Install it through
VS Code's Install from VSIX action or code --install-extension PATH.

Open a trusted local project and use PatchPort: Open AI Terminal Board.
Project work provides Submit work, Copies & recovery, Roles & archives, and
Sessions & updates. Jobs & usage reads saved tasks/results and scoped usage.

Direct terminals operate on the original project. Batch/role workflows use
selected copies and promote only explicitly reviewed paths. Submitting a task,
starting a runner and promoting files are separate actions. Missing usage is
unknown; delivery/claim/input limits do not enforce provider charges.

Read the [full screenshot guide](https://github.com/utialy/PatchPort/blob/main/docs/VSCODE_GUIDE.md)
or the [bundled guide](docs/USER_GUIDE.md). The toolbar Guide button opens it.
Remote/other-OS extension sessions and external-process handoff are not yet
validated. Previously published Python release assets are unchanged.
