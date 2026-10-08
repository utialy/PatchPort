import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import type { Board, Profile, Runtime, Delivery, Notice, NodeConfig, Group } from "./model";
import { WorkflowPanel } from "./workflowWebview";

declare function acquireVsCodeApi(): { postMessage(message: unknown): void; getState(): unknown; setState(value: unknown): void };
const api = acquireVsCodeApi();
const post = (type: string, values: Record<string, unknown> = {}) => api.postMessage({ type, ...values });
interface State { board: Board; runtime: Runtime[]; active: string[]; profiles: Profile[]; projects: string[]; trusted: boolean; supported: boolean; remote: string; version: string; deliveries: Delivery[]; notices: Notice[]; routes: { id: string; state: string; label: string }[]; external: { id: string; name: string; capability: string }[]; core?: any }
let state: State | undefined;
let zoom = 0.9;
let connecting = "";
let inspectorTab = "links";
const cards = new Map<string, { el: HTMLElement; terminal: Terminal; fit: FitAddon; observer: ResizeObserver }>();
const pendingOutput = new Map<string, { sequence: number; data: string }[]>();
const streams = new Map<string, { last: number; waiting: boolean }>();
let expandedId = "";
const $ = <T extends HTMLElement = HTMLElement>(selector: string): T => document.querySelector(selector)!;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text = ""): HTMLElementTagNameMap[K] => { const element = document.createElement(tag); element.className = className; element.textContent = text; return element; };
const button = (text: string, action: () => void, className = "") => { const b = el("button", className, text); b.type = "button"; b.onclick = action; return b; };
const option = (value: string, text: string) => { const o = el("option", "", text); o.value = value; return o; };
const phaseLabels: Record<string, string> = { stopped: "Stopped", starting: "Starting", ready: "Ready to receive", busy: "Working", attention: "Needs attention", error: "Error" };
function flushOutput(id: string): void {
  const card = cards.get(id), stream = streams.get(id); if (!card || !stream || stream.waiting) return;
  const chunks = pendingOutput.get(id) || []; pendingOutput.delete(id);
  for (const chunk of chunks) if (chunk.sequence > stream.last) { card.terminal.write(chunk.data); stream.last = chunk.sequence; }
}
const statusLabels: Record<string, string> = { queued: "Queued", sent: "Sent", completed: "Completed", uncertain: "Uncertain", cancelled: "Cancelled" };

$("#app").innerHTML = "\n<header class=\"topbar\"><div class=\"brand\"><span class=\"brand-mark\">\u2197</span><div><strong>PatchPort</strong><span>AI TERMINAL BRIDGE</span></div><span class=\"version\">FIRST EDITION</span></div><div class=\"top-actions\"><button id=\"profiles\" class=\"quiet\">Launch profiles</button><button id=\"external\" class=\"quiet\">Existing terminals</button><button id=\"add\" class=\"primary\">+ AI terminal</button></div></header>\n<div class=\"workspacebar\"><div class=\"workspace-label\"><span class=\"live-dot\"></span><select id=\"project\" aria-label=\"Project\"></select><span id=\"workspace-kind\">LOCAL WORKSPACE</span></div><div class=\"workspace-actions\"><button id=\"new-group\">+ Group</button><button id=\"pause-all\">Pause routes</button><button id=\"stop-all\" class=\"danger-quiet\">Stop all terminals</button></div></div>\n<div id=\"banner\" hidden></div><main><section class=\"canvas-area\"><div class=\"canvas-heading\"><div><span class=\"eyebrow\">WORKSPACE CANVAS</span><h1>Your terminals. One team.</h1></div><div id=\"connect-hint\">Connect an output dot to another terminal's input dot.</div></div><div id=\"viewport\"><div id=\"world\"><svg id=\"wires\" aria-label=\"AI routes\"><defs><marker id=\"arrow\" markerWidth=\"9\" markerHeight=\"9\" refX=\"8\" refY=\"4.5\" orient=\"auto\"><path d=\"M0 0L9 4.5L0 9\" fill=\"#7dd3b0\"/></marker></defs><g id=\"wire-paths\"></g></svg><div id=\"nodes\"></div></div><div id=\"empty\"><div class=\"empty-orbit\">\u2197</div><span class=\"eyebrow\">YOUR AGENTS, CONNECTED</span><h2>Add your first AI terminal</h2><p>Use your existing CLIs and authentication.<br>Type in each terminal and route completed responses between them.</p><button id=\"first-add\" class=\"primary\">\uff0b Add AI terminal</button><div class=\"onboarding\"><span><b>01</b> Choose a profile</span><span><b>02</b> Type directly</span><span><b>03</b> Connect and collaborate</span></div></div></div><div class=\"canvas-tools\"><span id=\"counts\">Preparing</span><div><button id=\"zoom-out\" aria-label=\"Zoom out\">\u2212</button><button id=\"zoom-label\">90%</button><button id=\"zoom-in\" aria-label=\"Zoom in\">\uff0b</button><button id=\"arrange\">Arrange</button></div></div></section><aside class=\"inspector\"><div class=\"tabs\"><button data-tab=\"links\" class=\"active\">Routes</button><button data-tab=\"groups\">Groups</button><button data-tab=\"activity\">Delivery history</button></div><div id=\"inspector-body\"></div><div class=\"inspector-footer\">Routes forward subsequent completed responses.<br>Automatic delivery waits while you type.</div></aside></main><footer><span id=\"footer-state\">Local terminals - existing CLI authentication</span><span>Direct terminals work in the original project.</span></footer><div id=\"toast\" role=\"status\" hidden></div><div id=\"modal\" hidden><div class=\"modal-card\" role=\"dialog\" aria-modal=\"true\"><div class=\"modal-head\"><h2 id=\"modal-title\"></h2><button id=\"modal-close\" aria-label=\"Close\">\u00d7</button></div><div id=\"modal-body\"></div></div></div>";

