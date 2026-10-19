<p align="center">
  <img src="https://github.com/user-attachments/assets/888a1b3f-a3a0-4770-9742-15e044627587" alt="PatchPort logo" width="200" height="auto">
</p>

# PatchPort for VS Code

**Put your AI terminals on one board. Connect their replies. Review their changes.**

PatchPort brings real **Codex CLI** and **Claude Code** terminals into VS Code.
You can type into each terminal as usual, then draw an arrow to send one AI's
completed reply to another. Your existing CLI login and permission settings stay
in use.

[Install from Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=utialy.patchport)
| [Step-by-step guide](docs/VSCODE_GUIDE.md)
| [Build from source](docs/VSCODE_GUIDE.md#build-from-source)
| [Roadmap](docs/ROADMAP.md)

![Two managed AI terminals connected on the PatchPort board](docs/images/terminal-board.png)

Screenshots show an earlier English Windows build with controlled demo data,
not live provider responses or account usage. This README describes source
**0.5.0**; the Marketplace listing's published version can differ.

## What would I use it for?

Suppose you want Claude to propose an approach and Codex to question it. Open both
on the board, connect Claude's output to Codex's input, and ask Claude to explain
the approach to its peer. PatchPort passes the completed reply along the arrow.
You can follow the discussion and handle each CLI's approval prompts yourself.

For file changes, you can choose a separate workflow: give an AI selected files
in a working copy, inspect the diff, and apply only the paths you approve.

| Your goal | Start here | Where work happens |
| --- | --- | --- |
| Talk to an AI or connect two AI conversations | Terminal board | Original project directory, under the CLI's permissions |
| Request a change and review it before applying | Project work > Submit work | Selected working copy; promotion to the original is explicit |
| Run development, project tests and a separate AI review | Project work > Roles & archives | Role working copies with saved results and test evidence |
| Read saved answers and reported usage | Jobs & usage | Existing project records |

**A board terminal is a real terminal in your original project.** Drawing an
arrow does not isolate file edits. Choose the copy workflow when you want to
review proposed changes before applying them.

## Install and open the board

You need local desktop **VS Code**, **Python 3.11+**, and the official provider
CLI(s) you want to use. Install and sign in to each CLI through its own setup:
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) /
[Claude Code](https://code.claude.com/docs/en/quickstart).

1. Install **PatchPort** from the
   [Marketplace](https://marketplace.visualstudio.com/items?itemName=utialy.patchport).
   If you already have a matching native `.vsix`, use
   **Extensions > ... > Install from VSIX** instead.
2. Open a local project folder you trust. Press `Ctrl+Shift+P` on Windows/Linux
   or `Cmd+Shift+P` on macOS, then run **PatchPort: Open AI Terminal Board**.
3. If Python cannot be found, open VS Code Settings, search for
   `patchport.corePython`, and select the **Python executable**, not a folder.
   Finish any managed terminal work before running **Developer: Reload Window**.

The default Python command is `python` on Windows and `python3` on Linux/macOS.
The VSIX includes the Python core and workflow helpers; it does not include a
Python interpreter or the provider CLIs. **Node.js and Git are build tools;
you do not need them just to install a completed VSIX.**

### Connect your first two terminals

1. Click **+ AI terminal**, choose a provider profile, project and group, then
   **Start terminal**. Complete its normal login/startup/approval prompts.
2. Add a second AI terminal in the **same project and group**. When each CLI is
   at an empty prompt, click **Ready to receive** on its card.
3. Click the first card's right **Output port**, then the second card's left
   **Input port**. Begin with one arrow.
4. In **Groups > Settings**, set **Automatic delivery limit** to `2` and
   **Message hop limit** to `2`. For a discussion-only trial, set the shared
   goal to ask for comments without editing files or running tools. Click
   **Resume routes** for that group.
5. Ask the first AI to explain a small idea to the connected peer. Click outside
   the terminal after submitting so focus does not hold delivery. Check
   **Delivery history** when its answer finishes.

You should see the finished reply arrive as a new request in the second terminal.
Only a second arrow sends that AI's reply back. **Pause routes** pauses delivery;
**Stop** ends a terminal. Provider requests can consume your account's allowance.
See the [full walkthrough](docs/VSCODE_GUIDE.md#make-your-first-connection) for
the example prompt, route states and stopping controls.

## Next: request a change in a copy

The copy workflow needs a connected project (`bridge.json`). Install the Python
backend from this repository into a separate environment and run its setup
wizard with the same interpreter selected in `patchport.corePython`.
The [copy-work tutorial](docs/VSCODE_GUIDE.md#request-a-change-in-a-working-copy)
includes the commands.

```text
Choose input -> Preview -> Submit -> Start runner -> Read result
                                                    |
                         Apply selected paths <- Review diff
```

Submitting saves a task to the queue. Starting the runner executes pending work.
Applying selected paths changes the original project after review. These are
separate actions. A failed or interrupted provider task is not retried
automatically; an intentional rerun uses a new ID.

![Reviewing an original file beside its proposed working-copy change](docs/images/copies-recovery.png)

## Find the right panel

| Panel | What you can do |
| --- | --- |
| Jobs & usage | Read retained answers, task status, sessions and reported consumption |
| Submit work | Choose full files, omit files or use reviewed summaries before submitting |
| Copies & recovery | Compare changes, promote selected paths and preview backup recovery |
| Roles & archives | Run developer/test/reviewer steps and preserve evidence |
| Sessions & updates | Inspect history, explicitly resume eligible conversations and check runtime integrity |
| Data & settings | Manage project connections and preview selected data cleanup |

Usage marked **unknown** is not zero. Delivery limits count automatic input
attempts; they are not token budgets, spending caps or subscription balances.
Restoring a board keeps routes paused and does not restart AI work or resend
uncertain messages.

## Platforms and current limits

Source 0.5.0 has separate native packages for **Windows x64**, **glibc Linux
x64/ARM64**, and **macOS Intel/Apple Silicon**. Use a VSIX matching your local
VS Code's OS and CPU. See [platform requirements](docs/PLATFORMS.md) and
[measured validation](docs/VALIDATION.md).

- Default provider shells: Windows PowerShell, Linux Bash, macOS Zsh.
- Remote extension hosts, Remote-WSL, Alpine/musl and Windows ARM64 are outside
  the current supported scope.
- Ordinary-task/role-flow body removal, connection-file removal and the guided
  cleanup stages containing them remain **Windows-only**. Other supported
  workflows are described in the [data guide](docs/PROJECT_DATA.md).
- Dedicated **local LLM** provider support is planned, not implemented. An
  ordinary terminal profile does not add automatic local LLM reply routing.

## Learn more or contribute

- [User guide](docs/VSCODE_GUIDE.md): installation, first connection, copy work,
  roles, recovery and troubleshooting.
- [Project setup](docs/SETUP.md) / [data management](docs/PROJECT_DATA.md): detailed
  connection, cleanup, preserved data and receipt handling.
- [Contributing](CONTRIBUTING.md) / [architecture](docs/ARCHITECTURE.md): source
  builds, tests and how the parts fit together.
- [Roadmap](docs/ROADMAP.md) / [MIT license](LICENSE).
