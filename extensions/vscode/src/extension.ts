import * as vscode from "vscode";
import * as path from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { Board, NodeConfig, Profile, Router, bounded, defaultBoard, restoreBoard, validateProfile, explicitTerminalText, projectKey } from "./model";
import { SessionHost } from "./session";
import { CoreClient, Ledger, endpointProfiles } from "./core";
import { affectedPaths, dirtyConflicts, resumeProfile, runtimeReport, workflowActions } from "./workflow";
import { StateStore } from "./stateStore";

let controller: Controller | undefined;
const defaultProfiles = (): Profile[] => [
  { id: "claude", name: "Claude Code", provider: "claude", command: "claude", args: [], shell: process.platform === "win32" ? "powershell" : "bash" },
  { id: "codex", name: "Codex", provider: "codex", command: "codex", args: [], shell: process.platform === "win32" ? "powershell" : "bash" },
  { id: "shell", name: "Shell", provider: "terminal", command: process.platform === "win32" ? "powershell.exe" : "bash", args: [], shell: "direct" }
];

class Controller implements vscode.TreeDataProvider<vscode.TreeItem>, vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private treeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.treeEmitter.event;
  readonly router: Router;
  readonly host: SessionHost;
  readonly core: CoreClient;
  readonly ledger: Ledger;
  readonly stateStore: StateStore;
  private savedConfigs: Record<string, string> = {};
  private savedVersion = "";
  private persistenceError = "";
  private deliverySnapshots = new Map<string, string>();
  private coreView: any = { state: "idle" };
  private workflowView: any = { state: "idle" };
  private workflowBusy = false;
  private workflowRevision = 0;
  private workflowPlan?: { id: string; project: string; action: string; args: Record<string, any>; report: any; expires: number };
  private runtimeView: any = { state: "unchecked" };
  private saveTimer?: NodeJS.Timeout;
  private stateTimer?: NodeJS.Timeout;
  private outputBatches = new Map<string, { sequence: number; data: string }[]>();
  private outputTimer?: NodeJS.Timeout;
  private disposed = false;
  private external = new Map<string, vscode.Terminal>();
  private externalIds = new WeakMap<vscode.Terminal, string>();
  private subscriptions: vscode.Disposable[] = [];
  constructor(readonly context: vscode.ExtensionContext) {
    this.stateStore = new StateStore(context.globalStorageUri.fsPath, this.projects());
    let snapshot: any;
    try { snapshot = this.stateStore.load(); } catch (error) { this.persistenceError = String(error); }
    this.savedConfigs = snapshot?.configs || {}; this.savedVersion = snapshot?.version || "";
    const board = restoreBoard(snapshot?.board || context.workspaceState.get("board.v1"), this.projects());
    this.core = new CoreClient(vscode.workspace.getConfiguration("patchport").get<string>("corePython", "python"), path.join(context.extensionPath, "core"));
    this.ledger = new Ledger(this.core, path.join(context.globalStorageUri.fsPath, "ledgers"), () => this.changed());
    this.router = new Router(board, async (id, text) => {
      const delivery = this.router.deliveries.find(d => d.target === id && d.status === "sent");
      if (!delivery) throw new Error("The delivery ledger is missing.");
      await this.ledger.delivery({ ...delivery, status: "queued" });
      await this.ledger.record(id, "input_attempt", { delivery_id: delivery.id, origin: "peer" });
      await this.ledger.delivery(delivery);
      const target = this.router.runtime.get(id);
      if (!target || target.generation !== delivery.generation || target.phase !== "busy" || target.dirty || target.focused) throw new Error("Target input state changed while preparing delivery.");
      this.host.send(id, text);
    }, () => this.changed());
    this.host = new SessionHost(this.router, path.join(context.globalStorageUri.fsPath, "sessions"), context.extensionPath, this.ledger);
    this.host.on("output", (id: string, data: string, sequence: number) => {
      const chunks = this.outputBatches.get(id) || []; chunks.push({ sequence, data }); this.outputBatches.set(id, chunks);
      if (!this.outputTimer) this.outputTimer = setTimeout(() => { this.outputTimer = undefined; for (const [nodeId, content] of this.outputBatches) this.post({ type: "output", id: nodeId, chunks: content }); this.outputBatches.clear(); }, 24);
    });
    this.host.on("started", (id: string) => { this.post({ type: "reset", id }); this.changed(); });
    this.subscriptions.push(vscode.workspace.onDidChangeConfiguration(e => { if (e.affectsConfiguration("patchport.profiles")) this.changed(); }), vscode.window.onDidOpenTerminal(() => this.changed()), vscode.window.onDidCloseTerminal(() => this.changed()), vscode.workspace.onDidGrantWorkspaceTrust(() => this.changed()));
  }
  projects(): string[] { return (vscode.workspace.workspaceFolders || []).filter(f => f.uri.scheme === "file").map(f => f.uri.fsPath); }
  private selectedConfig(project: string): string | undefined {
    const canonical = `core.config.${projectKey(project)}`;
    const legacy = this.context.workspaceState.keys().find(key => key.startsWith("core.config.") && projectKey(key.slice("core.config.".length)) === projectKey(project));
    return this.savedConfigs[projectKey(project)] || this.context.workspaceState.get<string>(canonical) || (legacy ? this.context.workspaceState.get<string>(legacy) : undefined);
  }
  profiles(): Profile[] {
    const configured = vscode.workspace.getConfiguration("patchport").get<unknown[]>("profiles", []);
    const values = new Map(defaultProfiles().map(p => [p.id, p]));
    for (const raw of configured) { try { const p = validateProfile(raw); values.set(p.id, p); } catch { /* Invalid settings are shown by the profile editor on save. */ } }
    return [...values.values()];
  }
  getState(): unknown {
    this.external.clear();
    vscode.window.terminals.forEach(t => { let id = this.externalIds.get(t); if (!id) { id = randomUUID(); this.externalIds.set(t, id); } this.external.set(id, t); });
    return {
      board: this.router.board, runtime: [...this.router.runtime.values()], active: this.router.board.nodes.filter(n => this.host.isActive(n.id)).map(n => n.id), profiles: this.profiles(), projects: this.projects(),
      trusted: vscode.workspace.isTrusted, supported: !vscode.env.remoteName && process.platform === "win32", platform: process.platform, remote: vscode.env.remoteName || "",
      deliveries: this.router.deliveries.slice(-100).map(d => ({ ...d, text: d.text.slice(0, 2000) })), notices: this.router.notices, routes: this.router.board.edges.map(e => this.router.routeStatus(e)),
      external: [...this.external].map(([id, t]) => ({ id, name: t.name, capability: "focus-and-explicit-input" })), version: this.context.extension.packageJSON.version, core: this.coreView,
      workflow: this.workflowView, lifecycle: { ...this.runtimeView, active: this.host.sessions.size, previousVersion: this.savedVersion || this.context.workspaceState.get("runtime.version"), restartRequired: (this.savedVersion || this.context.workspaceState.get("runtime.version")) !== this.context.extension.packageJSON.version, persistence: this.persistenceError || "READY" }
    };
  }
  private post(value: unknown): void { void this.panel?.webview.postMessage(value); }
  private changed(): void {
    if (this.disposed) return;
    for (const delivery of this.router.deliveries) {
      const key = JSON.stringify(delivery);
      if (this.deliverySnapshots.get(delivery.id) === key || delivery.status === "sent") continue;
      this.deliverySnapshots.set(delivery.id, key);
      const project = this.router.board.nodes.find(n => n.id === delivery.target)?.project;
      void this.ledger?.delivery(delivery, project).catch(() => {
        for (const group of this.router.board.groups) group.paused = true;
        this.coreView = { ...this.coreView, error: "Delivery recording failed; routes have been paused." };
        this.post({ type: "state", value: this.getState() });
      });
    }
    if (!this.stateTimer) this.stateTimer = setTimeout(() => { this.stateTimer = undefined; this.post({ type: "state", value: this.getState() }); this.treeEmitter.fire(); }, 40);
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => { this.saveTimer = undefined; this.saveState(); }, 200);
  }
  private saveState(): void {
    void this.context.workspaceState.update("board.v1", this.router.board);
    for (const project of this.projects()) { const selected = this.selectedConfig(project); if (selected) this.savedConfigs[projectKey(project)] = selected; }
    try { this.stateStore.save(this.router.board, this.savedConfigs, this.context.extension.packageJSON.version); this.persistenceError = ""; }
    catch (error) { this.persistenceError = String(error); this.post({ type: "state", value: this.getState() }); }
  }
  private requireWorkspace(): void {
    if (!vscode.workspace.isTrusted) throw new Error("Trust the VS Code workspace before starting terminals.");
    if (!this.projects().length) throw new Error("Open a local project folder first.");
    if (vscode.env.remoteName) throw new Error("Use local VS Code. Remote and WSL extension sessions are not yet validated.");
  }
  private async refreshCore(project: string, config?: string, days?: number, groupBy = "endpoint"): Promise<void> {
    project = this.projects().find(p => projectKey(p) === projectKey(project)) || project;
    this.requireWorkspace(); if (!this.projects().includes(project)) throw new Error("Select an open project.");
    this.coreView = { state: "loading", project }; this.changed();
    try {
      const binding = await this.ledger.inspect(project, config || this.selectedConfig(project));
      const [tasks, usage, history] = await Promise.all([
        binding.linked ? this.core.request("tasks.list", { config: binding.config }) : Promise.resolve({ tasks: [] }),
        binding.linked ? this.core.request("usage", { config: binding.config, ...(days ? { days } : {}), group_by: groupBy }) : Promise.resolve(null),
        this.ledger.report(project, days)
      ]);
      for (const group of this.router.board.groups) group.used = Math.max(group.used, history.group_attempts?.[`${group.id}:${group.round || "legacy"}`] || 0);
      this.coreView = { state: "ready", project, binding, tasks, usage, history, days, groupBy };
      this.changed();
    } catch (error) { this.coreView = { state: "error", project, error: String(error) }; this.changed(); }
  }
  private dirtyFiles(): string[] {
    const files = vscode.workspace.textDocuments.filter(d => d.isDirty && d.uri.scheme === "file").map(d => d.uri.fsPath);
    for (const group of vscode.window.tabGroups.all) for (const tab of group.tabs) {
      const input = tab.input as { uri?: vscode.Uri };
      if (tab.isDirty && input?.uri?.scheme === "file") files.push(input.uri.fsPath);
    }
    return [...new Set(files)];
  }
  private checkDirty(plan: { project: string; action: string; args: Record<string, any>; report: any }): void {
    const paths = affectedPaths(plan.action, plan.args, plan.report);
    const roots = [plan.project];
    if (plan.report.workspace) roots.push(path.join(plan.report.workspace, "project"));
    if (plan.args.backup && plan.report.workspace) roots.push(path.join(plan.report.workspace, plan.args.backup));
    if (plan.action === "roles.prune.preview" && plan.report.workspace && dirtyConflicts([plan.report.workspace], ["."], this.dirtyFiles()).length) throw new Error("The role copy has unsaved edits. Save or revert them, then preview again.");
    const conflicts = dirtyConflicts(roots, [...paths, "bridge.json"], this.dirtyFiles());
    if (conflicts.length) throw new Error(`Save or revert unsaved edits, then preview again: ${conflicts.join(", ")}`);
  }
  private async cancelPlan(): Promise<void> {
    this.workflowRevision++;
    const plan = this.workflowPlan; this.workflowPlan = undefined;
    if (plan) await this.core.request("operation.cancel", { plan_id: plan.id }).catch(() => {});
  }
  async cancelWorkflow(): Promise<void> { await this.cancelPlan(); this.workflowView = { state: "idle" }; this.changed(); }
  async workflow(project: string, action: string, rawArgs: Record<string, unknown> = {}): Promise<any> {
    project = this.projects().find(p => projectKey(p) === projectKey(project)) || project;
    this.requireWorkspace();
    if (!this.projects().includes(project) || !workflowActions.has(action)) throw new Error("Select an open project and a supported action.");
    if (this.workflowBusy) throw new Error("A management action is running. Wait for its outcome.");
    this.workflowBusy = true;
    this.workflowView = { ...this.workflowView, state: "loading", project, action, error: undefined }; this.changed();
    try {
      await this.cancelPlan();
      const revision = this.workflowRevision;
      const binding = await this.ledger.inspect(project, this.selectedConfig(project));
      if (!binding.linked) throw new Error("Connect bridge.json first. Select its file in Jobs & usage.");
      const capabilities = await this.core.request("capabilities");
      if (!capabilities.actions?.includes(action)) throw new Error("This core lacks project workflow features. Install a verified extension build.");
      const args = { ...rawArgs, config: binding.config };
      const result = await this.core.request(action, args);
      if (revision !== this.workflowRevision) { if (result.plan_id) await this.core.request("operation.cancel", { plan_id: result.plan_id }); throw new Error("Input or project changed. Make a new preview."); }
      if (result.plan_id) {
        const plan = { id: result.plan_id, project, action, args, report: result.report, expires: Date.now() + result.expires_in * 1000 };
        try { this.checkDirty(plan); } catch (error) { await this.core.request("operation.cancel", { plan_id: plan.id }); throw error; }
        this.workflowPlan = plan;
      }
      this.workflowView = { state: "ready", project, action, result, plan: Boolean(this.workflowPlan), expires: this.workflowPlan?.expires };
      this.changed(); return result;
    } catch (error) { this.workflowView = { state: "error", project, action, error: String(error) }; this.changed(); throw error; }
    finally { this.workflowBusy = false; }
  }
  async applyWorkflow(confirm = true): Promise<any> {
    this.requireWorkspace(); const plan = this.workflowPlan;
    if (!plan || this.workflowBusy || Date.now() >= plan.expires) throw new Error("Make a new, valid preview.");
    this.checkDirty(plan);
    if (confirm && await vscode.window.showWarningMessage(plan.action === "roles.preview" ? "Start the role workflow? It runs at most two provider tasks and the configured test commands." : "Apply the reviewed preview?", { modal: true, detail: JSON.stringify(plan.report, null, 2).slice(0, 4000) }, "Apply") !== "Apply") return;
    if (this.workflowPlan?.id !== plan.id || !this.projects().includes(plan.project)) throw new Error("The preview changed. Review a new plan.");
    this.checkDirty(plan); this.workflowBusy = true; this.workflowPlan = undefined;
    this.workflowView = { ...this.workflowView, state: "loading", plan: false }; this.changed();
    try {
      const result = await this.core.request("operation.apply", { plan_id: plan.id, apply: true });
      this.workflowView = { state: "ready", project: plan.project, action: "operation.apply", result, plan: false };
      await this.refreshCore(plan.project); this.changed(); return result;
    } catch (error) { this.workflowView = { state: "error", project: plan.project, action: plan.action, plan: false, error: `${String(error)} - inspect results, backups and archives first.` }; this.changed(); throw error; }
    finally { this.workflowBusy = false; }
  }
  private async checkRuntime(): Promise<void> {
    this.requireWorkspace();
    try {
      const [runtime, capabilities] = await Promise.all([runtimeReport(this.context.extensionPath), this.core.request("capabilities")]);
      if (!capabilities.actions?.includes("batch.preview")) throw new Error("The required workflow runtime is missing.");
      this.runtimeView = { state: "ready", ...runtime, capabilities, corePython: this.core.python };
      if (!this.host.sessions.size) { this.savedVersion = this.context.extension.packageJSON.version; await this.context.workspaceState.update("runtime.version", this.savedVersion); }
    } catch (error) { this.runtimeView = { state: "error", error: String(error) }; }
    this.changed();
  }
  open(): void {
    if (this.panel) { this.panel.reveal(); return; }
    this.panel = vscode.window.createWebviewPanel("patchport.board", "PatchPort - AI terminals", vscode.ViewColumn.One, { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, "dist"), vscode.Uri.joinPath(this.context.extensionUri, "media")] });
    this.panel.webview.html = this.html(this.panel.webview).replace("</head>", `<link rel=\"stylesheet\" href=\"${this.panel.webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "media/p0.css"))}\"></head>`);
    this.panel.onDidDispose(() => { this.panel = undefined; for (const r of this.router.runtime.values()) r.focused = false; if (!this.disposed) this.router.drain(); });
    this.panel.webview.onDidReceiveMessage(message => void this.handle(message).catch(error => { this.post({ type: "error", text: error instanceof Error ? error.message : String(error) }); }));
    this.panel.onDidChangeViewState(event => { if (!event.webviewPanel.visible) { for (const r of this.router.runtime.values()) r.focused = false; this.router.drain(); } });
  }
  async refreshProject(project: string): Promise<void> { await this.refreshCore(project); }
  openGuide(): void { void vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.joinPath(this.context.extensionUri, "docs/USER_GUIDE.md")); }
  showPage(page: string): void { this.open(); setTimeout(() => this.post({ type: "page", page }), 150); }
  async add(profileId: string, project: string, groupId: string): Promise<string> {
    project = this.projects().find(p => projectKey(p) === projectKey(project)) || project;
    this.requireWorkspace();
    if (!this.projects().includes(project)) throw new Error("Select a project from the current workspace.");
    const profile = this.profiles().find(p => p.id === profileId); if (!profile) throw new Error("The launch profile was not found.");
    if (this.router.board.nodes.length >= 24) throw new Error("Remove unused terminal cards first.");
    const group = this.router.board.groups.find(g => g.id === groupId) || this.router.board.groups[0];
    const index = this.router.board.nodes.length;
    const node: NodeConfig = { id: randomUUID(), name: profile.name, provider: profile.provider, profileId, project, groupId: group.id, x: 32 + (index % 2) * 570, y: 32 + Math.floor(index / 2) * 430 };
    this.router.board.nodes.push(node); this.changed();
    await this.startNode(node, profile); return node.id;
  }
  private async startNode(node: NodeConfig, profile: Profile): Promise<void> {
    await this.checkRuntime();
    if (this.runtimeView.state === "error") throw new Error(this.runtimeView.error);
    await this.refreshCore(node.project);
    if (this.coreView.state === "error") throw new Error(this.coreView.error);
    await this.host.start(node, profile);
  }
  async stopAll(confirm = true): Promise<void> {
    if (confirm && this.host.sessions.size && await vscode.window.showWarningMessage("Stop all terminals started by PatchPort?", { modal: true }, "Stop") !== "Stop") return;
    for (const id of [...this.host.sessions.keys()]) this.host.stop(id);
  }
  private async handle(raw: unknown): Promise<void> {
    if (!raw || typeof raw !== "object") return;
    const m = raw as Record<string, unknown>;
    const id = typeof m.id === "string" ? m.id : "";
    if (typeof m.type !== "string") return;
    if (m.type === "ready") {
      for (const r of this.router.runtime.values()) r.focused = false;
      this.post({ type: "state", value: this.getState() });
      for (const nodeId of this.host.sessions.keys()) { const snapshot = await this.host.snapshot(nodeId); if (snapshot) this.post({ type: "snapshot", id: nodeId, ...snapshot }); }
      return;
    }
    if (m.type === "focusExternal") { this.external.get(id)?.show(); return; }
    if (m.type === "openFolder") { await vscode.commands.executeCommand("vscode.openFolder"); return; }
    if (m.type === "openGuide") { this.openGuide(); return; }
    if (m.type === "corePythonSettings") { await vscode.commands.executeCommand("workbench.action.openSettings", "patchport.corePython"); return; }
    this.requireWorkspace();
    const node = this.router.board.nodes.find(n => n.id === id);
    switch (m.type) {
      case "workflow": if (typeof m.project === "string" && typeof m.action === "string" && m.args && typeof m.args === "object") await this.workflow(m.project, m.action, m.args as Record<string, unknown>); break;
      case "workflowApply": await this.applyWorkflow(); break;
      case "workflowCancel": await this.cancelWorkflow(); break;
      case "runtimeRefresh": await this.checkRuntime(); break;
      case "resumeSession": {
        if (typeof m.project !== "string" || !this.projects().includes(m.project) || typeof m.sessionId !== "string") break;
        const binding = await this.ledger.inspect(m.project, this.selectedConfig(m.project));
        const info = await this.core.request("interactive.resume", { database: binding.database, project: binding.project, session_id: m.sessionId });
        const resumed = this.router.board.nodes.find(n => n.id === info.node && n.project === info.project);
        if (!resumed || this.host.isActive(resumed.id)) throw new Error("Stop the original terminal card before resuming it.");
        const profile = this.profiles().find(p => p.id === resumed.profileId);
        if (!profile || profile.provider !== info.provider) throw new Error("Check the original AI launch profile.");
        if (await vscode.window.showInformationMessage("Resume this provider conversation? End it in other windows first. Previous prompts and uncertain deliveries will not be resent.", { modal: true, detail: info.provider_session }, "Resume") !== "Resume") break;
        await this.startNode(resumed, resumeProfile(profile, info.provider_session)); break;
      }
      case "runtimeReload": {
        if (this.host.sessions.size) throw new Error("Finish and stop active terminals before reloading the window.");
        await this.checkRuntime();
        if (this.runtimeView.state !== "ready") throw new Error("Pass the runtime check first.");
        if (await vscode.window.showInformationMessage("Reload the VS Code window while preserving the board and recorded history?", { modal: true }, "Reload") === "Reload") { this.saveState(); if (this.persistenceError) throw new Error(this.persistenceError); await this.context.workspaceState.update("board.v1", this.router.board); await vscode.commands.executeCommand("workbench.action.reloadWindow"); } break;
      }
      case "runnerControl": {
        const project = String(m.project); if (!this.projects().includes(project)) break;
        const binding = await this.ledger.inspect(project, this.selectedConfig(project));
        if (!binding.linked || !["start", "stop"].includes(String(m.action))) break;
        if (await vscode.window.showWarningMessage(m.action === "start" ? "Start the queue runner? Pending tasks may execute." : "Stop the selected runner?", { modal: true }, "Apply") !== "Apply") break;
        const status = await this.core.request("runner.status", { config: binding.config });
        const runId = status.management?.run_id;
        const result = await this.core.request(`runner.${m.action}`, { config: binding.config, apply: true, ...(m.action === "stop" ? { run_id: runId, cancel_active: m.cancelActive === true } : {}) });
        this.workflowView = { state: "ready", project, action: `runner.${m.action}`, result }; await this.refreshCore(project); this.changed(); break;
      }
      case "coreRefresh": await this.refreshCore(String(m.project || this.projects()[0]), undefined, typeof m.days === "number" ? m.days : undefined, typeof m.groupBy === "string" ? m.groupBy : "endpoint"); break;
      case "coreConfig": {
        const project = String(m.project || this.projects()[0]);
        const files = await vscode.window.showOpenDialog({ canSelectMany: false, filters: { "Bridge configuration": ["json"] } });
        if (files?.[0]) { await this.refreshCore(project, files[0].fsPath); if (this.coreView.state === "ready") { this.savedConfigs[projectKey(project)] = files[0].fsPath; await this.context.workspaceState.update(`core.config.${projectKey(project)}`, files[0].fsPath); this.changed(); } } break;
      }
      case "coreResult": {
        const binding = this.ledger.binding(String(m.project)); if (!binding?.linked || typeof m.taskId !== "string") break;
        const result = await this.core.request("result", { config: binding.config, task_id: m.taskId });
        if (this.coreView.project === m.project) { this.coreView = { ...this.coreView, result }; this.changed(); } break;
      }
      case "coreEvent": {
        const binding = this.ledger.binding(String(m.project)); if (!binding || typeof m.eventId !== "string") break;
        const result = await this.core.request("interactive.event", { database: binding.database, project: binding.project, event_id: m.eventId });
        if (this.coreView.project === m.project) { this.coreView = { ...this.coreView, event: result }; this.changed(); } break;
      }
      case "coreImport": {
        const binding = this.ledger.binding(String(m.project)); if (!binding?.linked) break;
        if (await vscode.window.showInformationMessage("Import command and shell settings from bridge.json into launch profiles?", { modal: true }, "Import") !== "Import") break;
        const existing = vscode.workspace.getConfiguration("patchport").get<Profile[]>("profiles", []), imported = endpointProfiles(binding);
        await vscode.workspace.getConfiguration("patchport").update("profiles", [...existing.filter(p => !imported.some(i => i.id === p.id)), ...imported], vscode.ConfigurationTarget.Global); this.changed(); break;
      }
      case "coreControl": {
        const binding = this.ledger.binding(String(m.project)); if (!binding?.linked || !["pause", "resume", "limit"].includes(String(m.action))) break;
        const action = String(m.action);
        if (await vscode.window.showWarningMessage("Change existing batch queue controls? These do not limit interactive terminals or token charges.", { modal: true }, "Apply") !== "Apply") break;
        await this.core.request(action, { config: binding.config, apply: true, ...(action === "limit" ? { max_calls: m.maxCalls } : {}) }); await this.refreshCore(binding.project, binding.config, this.coreView.days, this.coreView.groupBy); break;
      }
      case "add": if (typeof m.profileId === "string" && typeof m.project === "string") await this.add(m.profileId, m.project, String(m.groupId || "team")); break;
      case "start": {
        if (!node) break; const p = this.profiles().find(p => p.id === node.profileId); if (!p) throw new Error("The launch profile is missing.");
        if (p.provider !== node.provider) throw new Error("The profile provider changed. Create a new card.");
        await this.startNode(node, p); break;
      }
      case "input": if (node && typeof m.data === "string") this.host.input(id, m.data, false); break;
      case "activity": if (node && (m.kind === "edit" || m.kind === "submit" || m.kind === "interrupt")) this.host.activity(id, m.kind); break;
      case "resize": if (node && typeof m.cols === "number" && typeof m.rows === "number") this.host.resize(id, m.cols, m.rows); break;
      case "focus": { const r = this.router.runtime.get(id); if (r) { r.focused = m.focused === true; if (!r.focused) this.router.drain(); this.changed(); } break; }
      case "markReady": if (node && node.provider !== "terminal") this.router.ready(id, true); break;
      case "interrupt": if (node) this.host.input(id, "\u0003"); break;
      case "stop": if (node && await vscode.window.showWarningMessage(`${node.name}: stop this terminal?`, { modal: true }, "Stop") === "Stop") this.host.stop(id); break;
      case "remove": if (node) { if (this.host.isActive(id)) throw new Error("Stop this terminal first."); for (const e of [...this.router.board.edges]) if (e.source === id || e.target === id) this.router.removeEdge(e.id); this.router.board.nodes = this.router.board.nodes.filter(n => n.id !== id); this.router.runtime.delete(id); this.changed(); } break;
      case "move": if (node) { node.x = bounded(m.x, 0, 4000, node.x); node.y = bounded(m.y, 0, 4000, node.y); this.changed(); } break;
      case "rename": if (node && typeof m.name === "string" && m.name.trim()) { node.name = m.name.slice(0, 80); this.changed(); } break;
      case "nodeGroup": if (node && typeof m.groupId === "string" && this.router.board.groups.some(g => g.id === m.groupId)) { for (const e of [...this.router.board.edges]) if (e.source === id || e.target === id) this.router.removeEdge(e.id); node.groupId = m.groupId; this.changed(); } break;
      case "connect": if (typeof m.source === "string" && typeof m.target === "string") { this.router.connect(m.source, m.target); this.router.note("Connected. Subsequent completed responses can be delivered."); } break;
      case "edge": { const e = this.router.board.edges.find(e => e.id === id); if (e) { e.enabled = m.enabled === true; this.changed(); this.router.drain(); } break; }
      case "removeEdge": this.router.removeEdge(id); break;
      case "group": {
        const group = this.router.board.groups.find(g => g.id === id); if (!group) break;
        if (typeof m.paused === "boolean") group.paused = m.paused;
        if (typeof m.name === "string" && m.name.trim()) group.name = m.name.slice(0, 80);
        if (typeof m.goal === "string") group.goal = m.goal.slice(0, 4000);
        if (typeof m.maxDeliveries === "number") group.maxDeliveries = bounded(m.maxDeliveries, 1, 100, 12);
        if (typeof m.maxHops === "number") group.maxHops = bounded(m.maxHops, 1, 12, 4);
        this.changed(); this.router.drain(); break;
      }
      case "newGroup": if (this.router.board.groups.length < 20) { this.router.board.groups.push({ ...defaultBoard().groups[0], id: randomUUID(), name: typeof m.name === "string" && m.name.trim() ? m.name.slice(0, 80) : "New group" }); this.changed(); } break;
      case "newRound": { const g = this.router.board.groups.find(g => g.id === id); if (g && await vscode.window.showInformationMessage(`${g.name}: reset its delivery count and start a new round?`, { modal: true }, "New round") === "New round") { for (const d of this.router.deliveries) if (d.status === "queued" && this.router.board.nodes.some(n => n.id === d.target && n.groupId === g.id)) { d.status = "cancelled"; d.reason = "New round started"; } g.used = 0; g.round = randomUUID(); g.paused = false; this.changed(); } break; }
      case "profile": {
        const p = validateProfile(m.profile);
        const values = vscode.workspace.getConfiguration("patchport").get<Profile[]>("profiles", []).filter(old => old.id !== p.id);
        values.push(p); await vscode.workspace.getConfiguration("patchport").update("profiles", values, vscode.ConfigurationTarget.Global);
        this.router.note(`${p.name}: launch profile saved.`); this.post({ type: "state", value: this.getState() }); this.post({ type: "savedProfile", id: p.id }); this.changed(); break;
      }
      case "externalInput": {
        const terminal = this.external.get(id); if (!terminal) throw new Error("The existing terminal ended. Select it again from the list.");
        const text = explicitTerminalText(m.text);
        const answer = await vscode.window.showWarningMessage(`${terminal.name}: verify whether this terminal is running a shell or an AI before pasting.`, { modal: true, detail: text }, "Paste without Enter");
        if (answer === "Paste without Enter" && vscode.window.terminals.includes(terminal)) { terminal.show(); terminal.sendText(text, false); } break;
      }
      case "stopAll": await this.stopAll(); break;
    }
  }
  getTreeItem(item: vscode.TreeItem): vscode.TreeItem { return item; }
  getChildren(): vscode.TreeItem[] {
    const open = new vscode.TreeItem("Open terminal board"); open.iconPath = new vscode.ThemeIcon("layout"); open.command = { command: "patchport.openBoard", title: "Open board" };
    return [open, ...this.router.board.nodes.map(n => { const item = new vscode.TreeItem(n.name); item.description = this.router.runtime.get(n.id)?.phase || "stopped"; item.iconPath = new vscode.ThemeIcon("terminal"); item.command = { command: "patchport.openBoard", title: "Open board" }; return item; })];
  }
  private html(webview: vscode.Webview): string {
    const nonce = randomBytes(18).toString("base64");
    const uri = (file: string) => webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, file));
    return `<!doctype html><html lang=\"en\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src ${webview.cspSource} data:; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}'; font-src ${webview.cspSource};\"><link rel=\"stylesheet\" href=\"${uri("dist/xterm.css")}\"><link rel=\"stylesheet\" href=\"${uri("media/board.css")}\"></head><body><div id=\"app\"></div><script nonce=\"${nonce}\" src=\"${uri("dist/webview.js")}\"></script></body></html>`;
  }
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true; if (this.saveTimer) clearTimeout(this.saveTimer); if (this.stateTimer) clearTimeout(this.stateTimer); if (this.outputTimer) clearTimeout(this.outputTimer);
    this.saveState();
    this.host.dispose(); void this.ledger.end().catch(() => {}).finally(() => this.core.dispose()); this.panel?.dispose(); this.treeEmitter.dispose(); this.subscriptions.forEach(s => s.dispose());
  }
}

export function activate(context: vscode.ExtensionContext): { version: string; openBoard(): void; getState(): unknown; refreshProject(project: string): Promise<void>; workflow(project: string, action: string, args?: Record<string, unknown>): Promise<any>; applyWorkflow(): Promise<any>; cancelWorkflow(): Promise<void> } {
  controller = new Controller(context);
  context.subscriptions.push(controller, vscode.window.registerTreeDataProvider("patchport.sessions", controller));
  for (const [name, action] of Object.entries({ openBoard: () => controller!.open(), addTerminal: () => controller!.showPage("add"), manageProfiles: () => controller!.showPage("profiles"), connectExisting: () => controller!.showPage("external"), stopAll: () => controller!.stopAll() })) context.subscriptions.push(vscode.commands.registerCommand(`patchport.${name}`, action));
  return { version: context.extension.packageJSON.version, openBoard: () => controller!.open(), getState: () => controller!.getState(), refreshProject: project => controller!.refreshProject(project), workflow: (project, action, args) => controller!.workflow(project, action, args), applyWorkflow: () => controller!.applyWorkflow(false), cancelWorkflow: () => controller!.cancelWorkflow() };
}
export function deactivate(): void { controller?.dispose(); controller = undefined; }