function toast(text: string): void { const t = $("#toast"); t.textContent = text; t.hidden = false; setTimeout(() => t.hidden = true, 6500); }
$(".top-actions").prepend(button("Jobs & usage", () => openCore(), "quiet"));
$(".top-actions").append(button("Guide", () => post("openGuide"), "quiet"));
const workflowPanel = new WorkflowPanel(post, modal);
$(".top-actions").prepend(button("Project work", () => workflowPanel.open(selectedProject()), "quiet"));
let coreOpen = false, coreDays: number | undefined, coreGrouping = "endpoint";
const metricNames: Record<string, string> = { input_tokens: "Input tokens", output_tokens: "Output tokens", cache_read_tokens: "Cache read", cache_write_tokens: "Cache write", cost_usd: "Reported USD", elapsed_seconds: "Elapsed seconds" };
function usageTable(container: HTMLElement, summary: any): void {
  const table = el("table", "usage-table");
  for (const [key, label] of Object.entries(metricNames)) {
    const row = el("tr"), metric = summary?.metrics?.[key];
    const value = metric?.known_sum;
    row.append(el("th", "", label), el("td", "", typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: key === "cost_usd" ? 6 : 2 }) : "Unknown"), el("td", "muted", metric ? `Missing ${metric.unknown_tasks} \u00b7 ${metric.complete ? "Reported within scope" : "Partial sum"}` : "Not reported"));
    table.append(row);
  }
  container.append(table);
}
function openCore(): void { coreOpen = true; modal("Jobs & usage"); renderCore(); post("coreRefresh", { project: selectedProject(), days: coreDays, groupBy: coreGrouping }); }
function renderCore(): void {
  if (!coreOpen || !state) return;
  const body = $("#modal-body"); body.replaceChildren(); $("#modal .modal-card").classList.add("core-modal");
  const controls = el("div", "core-controls");
  const project = selectItems(state.projects.map(p => ({ value: p, label: p })), state.core?.project || selectedProject());
  const period = selectItems([{ value: "", label: "All time" }, { value: "7", label: "Last 7 days" }, { value: "30", label: "Last 30 days" }], coreDays ? String(coreDays) : "");
  const grouping = selectItems(["endpoint", "model", "batch", "day"].map(value => ({ value, label: value })), coreGrouping);
  const refresh = () => { coreDays = period.value ? Number(period.value) : undefined; coreGrouping = grouping.value; post("coreRefresh", { project: project.value, days: coreDays, groupBy: coreGrouping }); };
  project.onchange = refresh; period.onchange = refresh; grouping.onchange = refresh;
  controls.append(project, period, grouping, button("Refresh", refresh), button("Select configuration", () => post("coreConfig", { project: project.value }))); body.append(controls);
  const view = state.core;
  if (!view || view.state === "idle" || view.state === "loading") { body.append(el("p", "muted", "Reading existing configuration and databases. Inspection starts neither a runner nor an AI.")); return; }
  if (view.state === "error") { body.append(el("p", "core-error", view.error)); body.append(button("Set Python path", () => post("corePythonSettings"))); return; }
  const binding = view.binding;
  body.append(el("p", "muted", `${binding.linked ? "Existing configuration connected" : "No configuration; managed sessions only"} \u00b7 ${binding.config}\nLedger: ${binding.database}`));
  body.append(el("p", "muted", "Periods use batch task or managed session start time. Missing values are not zero. Reported USD is neither a subscription bill nor remaining quota; it does not enforce charges."));
  const columns = el("div", "core-columns"), batch = el("section"), live = el("section"); columns.append(batch, live); body.append(columns);
  batch.append(el("h3", "", "Existing batch jobs"));
  if (binding.linked) {
    const control = binding.control?.control || {};
    batch.append(el("p", "", `Lifetime claims ${control.calls_started ?? "?"} / ${control.max_calls ?? "Unlimited"} \u00b7 ${control.paused ? "New claims paused" : "New claims allowed"}`));
    const buttons = el("div", "core-controls");
    buttons.append(button("Pause batch", () => post("coreControl", { project: binding.project, action: "pause" })), button("Resume batch", () => post("coreControl", { project: binding.project, action: "resume" })), button("Import profiles", () => post("coreImport", { project: binding.project })));
    const cap = textInput(control.max_calls == null ? "" : String(control.max_calls), "Lifetime claim cap; empty means unlimited"); cap.type = "number"; cap.min = "0";
    buttons.append(cap, button("Apply cap", () => post("coreControl", { project: binding.project, action: "limit", maxCalls: cap.value ? Number(cap.value) : null }))); batch.append(buttons);
    batch.append(el("p", "muted", "Batch controls do not affect interactive terminals. Pausing does not cancel active tasks."));
    batch.append(el("pre", "budget-note", `Input budget: ${JSON.stringify(binding.context_budget || {})}\nFile and estimated input limits; not actual token or charge limits. Interactive terminals do not use this budget.`));
    usageTable(batch, view.usage?.summary);
    for (const [name, summary] of Object.entries(view.usage?.groups || {})) { const detail = el("details"); detail.append(el("summary", "", name)); usageTable(detail, summary); batch.append(detail); }
    const tasks = view.tasks?.tasks || [];
    batch.append(el("h4", "", `Recent tasks ${tasks.length}${view.tasks?.more ? " - more available" : ""}`));
    for (const task of tasks) batch.append(button(`${task.state} \u00b7 ${task.id}`, () => post("coreResult", { project: binding.project, taskId: task.id }), "task-select"));
    if (!tasks.length) batch.append(el("p", "muted", "No saved batch tasks."));
    if (view.result) { const result = el("details", "core-result"); result.open = true; result.append(el("summary", "", `Saved result - ${view.result.id}`), el("pre", "", JSON.stringify(view.result, null, 2))); batch.append(result); }
  } else batch.append(el("p", "muted", "No bridge.json. Inspection has not created a queue or started a runner."));
  live.append(el("h3", "", "Managed interactive sessions"));
  usageTable(live, view.history?.summary);
  live.append(el("p", "muted", "Automatic delivery limits count input attempts. Manual questions, tokens and costs are outside this cap."));
  for (const group of state.board.groups) live.append(el("p", "", `${group.name}: automatic deliveries ${group.used}/${group.maxDeliveries} - hops ${group.maxHops} steps - ${group.paused ? "Paused" : "Allowed"}`));
  for (const session of view.history?.sessions || []) {
    const detail = el("details"); detail.append(el("summary", "", `${session.name} \u00b7 ${session.state} \u00b7 ${new Date(session.started * 1000).toLocaleString()}`));
    detail.append(el("p", "muted", `${session.source} \u00b7 ${session.coverage}`), el("pre", "", JSON.stringify(session, null, 2))); live.append(detail);
  }
  live.append(el("h4", "", "Recorded deliveries and responses"));
  for (const delivery of view.history?.deliveries || []) { const detail = el("details"); detail.append(el("summary", "", `${statusLabels[delivery.status] || delivery.status} \u00b7 ${delivery.source} \u2192 ${delivery.target}`), el("pre", "", delivery.text || ""), el("small", "", delivery.reason || "")); live.append(detail); }
  for (const event of view.history?.events || []) { const detail = el("details"); detail.append(el("summary", "", `${event.kind} \u00b7 ${new Date(event.at * 1000).toLocaleTimeString()}`), el("pre", "", JSON.stringify(event.data, null, 2)), button("Read saved detail", () => post("coreEvent", { project: binding.project, eventId: event.id }))); live.append(detail); }
  if (view.event) { const detail = el("details"); detail.open = true; detail.append(el("summary", "", `Saved detail - ${view.event.kind}`), el("pre", "", JSON.stringify(view.event.data, null, 2))); live.append(detail); }
  const merged = el("section"); merged.append(el("h3", "", "Consumption - batch jobs and managed sessions"));
  const summaries = [view.usage?.summary, view.history?.summary].filter(Boolean), metrics: any = {};
  for (const key of Object.keys(metricNames)) { const parts = summaries.map(s => s.metrics[key]); const known = parts.filter(p => typeof p?.known_sum === "number"); metrics[key] = { known_sum: known.length ? known.reduce((sum, p) => sum + p.known_sum, 0) : null, unknown_tasks: parts.reduce((sum, p) => sum + (p?.unknown_tasks || 0), 0), complete: parts.every(p => p?.complete) }; }
  usageTable(merged, { metrics }); body.append(merged);
}
const expanded = el("section", "terminal-expanded"); expanded.hidden = true;
const expandedHeader = el("div", "expanded-header"); const expandedTitle = el("strong");
const expandedBody = el("div", "expanded-body");
function closeExpanded(): void {
  const card = cards.get(expandedId), area = expandedBody.querySelector(".terminal-area");
  if (card && area) card.el.insertBefore(area, card.el.querySelector(".card-controls"));
  expanded.hidden = true; expandedId = ""; if (card) requestAnimationFrame(() => card.fit.fit());
}
expandedHeader.append(expandedTitle, button("Back to board", closeExpanded)); expanded.append(expandedHeader, expandedBody); $("#app").append(expanded);
function expandTerminal(id: string): void {
  if (expandedId) closeExpanded();
  const card = cards.get(id), node = state?.board.nodes.find(n => n.id === id); const area = card?.el.querySelector(".terminal-area"); if (!card || !area) return;
  expandedId = id; expandedTitle.textContent = node?.name || "Terminal"; expandedBody.append(area); expanded.hidden = false;
  requestAnimationFrame(() => { card.fit.fit(); card.terminal.focus(); });
}
function modal(title: string): HTMLElement { if (title !== "Jobs & usage") { coreOpen = false; $("#modal .modal-card").classList.remove("core-modal"); } $("#modal-title").textContent = title; const body = $("#modal-body"); body.replaceChildren(); $("#modal").hidden = false; return body; }
function closeModal(): void { workflowPanel.close(); $("#modal").hidden = true; coreOpen = false; $("#modal .modal-card").classList.remove("core-modal"); }
$("#modal-close").onclick = closeModal;
$("#modal").addEventListener("click", event => { if (event.target === $("#modal")) closeModal(); });
document.addEventListener("keydown", event => { if (event.key === "Escape") { closeModal(); connecting = ""; updateHint(); } });
function field(form: HTMLElement, label: string, input: HTMLElement, hint = ""): void { const wrapper = el("label", "field"); wrapper.append(el("span", "field-label", label), input); if (hint) wrapper.append(el("small", "", hint)); form.append(wrapper); }
function selectItems(items: { value: string; label: string }[], value?: string): HTMLSelectElement { const s = el("select"); items.forEach(i => s.append(option(i.value, i.label))); if (value) s.value = value; return s; }
function textInput(value = "", placeholder = ""): HTMLInputElement { const input = el("input"); input.value = value; input.placeholder = placeholder; return input; }
function selectedProject(): string { return $<HTMLSelectElement>("#project").value || state?.projects[0] || ""; }
function openAdd(selectedProfile?: string): void {
  if (!state) return;
  if (!state.projects.length) { post("openFolder"); return; }
  const body = modal("Add AI terminal");
  body.append(el("p", "muted", "The CLI runs in the selected original project using its existing login and permissions."));
  const form = el("form");
  const profile = selectItems(state.profiles.map(p => ({ value: p.id, label: `${p.name} \u00b7 ${p.command}` })), selectedProfile);
  const project = selectItems(state.projects.map(p => ({ value: p, label: p })), selectedProject());
  const group = selectItems(state.board.groups.map(g => ({ value: g.id, label: g.name })));
  field(form, "Launch profiles", profile); field(form, "Project folder", project); field(form, "Collaboration group", group);
  const detail = el("div", "profile-detail");
  const refresh = () => { const p = state!.profiles.find(p => p.id === profile.value)!; detail.textContent = `${p.command} ${p.args.join(" ")}\n${p.shell}${p.profilePath ? ` \u00b7 ${p.profilePath}` : " - default user profile"}`; };
  refresh(); profile.onchange = refresh; form.append(detail);
  const actions = el("div", "form-actions"); actions.append(button("+ Add custom command", () => openProfiles()), button("Cancel", closeModal));
  const submit = el("button", "primary", "Start terminal"); submit.type = "submit"; actions.append(submit); form.append(actions);
  form.onsubmit = event => { event.preventDefault(); post("add", { profileId: profile.value, project: project.value, groupId: group.value }); closeModal(); };
  body.append(form);
}
function openProfiles(selected?: string): void {
  if (!state) return;
  const body = modal("Launch profiles");
  body.append(el("p", "muted", "Register functions or aliases such as claude-2. Enter the command and fixed arguments separately."));
  const picker = selectItems([{ value: "", label: "+ New profile" }, ...state.profiles.map(p => ({ value: p.id, label: p.name }))], selected);
  field(body, "Profile", picker); picker.onchange = () => openProfiles(picker.value);
  const existing = state.profiles.find(p => p.id === picker.value);
  const form = el("form");
  const name = textInput(existing?.name || "", "Example: Claude 2"); name.required = true;
  const provider = selectItems([{ value: "claude", label: "Claude Code - automatic routing" }, { value: "codex", label: "Codex - automatic routing" }, { value: "terminal", label: "Ordinary terminal - manual input" }], existing?.provider);
  const command = textInput(existing?.command || "", "Example: claude-2"); command.required = true;
  const args = textInput(JSON.stringify(existing?.args || []), "[\"--model\", \"...\"]");
  const shell = selectItems(["powershell", "pwsh", "bash", "zsh", "direct"].map(v => ({ value: v, label: v })), existing?.shell || "powershell");
  const shellPath = textInput(existing?.shellPath || "", "Empty uses the default shell");
  const profilePath = textInput(existing?.profilePath || "", "Empty uses the default user profile");
  field(form, "Display name", name); field(form, "Provider", provider); field(form, "Command name or executable", command, "Enter only the command name. Put additional options in Arguments below.");
  field(form, "Fixed arguments (JSON array)", args); field(form, "Shell for loading profiles", shell); field(form, "Shell executable path (optional)", shellPath); field(form, "Profile file path (optional)", profilePath);
  const actions = el("div", "form-actions"); actions.append(button("Cancel", closeModal)); const save = el("button", "primary", "Save profile"); save.type = "submit"; actions.append(save); form.append(actions);
  form.onsubmit = event => { event.preventDefault(); try { post("profile", { profile: { id: existing?.id || crypto.randomUUID(), name: name.value, provider: provider.value, command: command.value, args: JSON.parse(args.value), shell: shell.value, shellPath: shellPath.value, profilePath: profilePath.value } }); } catch { toast("Enter fixed arguments as a JSON array, for example [\"--model\", \"...\"]."); } };
  body.append(form);
}
function openExternal(): void {
  if (!state) return;
  const body = modal("Existing VS Code terminals");
  body.append(el("p", "muted", "Focus an existing terminal or paste one line. This does not collect output, detect AI state or create automatic routes. Use board-managed AI terminals for automatic collaboration."));
  if (!state.external.length) body.append(el("div", "empty-small", "No existing VS Code terminals."));
  for (const t of state.external) { const row = el("div", "external-row"); row.append(el("strong", "", t.name), button("Focus terminal", () => post("focusExternal", { id: t.id }))); const input = textInput("", "Text to paste (no automatic Enter)"); row.append(input, button("Paste", () => post("externalInput", { id: t.id, text: input.value }))); body.append(row); }
}
function editGroup(group: Group): void {
  const body = modal("Group settings"); const form = el("form"); const name = textInput(group.name); name.required = true;
  const goal = el("textarea"); goal.value = group.goal; goal.rows = 3;
  const max = textInput(String(group.maxDeliveries)); max.type = "number"; max.min = "1"; max.max = "100";
  const hops = textInput(String(group.maxHops)); hops.type = "number"; hops.min = "1"; hops.max = "12";
  field(form, "Group name", name); field(form, "Shared goal (optional)", goal); field(form, "Automatic delivery limit", max, `Consumed deliveries: ${group.used}. Manual input is separate.`); field(form, "Message hop limit", hops, "Bound two-way routes to prevent an endless loop.");
  const save = el("button", "primary", "Save settings"); save.type = "submit"; form.append(save); form.onsubmit = event => { event.preventDefault(); post("group", { id: group.id, name: name.value, goal: goal.value, maxDeliveries: Number(max.value), maxHops: Number(hops.value) }); closeModal(); }; body.append(form);
}
function updateHint(): void { $("#connect-hint").textContent = connecting ? "Click the target's left input dot. Press Esc to cancel." : "Connect an output dot to another terminal's input dot."; $("#connect-hint").classList.toggle("connecting", Boolean(connecting)); cards.forEach((c, id) => c.el.classList.toggle("connect-source", id === connecting)); }

