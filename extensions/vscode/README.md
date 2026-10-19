# PatchPort for VS Code

**Put your AI terminals on one board. Connect their replies. Review their changes.**

PatchPort opens real **Codex CLI** and **Claude Code** terminals inside VS Code.
Type and approve requests in each CLI as usual, then connect an output dot to
another terminal's input dot to forward completed replies. Your existing CLI
authentication and permissions stay in use.

![Managed AI terminals and a directed route](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/terminal-board.png)

Screenshots show an earlier English Windows build with controlled demo data,
not live provider responses or account usage. This document describes source 0.5.0.

## Before you start

Install **Python 3.11+** and the official provider CLI(s), then finish each CLI's
own login. Open a trusted local project folder in VS Code. The VSIX includes the
Python core and workflow helpers; Python and the provider CLIs are separate.
Node.js and Git are needed only for building from source.

If Python is not found, set `patchport.corePython` to its executable in Settings.
Empty uses `python` on Windows or `python3` on Linux/macOS. Finish managed
terminal work before reloading the window after a setting change.

## Your first connection

1. Run **PatchPort: Open AI Terminal Board** from the Command Palette.
2. Click **+ AI terminal**, select a profile/project/group, then **Start terminal**.
   Add a second AI terminal in the same project and group.
3. Complete startup and approval prompts. Click **Ready to receive** only when
   the CLI input is empty and waiting for a new request.
4. Click the first card's right **Output port**, then the second card's left
   **Input port**. Begin with one directional arrow.
5. In **Groups > Settings**, choose small delivery/hop limits, such as `2` each.
   Click the group's **Resume routes**, ask the first AI a question, then click
   outside its terminal. Inspect **Delivery history** when its reply finishes.

An arrow forwards subsequent completed replies, not the entire conversation or
raw terminal log. A return message needs a return arrow. **Pause routes** pauses
delivery; **Stop** ends a terminal. Provider requests can consume your allowance.

## Choose how files are changed

| Workflow | Behavior |
| --- | --- |
| Terminal board | Real CLI sessions in the original project directory, under the CLI's permissions |
| Project work > Submit work | Selected working copies; submit, start the runner, inspect the diff and explicitly promote approved paths |
| Roles & archives | Developer/test/reviewer workflow with saved answers, test logs and evidence |

For copy work, install the repository's Python backend in a separate environment,
run project setup and select that same Python in `patchport.corePython`.
Follow the [copy-work tutorial](https://github.com/utialy/PatchPort/blob/main/docs/VSCODE_GUIDE.md#request-a-change-in-a-working-copy).

## Other useful panels

- **Jobs & usage**: retained results, sessions and reported consumption. Unknown
  means unavailable, not zero; delivery/claim limits are not spending caps.
- **Copies & recovery**: compare proposed edits, apply selected paths and preview
  recovery from compatible backups.
- **Sessions & updates**: restore paused boards, explicitly resume eligible
  provider conversations and check runtime integrity.
- **Data & settings**: preview project configuration changes and selected data
  cleanup. IDs, usage, originals and backups remain protected.

Failed/interrupted provider work is not retried automatically. Restoring a board
does not restart AI work or resend uncertain deliveries.

## Platforms and help

Native packages target local **Windows x64**, **glibc Linux x64/ARM64**, and
**macOS Intel/Apple Silicon**. Remote extension hosts, Remote-WSL, Alpine/musl
and Windows ARM64 are outside the current supported scope. Ordinary-task/role-flow
body removal, connection-file removal and their guided cleanup stages remain
Windows-only. Dedicated local LLM provider support is planned, not implemented.

Open **Guide** on the board for the [bundled walkthrough](docs/USER_GUIDE.md).
The [online guide](https://github.com/utialy/PatchPort/blob/main/docs/VSCODE_GUIDE.md)
covers examples and troubleshooting; the
[platform guide](https://github.com/utialy/PatchPort/blob/main/docs/PLATFORMS.md)
covers native requirements. Source and build instructions are on
[GitHub](https://github.com/utialy/PatchPort).
