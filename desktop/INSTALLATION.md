# Windows installation candidate

`PatchPortSetup.exe` is an offline, per-user development installer. It carries its
own GUI environment and a verified payload containing a separate CPython core and
the PatchPort management application. It does not need system Python to launch.
Clean-machine installation, signing, and release trust remain unverified.

Run the installer, choose a dedicated folder, and select **Preview install / upgrade**.
Review the version, destination, size, and optional version-specific shortcut, then
select **Apply preview**. Installation does not change PATH, install providers,
authenticate, register services, start a runner, or invoke AI. No administrator
prompt is requested. An existing folder must be empty or already owned by this
installer. Junctions, symbolic links, hard-linked files, and network paths are rejected.

Each build occupies `INSTALL_ROOT/versions/VERSION-PAYLOAD_HASH/`. A successful
installation has a `.patchport-install.json` ownership record with file hashes.
The optional `PatchPort-VERSION-PAYLOAD_HASH.lnk` shortcut is created inside the
install root. Open that shortcut or the displayed `gui/PatchPort.exe` path.
Identical reinstallations leave files unchanged. Different builds install side by
side; old versions are never overwritten or automatically removed.

To move a project to the new version:

1. Stop the project's runner through its current management app. Wait for it to stop.
2. Open the new management app and select that project explicitly.
3. Preview its connection, review the file changes, and apply. The project launcher
   will use the new core; a backup records the old integration files and manifest.
4. Refresh status and start the new runner only when you want queued work to run.

An active runner or unconfirmed start blocks reconnection. Reconnection locks out
concurrent managed starts and foreground runners during the file update. It may
create lock files in the existing project's state directory, but does not initialize
or rewrite its queue. Input files, answers, pause state, call limits, and usage remain.
Projects are moved independently; a failure in one project does not roll back others.

To remove a version's **GUI**, choose **List installed versions**, select the version,
and preview GUI removal. The preview lists every file to remove. Close that version's
management window before applying. The remover opens all candidates exclusively
and rechecks their hashes before deleting any file. Modified, linked, or busy files
block removal. Unowned files are retained, and no recursive directory deletion occurs.

**Core runtimes are retained even after GUI removal.** Projects outside the registry
may still reference them. Core files, notices, package manifests, ownership records,
project code, queues, answers, requests, backups, and app project lists are preserved.
The installer does not provide full runtime reclamation or remove itself.

A failed install is marked `INSTALL_FAILED` (or `INSTALLING` after interruption).
It is not silently reused or deleted. Inspect the recorded location and failure.
A partially removed GUI is marked `REMOVE_FAILED` or `REMOVING`; inspect it and
create a new removal preview to continue explicitly. No operation retries itself.
Plans are memory-only, expire after ten minutes, and are consumed by Apply even when
it fails. Closing the installer during a pending operation is disabled.

For a source run:

```powershell
$env:PYTHONPATH = 'C:/checkout/src'
python -m desktop.installer --payload C:/build/payload.zip --install-root C:/test/PatchPort
```

For a build, use an isolated environment containing the pinned PyInstaller version:

```powershell
python packaging/windows/build_installer.py --candidate C:/reviewed/bundle --version 0.1.0a2-desktop-b3.1 --output C:/new/build
```

The output must not already exist. The candidate must contain only the reviewed
`core`, `gui`, `notices`, and `package.json` files. The builder excludes Python caches,
rejects known private paths, and checks the complete archive inventory and SHA-256
values. This is an integrity check, not a digital signature or a complete secret scan.
Review all bundled licenses before publication.

Follow the [component notice checklist](../packaging/windows/NOTICE_CHECKLIST.md)
before building a payload. Include the reviewed notice directory in the payload
and an identical copy beside the installer so it is available before installation.

The Windows implementation uses [exclusive file sharing](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
and [handle-based file disposition](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_disposition_info)
for GUI removal. Payload embedding follows the [PyInstaller data-file mechanism](https://pyinstaller.org/en/stable/spec-files.html).

Validation distinguishes source Tk interaction, installed frozen GUI execution,
and frozen installer button interaction. The current Windows tests exercised real
installation, shortcuts, upgrade, reconnection, removal protection, and mock requests;
frozen installer clicks, other DPI settings, accessibility, clean VMs, and other
operating systems remain unverified. No actual provider calls were made.