function createCard(node: NodeConfig): void {
  const card = el("article", "terminal-card"); card.dataset.id = node.id;
  const header = el("div", "card-header");
  const badge = el("span", `provider-icon ${node.provider}`, node.provider === "claude" ? "\u2733" : node.provider === "codex" ? "\u2318" : ">_");
  const title = el("input", "node-name"); title.value = node.name; title.setAttribute("aria-label", "Terminal name"); title.onchange = () => post("rename", { id: node.id, name: title.value });
  const status = el("span", "phase"); const maximize = button("\u26f6", () => expandTerminal(node.id), "expand-button"); maximize.setAttribute("aria-label", "Expand terminal"); header.append(badge, title, status, maximize); card.append(header);
  const sub = el("div", "card-subtitle"); const profileText = el("span", "profile-name"); const group = el("select", "node-group"); group.setAttribute("aria-label", "Collaboration group"); group.onchange = () => post("nodeGroup", { id: node.id, groupId: group.value }); sub.append(profileText, group); card.append(sub);
  const inputPort = button("\u25cf", () => { if (connecting && connecting !== node.id) { post("connect", { source: connecting, target: node.id }); connecting = ""; updateHint(); } }, "port input-port"); inputPort.title = "Connect to this terminal"; inputPort.setAttribute("aria-label", "Input port"); inputPort.dataset.target = node.id;
  const outputPort = button("\u25cf", () => { connecting = node.id; updateHint(); }, "port output-port"); outputPort.title = "Connect to another terminal"; outputPort.setAttribute("aria-label", "Output port");
  outputPort.onpointerdown = () => { connecting = node.id; updateHint(); };
  inputPort.onpointerup = () => { if (connecting && connecting !== node.id) { post("connect", { source: connecting, target: node.id }); connecting = ""; updateHint(); } };
  card.append(inputPort, outputPort);
  const terminalArea = el("div", "terminal-area"); card.append(terminalArea);
  const terminal = new Terminal({ cursorBlink: true, fontSize: 12, fontFamily: "'Cascadia Mono', Consolas, monospace", scrollback: 3000, allowProposedApi: false, theme: { background: "#101419", foreground: "#d6e0e7", cursor: "#85e0b9", selectionBackground: "#284b45", black: "#131922", brightBlack: "#75828f", green: "#85e0b9", brightGreen: "#acf4d2" } });
  const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(terminalArea);
  terminal.parser.registerOscHandler(52, () => true);
  terminal.onData(data => post("input", { id: node.id, data }));
  // Track actual user gestures separately from terminal protocol responses.
  terminalArea.addEventListener("keydown", event => {
    if (["Shift", "Control", "Alt", "Meta"].includes(event.key)) return;
    if (event.isComposing || event.key === "Process" || event.keyCode === 229) return;
    const interrupt = event.ctrlKey && ["c", "d"].includes(event.key.toLowerCase());
    post("activity", { id: node.id, kind: interrupt ? "interrupt" : event.key === "Enter" && !event.shiftKey && !event.altKey ? "submit" : "edit" });
  }, true);
  terminalArea.addEventListener("paste", () => post("activity", { id: node.id, kind: "edit" }), true);
  terminalArea.addEventListener("compositionstart", () => post("activity", { id: node.id, kind: "edit" }), true);
  terminal.textarea?.addEventListener("focus", () => post("focus", { id: node.id, focused: true }));
  terminal.textarea?.addEventListener("blur", () => post("focus", { id: node.id, focused: false }));
  terminal.onResize(({ cols, rows }) => post("resize", { id: node.id, cols, rows }));
  const observer = new ResizeObserver(() => { try { fit.fit(); } catch { /* The first frame may not have a measured size. */ } }); observer.observe(terminalArea);
  const controls = el("div", "card-controls");
  const ready = button("Ready to receive", () => post("markReady", { id: node.id }), "ready-button"); ready.title = "Confirm that the CLI input is empty and ready for another request.";
  const start = button("Start", () => post("start", { id: node.id }), "start-button");
  const interrupt = button("Interrupt", () => post("interrupt", { id: node.id }), "interrupt-button");
  const stop = button("Stop", () => post("stop", { id: node.id }), "stop-button");
  const remove = button("Remove card", () => post("remove", { id: node.id }), "remove-button");
  controls.append(start, ready, interrupt, stop, remove); card.append(controls);
  card.append(el("div", "card-reason"));
  $("#nodes").append(card); cards.set(node.id, { el: card, terminal, fit, observer });
  if (!streams.has(node.id)) streams.set(node.id, { last: 0, waiting: state?.active.includes(node.id) === true });
  flushOutput(node.id);
  let drag: { startX: number; startY: number; x: number; y: number } | undefined;
  header.onpointerdown = event => { if ((event.target as HTMLElement).closest("input,button")) return; drag = { startX: event.clientX, startY: event.clientY, x: parseFloat(card.style.left), y: parseFloat(card.style.top) }; header.setPointerCapture(event.pointerId); };
  header.onpointermove = event => { if (!drag) return; node.x = Math.max(0, Math.min(4000, drag.x + (event.clientX - drag.startX) / zoom)); node.y = Math.max(0, Math.min(4000, drag.y + (event.clientY - drag.startY) / zoom)); card.style.left = `${node.x}px`; card.style.top = `${node.y}px`; drawWires(); };
  header.onpointerup = () => { if (!drag) return; drag = undefined; post("move", { id: node.id, x: node.x, y: node.y }); };
  requestAnimationFrame(() => { fit.fit(); });
}
function drawWires(): void {
  const svg = $("#wire-paths"); svg.replaceChildren(); if (!state) return;
  for (const edge of state.board.edges) {
    const a = cards.get(edge.source)?.el, b = cards.get(edge.target)?.el;
    if (!a || !b || a.hidden || b.hidden) continue;
    const x1 = parseFloat(a.style.left) + 530, y1 = parseFloat(a.style.top) + 150, x2 = parseFloat(b.style.left), y2 = parseFloat(b.style.top) + 150;
    const curve = Math.max(80, Math.abs(x2 - x1) / 2);
    const route = x2 >= x1 ? `M ${x1} ${y1} C ${x1 + curve} ${y1}, ${x2 - curve} ${y2}, ${x2} ${y2}` : `M ${x1} ${y1} C ${x1 + 60} ${y1}, ${x1 + 60} ${Math.max(y1, y2) + 285}, ${x1} ${Math.max(y1, y2) + 285} L ${Math.max(8, x2 - 24)} ${Math.max(y1, y2) + 285} L ${Math.max(8, x2 - 24)} ${y2 + 24} Q ${Math.max(8, x2 - 24)} ${y2}, ${x2} ${y2}`;
    const status = state.routes.find(r => r.id === edge.id);
    const paused = !edge.enabled || ["paused", "limited", "offline", "invalid"].includes(status?.state || "");
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", route); p.setAttribute("class", paused ? "wire paused" : "wire"); p.setAttribute("marker-end", "url(#arrow)");
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title"); title.textContent = `${status?.label || "Checking route state"} - click to pause or resume this route`; p.append(title); p.addEventListener("click", () => post("edge", { id: edge.id, enabled: !edge.enabled })); svg.append(p);
  }
}
function renderInspector(): void {
  const body = $("#inspector-body"); body.replaceChildren(); if (!state) return;
  if (inspectorTab === "links") {
    body.append(el("div", "section-label", "MESSAGE ROUTES"));
    if (!state.board.edges.length) body.append(el("div", "empty-small", "No routes yet. Click a terminal's right output dot, then the target's left input dot."));
    for (const e of state.board.edges) {
      const source = state.board.nodes.find(n => n.id === e.source), target = state.board.nodes.find(n => n.id === e.target);
      const row = el("div", "route"); row.append(el("div", "route-title", `${source?.name || "?"}  \u2192  ${target?.name || "?"}`));
      const waiting = state.deliveries.filter(d => d.edgeId === e.id && d.status === "queued").length;
      const route = state.routes.find(r => r.id === e.id);
      row.append(el("small", "muted", `${route?.label || "Checking route state"}${waiting ? ` \u00b7 ${waiting} pending` : ""}`));
      const actions = el("div", "route-actions"); actions.append(button(e.enabled ? "Pause" : "Resume", () => post("edge", { id: e.id, enabled: !e.enabled })), button("Disconnect", () => post("removeEdge", { id: e.id }), "danger-quiet")); row.append(actions); body.append(row);
    }
    body.append(el("div", "hint-box", "Messages wait while the peer is working. Entire terminal logs and login details are not shared automatically."));
  } else if (inspectorTab === "groups") {
    body.append(el("div", "section-label", "COLLABORATION TEAMS"));
    for (const g of state.board.groups) {
      const row = el("div", "group-row"); row.append(el("strong", "", g.name), el("p", "muted", g.goal || "A shared goal is included with routed messages."));
      const meter = el("div", "meter"); const fill = el("i"); fill.style.width = `${Math.min(100, g.used / g.maxDeliveries * 100)}%`; meter.append(fill); row.append(meter, el("small", "", `Automatic deliveries ${g.used}/${g.maxDeliveries} - max ${g.maxHops} hops`));
      const actions = el("div", "route-actions"); actions.append(button(g.paused ? "Resume routes" : "Pause", () => post("group", { id: g.id, paused: !g.paused })), button("Settings", () => editGroup(g)), button("New round", () => post("newRound", { id: g.id }))); row.append(actions); body.append(row);
    }
  } else {
    body.append(el("div", "section-label", "DELIVERY JOURNAL"));
    if (!state.deliveries.length && !state.notices.length) body.append(el("div", "empty-small", "AI responses and route state appear here."));
    for (const d of [...state.deliveries].reverse()) {
      const row = el("details", `delivery ${d.status}`); row.append(el("summary", "", `${statusLabels[d.status]} \u00b7 ${d.hops} hops`));
      const source = state.board.nodes.find(n => n.id === d.source)?.name || "Stopped terminal", target = state.board.nodes.find(n => n.id === d.target)?.name || "Stopped terminal";
      row.append(el("small", "muted", `${source} \u2192 ${target}`), el("pre", "", d.text), el("small", "", d.reason)); body.append(row);
    }
    for (const n of state.notices.slice(0, 20)) { const row = el("div", "notice"); row.append(el("time", "", new Date(n.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })), el("span", "", n.text)); body.append(row); }
  }
}
function render(): void {
  if (!state) return;
  const project = $<HTMLSelectElement>("#project"), previous = project.value;
  project.replaceChildren(...state.projects.map(p => option(p, p.split(/[\\/]/).pop() || p))); if (state.projects.includes(previous)) project.value = previous;
  if (!state.projects.length) project.append(option("", "Open a project folder"));
  const banner = $("#banner"); banner.hidden = state.trusted && !state.remote;
  banner.textContent = !state.trusted ? "Restricted mode. Trust this workspace before running terminals." : state.remote ? "Use local VS Code. Remote terminals are not yet supported." : "";
  const ids = new Set(state.board.nodes.map(n => n.id)); for (const [id, c] of cards) if (!ids.has(id)) { if (expandedId === id) closeExpanded(); c.observer.disconnect(); c.terminal.dispose(); c.el.remove(); cards.delete(id); }
  for (const n of state.board.nodes) {
    if (!cards.has(n.id)) createCard(n);
    const c = cards.get(n.id)!; const r = state.runtime.find(r => r.id === n.id); const phase = r?.phase || "stopped";
    c.el.style.left = `${n.x}px`; c.el.style.top = `${n.y}px`; c.el.hidden = n.project !== selectedProject();
    const title = c.el.querySelector<HTMLInputElement>(".node-name")!; if (document.activeElement !== title) title.value = n.name;
    c.el.querySelector(".phase")!.textContent = phaseLabels[phase]; c.el.querySelector(".phase")!.className = `phase ${phase}`;
    const p = state.profiles.find(p => p.id === n.profileId); c.el.querySelector(".profile-name")!.textContent = p ? `${p.command} \u00b7 ${p.shell}` : "Missing profile";
    const group = c.el.querySelector<HTMLSelectElement>(".node-group")!; if (document.activeElement !== group) { group.replaceChildren(...state.board.groups.map(g => option(g.id, g.name))); group.value = n.groupId; }
    const running = state.active.includes(n.id);
    c.el.querySelector<HTMLElement>(".start-button")!.hidden = running;
    c.el.querySelector<HTMLElement>(".remove-button")!.hidden = running;
    c.el.querySelector<HTMLElement>(".stop-button")!.hidden = !running;
    c.el.querySelector<HTMLElement>(".interrupt-button")!.hidden = !running;
    c.el.querySelector<HTMLElement>(".ready-button")!.hidden = !running || n.provider === "terminal";
    c.el.querySelector(".card-reason")!.textContent = r?.reason || (r?.dirty ? "Human input in progress; delivery waits" : r?.focused ? "Terminal focused; click elsewhere to allow delivery" : n.provider === "terminal" ? "Ordinary terminal; manual input only" : "Check that the input is empty, then click Ready to receive.");
  }
  const visible = state.board.nodes.filter(n => n.project === selectedProject()); $("#empty").hidden = visible.length > 0;
  $("#counts").textContent = `${visible.length} terminals - ${state.board.edges.length} routes`;
  const allPaused = state.board.groups.every(g => g.paused); $("#pause-all").textContent = allPaused ? "Resume routes" : "Pause routes";
  $("#footer-state").textContent = `${state.version} \u00b7 ${state.runtime.filter(r => !["stopped", "error"].includes(r.phase)).length} active - existing local CLI authentication`;
  drawWires(); renderInspector(); updateHint();
}

