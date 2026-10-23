# PatchPort: from installation to your first reviewed change

PatchPort lets you work with real Codex CLI and Claude Code terminals inside
VS Code. Start with a simple conversation route. Use the separate copy workflow
when you want to review file changes before applying them.

This guide describes the English **0.6.0 source**. Screenshots show an earlier
English Windows build with controlled demo data, not live provider replies or
billing. The Marketplace version can differ from the source version.

- [Install and prepare](#install-and-prepare)
- [Make your first connection](#make-your-first-connection)
- [Understand routing and stopping](#understand-routing-and-stopping)
- [Customize launch profiles](#customize-launch-profiles)
- [Talk to a local model](#talk-to-a-local-model)
- [Request a change in a working copy](#request-a-change-in-a-working-copy)
- [Review, apply and recover files](#review-apply-and-recover-files)
- [Run development, tests and review](#run-development-tests-and-review)
- [Read answers and usage](#read-answers-and-usage)
- [Save sessions and update](#save-sessions-and-update)
- [Manage project data](#manage-project-data)
- [Troubleshooting](#troubleshooting)
- [Build from source](#build-from-source)

## Install and prepare

### What you need

| Requirement | Why it is needed |
| --- | --- |
| Local desktop VS Code | Hosts the board and managed terminals |
| Python 3.11+ | Runs the core bundled with the extension; Python itself is not bundled |
| Official Codex CLI and/or Claude Code | Runs the AI sessions, using each CLI's existing login and permissions |
| A local project folder you trust | Sets the working directory for the terminal cards |

You need two managed AI terminals to try routing. They can use different
providers or different profiles for the same provider. PatchPort does not log
you in or choose an account for you. Follow the official
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) or
[Claude Code](https://code.claude.com/docs/en/quickstart) instructions, and check
that your command starts normally in its configured shell.

**Node.js 24+ and Git are only needed to build from source.** Installing a
completed VSIX does not require them. Copy-based project setup additionally
requires the repository's Python package; that step is covered below.

### Install the extension

1. Open the [PatchPort Marketplace listing](https://marketplace.visualstudio.com/items?itemName=utialy.patchport)
   and install it in VS Code. If you have a native VSIX instead, open
   **Extensions > ... > Install from VSIX** and choose that file.
2. For source 0.6.0 packages, match the file to your local VS Code's OS and CPU:

   | Your machine | VSIX filename |
   | --- | --- |
   | Windows x64 | `patchport-0.6.0-win32-x64.vsix` |
   | glibc Linux x64 | `patchport-0.6.0-linux-x64.vsix` |
   | glibc Linux ARM64 | `patchport-0.6.0-linux-arm64.vsix` |
   | macOS Intel | `patchport-0.6.0-darwin-x64.vsix` |
   | macOS Apple Silicon | `patchport-0.6.0-darwin-arm64.vsix` |

3. Open your intended local project folder. Review VS Code's workspace trust
   prompt; restricted workspaces cannot run PatchPort terminals or mutations.
4. Open the Command Palette: `Ctrl+Shift+P` on Windows/Linux or
   `Cmd+Shift+P` on macOS. Run **PatchPort: Open AI Terminal Board**.

**Expected result:** an empty board or your previously saved cards. Opening the
board does not start the AI sessions automatically.

If Python cannot be found, search VS Code Settings for `patchport.corePython`.
Leave it empty to use `python` on Windows or `python3` on Linux/macOS, or enter
an absolute Python 3.11+ executable path. A directory, activation script or shell
command is not an interpreter path. Check the selected executable with
`--version`. After changing it, finish managed terminal work and run
**Developer: Reload Window**; a reload ends the host's managed terminals.

Default provider shells are Windows PowerShell, Linux Bash and macOS Zsh.
See [platform requirements](https://github.com/utialy/PatchPort/blob/main/docs/PLATFORMS.md) for compatible systems. Remote
extension hosts, Remote-WSL, Alpine/musl and Windows ARM64 are outside this scope.

## Make your first connection

For this trial, use a small project and one directional arrow. The board's
terminals run in the **original project directory**, under each CLI's permissions.

![The terminal board with two managed AI terminals and a route](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/terminal-board.png)

### 1. Start two terminals

Click **+ AI terminal**, select a launch profile, project and group, and click
**Start terminal**. Complete the CLI's normal login, startup or permission prompts
inside its terminal.

Repeat for a second AI. Select the **same project and group**. For example, call
the first card `Planner` and the second `Reviewer`; the names are just labels.
When each CLI is waiting at an empty input prompt, click **Ready to receive**.
An approval dialog is not an empty prompt.

### 2. Connect Planner to Reviewer

Click Planner's right **Output port**, then Reviewer's left **Input port**.
You should see an arrow pointing toward Reviewer.

Open the **Groups** tab and click the group's **Settings**. Start with:

| Setting | Trial value | Meaning |
| --- | --- | --- |
| Automatic delivery limit | `2` | Maximum automatic input attempts for the group; existing consumed attempts count |
| Message hop limit | `2` | Maximum steps a message chain can travel |
| Shared goal (optional) | `Discuss only. Do not edit files or run tools. Give your peer one useful suggestion.` | Context included with routed messages |

Save the settings, then click that group's **Resume routes** if paused.
Inspect any listed consumed count before choosing limits; a settings edit does
not erase previous use.

### 3. Send a small request

Type this into Planner and submit it:

```text
Do not edit files or run tools.
Explain a simple approach to validating an email address.
Address your completed reply to the connected peer and ask for one potential edge case.
```

Click outside the terminal after submitting. Automatic input waits while a target
is focused or has a human draft.

**Expected result:** after Planner finishes its answer, **Delivery history** shows
the delivery and Reviewer receives the answer as a new request. Only the completed
assistant reply is forwarded; raw terminal logs and the entire conversation are
not synchronized. These requests can use your provider allowance.

Reviewer has no outgoing arrow yet, so its answer stays in its terminal. Read it
yourself. When comfortable, add a return arrow for a bounded two-way discussion.

## Understand routing and stopping

If an arrow exists but no message arrives, read its status in **Routes** and
check **Delivery history** before submitting again.

| State or condition | What to do |
| --- | --- |
| Group or route paused | Resume the intended group/route when ready |
| Target stopped or still starting | Start it and finish its CLI startup |
| Target busy or waiting for approval | Wait or answer its CLI prompt |
| Human draft / focused input | Finish or clear your draft intentionally, then click elsewhere |
| Delivery or hop limit reached | Inspect the consumed count and conversation before changing the limit |
| Uncertain delivery | Inspect the saved delivery and target terminal; do not blindly resend |

Replies completed while the group is paused are not replayed when it resumes.
An incoming arrow alone does not create a reply path.

| Control | Effect |
| --- | --- |
| Pause routes | Pauses automatic delivery; the CLIs keep running |
| Interrupt | Sends a terminal interrupt; inspect the CLI's resulting state |
| Stop | Ends that managed terminal |
| Close the board | Closes the view; managed terminals stay alive |
| Reload/close the extension host | Ends that host's owned terminals |

A restored board keeps groups paused. PatchPort does not automatically restart
AI sessions or replay old/uncertain messages.

## Customize launch profiles

Use **Launch profiles > + New CLI profile** when your CLI command differs from the
default, or you maintain a separate command for another account.

![Launch profile fields](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/launch-profiles.png)

| Field | What to enter |
| --- | --- |
| Display name | A name you recognize, such as `Claude review` |
| Provider | Claude Code or Codex for supported automatic routes; Ordinary terminal for manual shell work |
| Command name or executable | Just the command or executable, such as `claude-2` |
| Fixed arguments | A JSON string array; start with `[]` and keep options out of the command field |
| Shell for loading profiles | The shell that defines your function/alias; direct mode requires a real executable |
| Shell executable / Profile file path | Optional explicit paths; a specified profile file must be absolute |

Save and select the profile when adding a terminal. Profiles do not store account
tokens; authenticate your custom command through its official CLI. Keep its
permission and sandbox settings appropriate for the project.

**Jobs & usage > Import profiles** can import existing endpoint launch settings
from `bridge.json` after confirmation. Board profiles and copy-work endpoints
are separate settings: adding a board profile does not add a batch endpoint.

**Existing terminals** can focus a normal VS Code terminal or paste one line
without Enter. They cannot provide output/state capture or automatic routes.
An Ordinary terminal profile does not add local LLM completion detection.
Use the dedicated local profile below for Ollama or LM Studio.

## Talk to a local model

Use this flow to ask a local model questions or include its replies in a board
conversation. Local chat receives only the text you type, paste or route to it.
It does not read/edit project files, run commands or perform batch/role work.

### 1. Prepare the runtime and profile

1. Set up Ollama or LM Studio and a text model yourself, then start its HTTP
   server. PatchPort does not download a model or install a runtime.
2. Open **Launch profiles > New local profile**. Choose **Ollama** or **LM Studio**.
3. Enter the **Server origin**, such as `http://127.0.0.1:11434` for a server
   configured on that port. Use the actual port shown by your runtime. Omit `/v1`,
   other API paths and tokens. Only loopback HTTP origins are accepted.
4. Click **Check connection** to list models and select the **Exact model ID**.
   This check does not generate, download or load a model. A listed model is not
   proof that its response format is supported. Known remote/embedding models
   are excluded; an unknown execution location requires your confirmation.
5. Set the **Context window (tokens)**, **Maximum output tokens** and
   **Total request timeout (seconds)**. Output must fit within the context window.
   Ollama receives the window as `num_ctx`; for LM Studio, enter the context
   window actually loaded on the server, which may differ from the model maximum.
6. Confirm in the server settings that the selected model runs on your computer,
   check the confirmation box and click **Save local profile**. A localhost
   connection alone does not guarantee offline inference.

If LM Studio requires authentication, save the profile and click **Set API token**.
Enter it in VS Code's separate password prompt. SecretStorage binds it to this
profile and exact server origin; it is not put in settings JSON or the webview.
If the initial model listing needs that token, enter the exact model ID manually,
save and set the token, then check the connection again. After changing the
server origin, set its token again.

### 2. Ask a question and connect its reply

Add an AI terminal using the saved profile and your project. On first use,
PatchPort may offer **Prepare** for the local conversation ledger. This retains
existing records and blocks older extension writers; stop managed terminals
before preparing it. Starting the terminal itself does not generate a reply.

Try a small text-only question, such as `Explain this error in three steps`,
followed by the error text. Press **Enter** to send. **Ctrl+J** or **Alt+Enter**
adds a line. Pasting multiple lines does not submit them. The first question may
load an already installed model into memory. **Ctrl+C** cancels the request;
the server may continue computing if it does not confirm cancellation.

Connect the local card's output to another local, Codex or Claude card as in the
first tutorial. Only successfully completed replies are routed. Active requests,
focused input and unsent drafts hold automatic delivery. After handling an error
or cancellation, clear the draft and check **Ready to receive** and the group's
**Resume routes**. Failed or uncertain requests are not automatically retried.

| Action | What changes |
| --- | --- |
| New local conversation | Starts a new session with empty context; retains previous records, usage and group delivery counts |
| Group > New round | Resets the group's automatic delivery count; retains local model context |
| Edit a running profile | Applies to the next session; the active session keeps its launch settings |
| Reopen VS Code | Restores cards/routes with groups paused; does not start models or resume local context automatically |

Requests to the same server are serialized within one extension host. This is
not a global limit across VS Code windows. Missing token usage remains unknown.
Successful user/assistant pairs form context; cancelled, failed and uncertain
turns are not silently replayed into the next question.

## Request a change in a working copy

Use this workflow when you want to inspect proposed changes before writing them
back to the original. It copies only the configured input. A working copy is not
a security sandbox; the provider's permissions still matter.

### Connect the project once

Keep the PatchPort checkout separate from the project being worked on.
First [prepare the Python backend](#build-from-source) from the checkout with
`pip install .` in its isolated environment. You can stop before the Node build
steps if you already installed the extension.

Set `patchport.corePython` to that environment's absolute Python executable.
Use the **same interpreter** for setup. Replace the example paths with your own.

Windows PowerShell:

```powershell
& 'C:\Projects\PatchPort\.venv\Scripts\python.exe' -m agent_bridge setup --project 'C:\Projects\hello-patchport'
```

Linux/macOS:

```sh
/path/to/PatchPort/.venv/bin/python -m agent_bridge setup --project /path/to/hello-patchport
```

The wizard lets you choose installed provider commands, input paths and paths
eligible for later promotion. Review its preview and explicitly apply it. Setup
does not log in, call an AI, submit a task or start the runner.

- **Input paths** decide what is copied into the task.
- **Writable paths** decide what may later be promoted into the original. They
  do not prevent the CLI from changing other files in its working copy.
- Existing `bridge.json` settings are preserved. An empty project can use the
  wizard's starter file. Authentication, environments and caches are not inputs.

Open the connected project in VS Code. In **Jobs & usage**, click **Refresh** to
discover its configuration. If needed, use **Select configuration**; its project
root must match the open folder. See [project setup](https://github.com/utialy/PatchPort/blob/main/docs/SETUP.md) for exact options.

### Preview, submit, then start

![Selected inputs and the reviewed submission plan](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/submit-work.png)

1. Open **Project work > Submit work**.
2. Enter a **New batch ID**, such as `readme-check-001`, and a concrete
   **Original request**. Choose endpoint checkboxes from the connected config.
   For a first trial, ask for a small review; to produce file edits, use an
   endpoint whose configured permissions allow the intended edits.
3. Click **Refresh input & budget**. Use `full` for files the AI needs and
   `omit` for others. Required input stays full.
4. Click **Preview input & submission**. Check selected files, destinations,
   rendered input estimates and limits. Nothing is queued yet.
5. Click **Apply reviewed preview** and confirm. This saves the task in the queue.
6. Inspect pending work, pause state and remaining claim limits in **Jobs & usage**.
   Click **Start queue runner** only when ready for pending tasks to execute.
7. Refresh **Jobs & usage** and read the saved answer and task outcome.

**Expected result:** a saved task and answer, with changes retained in its copy
if it made edits. Submission, runner execution and promotion are separate.
`DONE` describes the process outcome, not the quality or approval of its edits.
An intentional rerun after failure/interruption needs a new ID.

Changed inputs, another project, an expired plan or a lost core connection require
a fresh preview. If an action's response is lost, inspect its saved result/receipt
first rather than issuing it again.

For larger inputs, [reviewed summaries](https://github.com/utialy/PatchPort/blob/main/docs/CONTEXT_SUMMARIES.md) can supply reviewed,
hash-checked reference text. No automatic AI summary generation is performed.
[Input budgets](https://github.com/utialy/PatchPort/blob/main/docs/CONTEXT.md) use files, UTF-8 bytes and estimated tokens; these are
different from claim limits and automatic delivery limits.

## Review, apply and recover files

![Current original on the left and proposed copy on the right](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/copies-recovery.png)

1. Open **Copies & recovery** and select a finished batch task. Failed task
   copies can also be inspected; active tasks cannot be promoted.
2. Click **diff** beside a path. Compare the current original on the left with
   the proposed saved copy on the right. Inspect binary/oversized files separately.
3. Select only paths marked **Promotion allowed**, then click
   **Preview selected promotion**. Read the paths and hashes.
4. Save or revert overlapping unsaved editor changes intentionally. The extension
   does not silently save your buffers. Re-preview after changes.
5. Apply the reviewed preview and confirm. The core rechecks the original, proposal,
   permitted paths and task state before writing.
6. Refresh the task. Its `backup-...` recovery preview lets you inspect an undo
   operation; review it before explicitly applying.

**Expected result:** only the selected approved paths are promoted. Backup and
journal evidence support recovery from compatible known states. Later conflicting
edits, damaged backups or changed proposals block recovery; keep the evidence and
inspect the reported reason. A reviewer's answer is not approval to apply all files.

Role-task changes are reviewed in **Roles & archives**, using the same explicit
selection and promotion principles.

## Run development, tests and review

Use **Roles & archives** when a task needs a developer, your project checks, and a
separate reviewer. First connect the project and prepare `role_workflow.json`.
See [role configuration](https://github.com/utialy/PatchPort/blob/main/docs/ROLES.md) for commands, permissions and project-specific
test settings.

![Role outcomes and saved test evidence](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/roles-archives.png)

1. Enter a new flow ID and a concrete development/review request.
2. Click **Preview role execution**. Review input, configured host test commands,
   and the maximum of two provider task starts. Roles share the parent queue's
   lifetime claim cap with ordinary tasks.
3. Apply and confirm. Refresh with **Refresh roles & tests**.
4. Read test exit codes, logs and both role answers. A failed test stops later
   stages; provider failures are not retried automatically.
5. Select the developer task to review its diff and explicitly promote selected paths.

The job runs the configured local test commands, which are separate from AI
responses. No change is promoted automatically. If a launch response is lost,
inspect its existing receipt/result before starting another flow.

**Preview archive (no deletion)** preserves eligible flow evidence without
deleting live artifacts. Interrupted archives use **Preview archive resume**
with the exact recorded flow/operation ID. See [archives](https://github.com/utialy/PatchPort/blob/main/docs/ARCHIVES.md).

## Read answers and usage

![Saved jobs, answers and consumption scopes](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/jobs-usage.png)

Open **Jobs & usage**, choose the project/period/grouping, and click **Refresh**.
Select a task to read its retained result. **Read saved detail** opens a selected
interactive event.

Keep these units separate:

| Number | Meaning |
| --- | --- |
| Automatic deliveries, such as 0/12 | Automatic terminal input attempts used/allowed for the group |
| Batch claims | Lifetime task starts against the queue's claim cap |
| Input estimate | Selected files/bytes and a heuristic token estimate |
| Reported tokens/cost | Only the supported consumption signal in the selected scope |

**Unknown is not zero.** Partial totals are incomplete. These are not whole-account
billing, subscription remaining quota or a hard spending limit. The CLI `usage`
command covers batch work; the extension shows batch and interactive records
separately and combines known values with their coverage labels.

Pausing batch claims does not stop active work. **Drain & stop runner** waits for
active tasks; **Cancel active work & stop** requests cancellation. Neither is the
same action as stopping a board terminal.

## Save sessions and update

![Recorded sessions and runtime integrity controls](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/sessions-updates.png)

Saved boards restore cards, routes, group limits/counts and recorded history with
groups paused. They do not start an AI or resend prior requests. Unfinished records
from an earlier host can appear as `UNCONFIRMED`.

- **Start new managed session** starts the current profile with a new internal ID.
- **Resume this provider conversation** uses an exact recorded provider UUID for
  this project/card and asks for confirmation. End that conversation in other
  windows first. Missing IDs or incompatible flags require a new session or a
  corrected profile; PatchPort does not guess from global histories.
- **Check core & bundle integrity** checks the bundled runtime and helpers.
- Before an update/reload, finish and stop managed terminals. Install the matching
  VSIX and use **Reload after finishing work** when ready.

Conversation resume does not replay the old request. When resumed consumption
cannot be separated reliably, it stays unknown instead of double-counting.

## Manage project data

Start with **Project work > Data & settings > Refresh saved-data inventory**.
Inspection helps identify what is stored before choosing an action.

![Project data inventory and selected cleanup results](https://raw.githubusercontent.com/utialy/PatchPort/main/docs/images/project-data.png)

| What you want | Choose | What to expect |
| --- | --- | --- |
| Rebuild the workspace's board layout | Reset project cards and routes | Existing records and shared group consumption remain |
| Change the selected configuration | Attach default configuration / Select configuration and reconnect | Preview and apply for this workspace; other projects remain |
| Remove eligible old copies | Copy cleanup controls | Review selected copies and retention/protection reasons |
| Remove supported saved bodies | Body-management controls | Explicit protection preparation, selection, preview and confirmation |
| Disconnect and clean a project | Guided project cleanup | Review and apply one stage at a time; settings are last |

Ordinary-task/role-flow body removal, connection-file removal and guided stages
containing them are **Windows-only**. This restriction does not disable terminal
routing, saved usage, supported conversation management or copy review/recovery
on Linux/macOS.

Protection preparation changes ledger compatibility: older writers cannot use
the prepared ledger. Historical, shared, active, unknown/LOST, recovery-protected
or unowned data cannot simply be adopted for deletion. Follow the detailed
[data management guide](https://github.com/utialy/PatchPort/blob/main/docs/PROJECT_DATA.md) before preparing or removing data.

Cleanup preserves control IDs, usage, limits, originals and protected backups.
Selected-scope completion does not mean every project byte is erased. If a response
is lost, query the saved result first. Explicit resume handles the accepted
operation's remaining I/O; it never reruns an AI task. Result lookup remains
available after configuration removal.

## Troubleshooting

| What you see | Check first |
| --- | --- |
| No project / restricted mode | Open the intended local folder and review workspace trust |
| Python error | Select the actual Python 3.11+ executable, then finish work and reload |
| Connection assets missing during setup | Install the repository package into the same interpreter used for setup; the VSIX core alone is not the setup package |
| CLI/alias not found | Check it in the selected shell/profile; put flags in Fixed arguments |
| Empty Project work / no bridge.json | Complete project setup, then Refresh; direct board terminals do not need a batch config |
| Wrong configuration | Choose one whose project root matches the open folder |
| Arrow with no delivery | Read Routes: paused state, readiness, focus/draft, busy state and limits |
| Permission prompt | Handle it inside the CLI; do not mark it Ready to receive |
| Preview expired / files changed | Inspect the change, then make a new preview |
| Promotion conflict | Save/revert intentional edits and compare the current original again |
| Task waiting | Check queue pause, remaining claims and runner status |
| Runner launch or action unconfirmed | Query existing status/result/receipt before another action |
| Tests failed | Read the saved test log and inspect the copy; reruns use a new flow ID |
| Cleanup blocked / interrupted | Read protection reasons or the saved operation receipt; do not remove guards to force progress |
| Board snapshot changed/locked | Check other windows and preserve interrupted snapshot files |
| Unknown usage / missing resume ID | The supported signal was unavailable; do not treat it as zero or guess a conversation |

For deeper reference: [runner management](https://github.com/utialy/PatchPort/blob/main/docs/RUNNER_MANAGEMENT.md),
[usage coverage](https://github.com/utialy/PatchPort/blob/main/docs/USAGE.md), [data management](https://github.com/utialy/PatchPort/blob/main/docs/PROJECT_DATA.md) and
[measured validation](https://github.com/utialy/PatchPort/blob/main/docs/VALIDATION.md). External-process handoff and local
coding-agent tools remain future work.

## Build from source

Use this section to build the current source or prepare its Python setup backend.
It is not a prerequisite for installing a completed VSIX.

Install Git, Python 3.11+ and, for the extension build, Node.js 24+. Keep the
checkout separate from the project you will connect. Build on the matching OS
and CPU; renaming a VSIX does not change its native terminal binaries.

### Windows PowerShell

From the directory where you keep source checkouts:

```powershell
git clone https://github.com/utialy/PatchPort.git
cd PatchPort
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

The Python setup backend is now installed in that environment. To build the VSIX:

```powershell
cd extensions/vscode
npm ci
npm run package
cd ../..
code --install-extension .bridge/artifacts/patchport-0.6.0-win32-x64.vsix
```

If `code` is not on PATH, use **Install from VSIX** in the editor.

### Linux or macOS

Linux source builds need a C++ compiler and make for the native terminal module.
See [platform requirements](https://github.com/utialy/PatchPort/blob/main/docs/PLATFORMS.md) for runtime/test dependencies.

```sh
git clone https://github.com/utialy/PatchPort.git
cd PatchPort
python3 -m venv .venv
.venv/bin/python -m pip install .
```

To build the extension:

```sh
cd extensions/vscode
npm ci
npm run package
```

Install the target-specific VSIX printed by the command through
**Extensions > ... > Install from VSIX**. In Settings, select the absolute
`.venv/bin/python` executable from your checkout for copy setup/work.

The source product is the VS Code extension, not a separate desktop installer.
CI publishes generated native VSIX files as workflow artifacts; a source build
does not publish a Marketplace version. See [Contributing](https://github.com/utialy/PatchPort/blob/main/CONTRIBUTING.md)
for tests and contribution guidelines.
