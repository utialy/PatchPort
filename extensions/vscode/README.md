# PatchPort for VS Code

English UI, version **0.4.0**, local Windows x64 VS Code. Run real AI terminals,
route completed replies, inspect saved jobs/usage, and review selected-copy work.

The VSIX includes the standard-library Python core and fixed helpers. Set
**patchport.corePython** to a Python 3.11+ executable. Provider CLIs, authentication
and permissions remain yours. Node.js 24+ is required to build from source.

From the repository root, prepare the internal backend with `py -3 -m venv .venv`
and `.venv/Scripts/python.exe -m pip install .`. From `extensions/vscode`, run
`npm ci` and `npm run package`. Install the generated
`.bridge/artifacts/patchport-0.4.0-win32-x64.vsix` through **Install from VSIX**.

Open **PatchPort: Open AI Terminal Board** in a trusted local folder. Project work
includes Submit work, Copies & recovery, Roles & archives, Sessions & updates,
and Data & settings. Guided cleanup previews/applies each conversation/task/flow/
connection stage separately, preserves usage/IDs/originals/backups, and supports
receipt lookup and remaining-I/O resume after configuration removal.

Direct terminals use the original project; batch/role workflows use selected
copies. Submission, runner start and promotion are separate. Missing usage is
unknown, and claim/delivery/input limits do not enforce actual provider charges.
No automatic retry or next-stage cleanup apply is performed.

Read the [bundled guide](docs/USER_GUIDE.md) or
[online walkthrough](https://github.com/utialy/PatchPort/blob/main/docs/VSCODE_GUIDE.md).
Remote/other-OS extension sessions and external-process handoff remain unvalidated.
The public product is the VS Code extension; no separate desktop app/OS installer
is included. A local build does not imply a Marketplace publication.