$("#add").onclick = $("#first-add").onclick = () => openAdd();
$("#profiles").onclick = () => openProfiles(); $("#external").onclick = openExternal;
$("#stop-all").onclick = () => post("stopAll");
$("#new-group").onclick = () => { const body = modal("Add collaboration group"); const name = textInput("", "Example: implementation and review"); name.required = true; const form = el("form"); field(form, "Group name", name); const submit = el("button", "primary", "Create group"); submit.type = "submit"; form.append(submit); form.onsubmit = event => { event.preventDefault(); post("newGroup", { name: name.value }); inspectorTab = "groups"; closeModal(); }; body.append(form); };
$("#pause-all").onclick = () => { if (!state) return; const paused = !state.board.groups.every(g => g.paused); state.board.groups.forEach(g => post("group", { id: g.id, paused })); };
$<HTMLSelectElement>("#project").onchange = () => render();
document.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach(b => b.onclick = () => { inspectorTab = b.dataset.tab!; document.querySelectorAll("[data-tab]").forEach(t => t.classList.toggle("active", t === b)); renderInspector(); });
function setZoom(value: number): void { zoom = Math.min(1.3, Math.max(0.5, value)); $("#world").style.transform = `scale(${zoom})`; $("#zoom-label").textContent = `${Math.round(zoom * 100)}%`; }
$("#zoom-out").onclick = () => setZoom(zoom - 0.1); $("#zoom-in").onclick = () => setZoom(zoom + 0.1); $("#zoom-label").onclick = () => setZoom(0.9);
$("#arrange").onclick = () => state?.board.nodes.filter(n => n.project === selectedProject()).forEach((n, i) => post("move", { id: n.id, x: 32 + i % 2 * 570, y: 32 + Math.floor(i / 2) * 430 }));
window.addEventListener("message", event => {
  const m = event.data;
  if (m.type === "state") { state = m.value; workflowPanel.update(state); render(); if (coreOpen) renderCore(); }
  else if (m.type === "output") { pendingOutput.set(m.id, [...(pendingOutput.get(m.id) || []), ...m.chunks]); flushOutput(m.id); }
  else if (m.type === "reset") {
    streams.set(m.id, { last: 0, waiting: false }); pendingOutput.delete(m.id); const card = cards.get(m.id);
    if (card) { card.terminal.reset(); card.fit.fit(); post("resize", { id: m.id, cols: card.terminal.cols, rows: card.terminal.rows }); }
  }
  else if (m.type === "snapshot") {
    const card = cards.get(m.id); if (card) {
      streams.set(m.id, { last: m.sequence, waiting: true }); card.terminal.reset(); card.terminal.resize(m.cols, m.rows);
      card.terminal.write(m.data, () => { streams.set(m.id, { last: m.sequence, waiting: false }); card.fit.fit(); flushOutput(m.id); });
    }
  }
  else if (m.type === "error") toast(m.text);
  else if (m.type === "savedProfile") { toast("Launch profile saved."); closeModal(); openAdd(m.id); }
  else if (m.type === "page") { if (m.page === "profiles") openProfiles(); else if (m.page === "external") openExternal(); else openAdd(); }
});
setZoom(0.9); post("ready");
