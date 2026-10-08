# PatchPort management window

This is the Windows management UI development candidate, not an installer or a
signed release. The GUI uses a separate installed core Python through inherited
JSON pipes. The core package does not import Tk or require desktop dependencies.

An offline installation candidate is now available separately. See
[installation and retention behavior](INSTALLATION.md) for upgrades, project
reconnection, and removal of owned GUI files while retaining core runtimes.

This Tk window manages the existing database-backed core; it is separate from
the VS Code interactive terminal extension. It provides project setup, runner
controls, call limits, recent tasks, and saved results. It does not expose a
dedicated token/cost dashboard or the full review/apply/recovery UI. Use the
core CLI for those existing functions. VS Code 0.3.0 also uses the management API,
adds guarded project workflows, and keeps interactive records beside batch tasks.

For source development, run from the repository root with a Python that includes
Tk. The selected core Python must already contain the current runtime package:

```powershell
python -m desktop.app --core C:/path/to/runtime/python.exe
```

`--source C:/path/to/repo/src` explicitly selects development core sources; the
selected Python still needs the installed runtime for connection validation.
`--registry C:/path/to/projects.json` selects an alternate project list. The default
is `%LOCALAPPDATA%/PatchPort/projects.json`. Only paths explicitly selected in the
window are registered. No disk scan, provider invocation, or runner start happens
on launch.

1. Open a project, or choose a parent folder and new folder name.
2. For a new connection, choose CLI command names or executable paths and input files, or a
   starter file. Enter relative paths on separate lines. Existing `bridge.json`
   settings are preserved. Writable paths describe explicit promotion to the
   original project; they do not restrict the AI's work inside the copy.
   For a profile function or alias such as `claude-1`, select `powershell`, `pwsh`,
   `bash`, or `zsh` instead of `direct`. Optional shell and profile paths select
   the executable and startup file; an explicit profile path must be absolute.
   Preview finds the shell but does not load profiles or verify their commands.
   Existing endpoint commands remain in `bridge.json`; stop the runner before editing them.
3. Preview the connection and review the file changes. Apply explicitly, or discard.
   Editing inputs or switching projects invalidates the preview. Expired plans
   require a new preview. Connection does not authenticate or make an AI call.
4. Copy the first request into your existing AI conversation. Sign in through each
   official CLI if needed; finding its executable does not verify authentication.
5. Refresh status before starting a runner. Starting or resuming can execute queued
   provider work. Set a lifetime call cap when needed; changing it never resets usage.
6. Stop after active work completes, or explicitly cancel active work and stop.
   Closing the window leaves the runner running. Refresh status or select **Refresh
   tasks** to list the latest 20 tasks. Select a row and choose **Read selected result**,
   double-click it, or press Enter to read its saved answer. **Read by ID** remains
   available for older tasks. These actions make no provider calls or queue changes.

Use `--project C:/path/to/project` to open an existing project directly. A connected
project opens on the status/results tab and reads its status and recent task list.
It does not start or resume a runner, submit work, or load provider profiles. Changing
projects or losing the connection clears the previous result selection.

The UI serializes operations. A missing response or a 45-second response deadline
does not mean a mutation failed or rolled back. The UI closes its core input, expires
local plans, and never retries a mutation. Inspect state before reconnecting. A
still-running core must exit before reconnecting; it is not forcibly terminated.
The core also expires idle sessions after five minutes.

Build the GUI using the isolated build environment and `desktop/launch.py`:

```powershell
python -m PyInstaller --onedir --windowed --paths . --name PatchPort desktop/launch.py
```

Place the generated `PatchPort` folder at `BUNDLE/gui`, alongside the separately
assembled `BUNDLE/core` and `BUNDLE/package.json`. Set the manifest's `gui` to
`gui/PatchPort.exe`; its `core_python` must identify a file inside the bundle.
The packaged GUI rejects development core/source overrides. Follow the runtime
and license checks in [the packaging guide](../packaging/windows/README.md).

Windows Python 3.14.6 tests cover real pipes, previews, controls, runner survival,
and normal frozen window launch/close. Source Tk interaction was also checked
against the bundled isolated core. Frozen button interaction, other DPI settings,
screen readers, clean machines, signing, installation lifecycle, and other OSes
remain separate validation work. No actual provider calls were made.
