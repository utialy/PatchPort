# Windows packaging experiment

Before building a candidate for review, follow the [notice checklist](NOTICE_CHECKLIST.md)
and inspect both frozen executables and the embedded payload. Keep artifact checks,
installed-core mock execution, and actual GUI interaction as separate results.

The B-3 offline installer is built with `build_installer.py` from a reviewed desktop
candidate. It embeds a hashed payload and installs versioned directories without
replacing existing runtimes. See [installation behavior](../../desktop/INSTALLATION.md).
The installed GUI can be removed explicitly; core runtimes and projects are retained.
This remains a development candidate, not a signed or published installer.

The B-3 offline installer is built with `build_installer.py` from a reviewed desktop
candidate. It embeds a hashed payload and installs versioned directories without
replacing existing runtimes. See [installation behavior](../../desktop/INSTALLATION.md).
The installed GUI can be removed explicitly; core runtimes and projects are retained.
This remains a development candidate, not a signed or published installer.

The subsequent management UI candidate is documented in
[desktop/README.md](../../desktop/README.md). Its frozen entry is
`desktop/launch.py` with `--paths . --name PatchPort`; the core layout is unchanged.
The B-2 source UI was exercised against the bundled core, and the management EXE
passed window creation and normal close on the current Windows host. This does not
establish frozen button interaction, clean-machine support, signing, or installation
lifecycle validation. The original A experiment below remains separate evidence.

This is the 004-A development experiment, not an installer or a supported desktop release.
The frozen diagnostic GUI and the isolated CPython core are separate processes.
The core uses the official Python 3.14.6 x64 embeddable archive, verified against
the SHA-256 published on python.org. It does not install pip or use user site packages.

1. Create a separate build environment and install the versions recorded in the QA build requirements.
2. Build a current project wheel using the existing package build process.
3. Run `assemble_probe.py --runtime ARCHIVE --wheel WHEEL --output NEW_DIRECTORY`.
4. Build `desktop/packaging_probe.py` with PyInstaller 6.22.3, using `--onedir --windowed --name PatchPortProbe`.
5. Copy the generated `PatchPortProbe` directory to `NEW_DIRECTORY/gui`.
6. Run `verify_probe.py --candidate NEW_DIRECTORY --report REPORT.json` on a machine permitted to execute the generated application.

The verifier copies the candidate to a retained temporary directory outside the checkout,
removes Python environment settings and restricts PATH to Windows System32. It verifies
setup, the project launcher, an explicit runner, one local mock response and runner shutdown.
It then opens the diagnostic GUI, which checks the core and exits automatically.
No provider CLI, authentication, service registration, system Python update, or deployment is performed.

On 2026-10-01 the core sequence passed, but Windows Application Control blocked the unsigned
GUI with error 4551 (Code Integrity events 3077/3033). Do not change policy, rename/move the
binary to evade enforcement, or substitute source execution for an executable test.
The user later reported a successful GUI/core smoke run after disabling Smart App Control:
the frozen window was mapped and the bundled core reported isolated mode. This is user-provided
evidence, not a repeated automated run. Full visual QA, clean-machine installation and execution
under enabled application control remain unverified.

The candidate is about 51 MiB before installer compression; exact bytes and file hashes are
recorded under `.bridge/desktop-qa/`. The retained temporary project contains mock data only.
Licenses and all bundled components must be reviewed again before release.
