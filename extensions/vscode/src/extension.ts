import * as vscode from "vscode";
import * as path from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { Board, NodeConfig, Profile, Router, bounded, defaultBoard, restoreBoard, validateProfile, explicitTerminalText, projectKey } from "./model";
import { SessionHost } from "./session";
import { CoreClient, Ledger, endpointProfiles } from "./core";
import { affectedPaths, cleanupRoots, dirtyConflicts, resumeProfile, runtimeReport, workflowActions } from "./workflow";
import { Configs, Snapshot, StateStore } from "./stateStore";
import { changedProject, DataAction, DataPlan, dataActions, planProject, validateDataPlan } from "./projectData";

import { defaultProfiles, defaultPython, defaultShell, supportedLocal } from './platform';

let controller: Controller | undefined;

class Controller implements vscode.TreeDataProvider<vscode.TreeItem>, vscode.Disposable {
  private panel?: vscode.WebviewPanel;
  private treeEmitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.treeEmitter.event;
  readonly router: Router;
  readonly host: SessionHost;
  readonly core: CoreClient;
  readonly ledger: Ledger;
  readonly stateStore: StateStore;
  private savedConfigs: Configs = {};
  private mirrorChain: Promise<void> = Promise.resolve();
  private dataView: any = { state: "idle" };
  private dataPlan?: DataPlan;
  private dataBusy = false;
  private historyView: any = { state: "idle" };
  private historyBusy = false;
  private recordsView: any = { state: "idle" };
  private recordsRevision = 0;
  private recordsReads = 0;
  private bodyView: any = { state: "idle" };
  private bodyBusy = false;
  private bodyRevision = 0;
  private bodyPlan?: { id: string; project: string; config: string; database: string; report: any; expires: number };
  private bodyReceipt?: { project: string; database: string; operation: string };
  private connectionView: any = { state: "idle" };
  private connectionBusy = false;
  private connectionEpoch = 0;
  private connectionPlan?: { id: string; project: string; database: string; report: any; expires: number };
  private connectionReceipt?: { project: string; database: string; operation: string };
  private cleanupView: any = { state: "idle" };
  private cleanupBusy = false;
  private cleanupEpoch = 0;
  private cleanupPlan?: { id: string; project: string; database: string; report: any; expires: number };
  private cleanupReceipt?: { project: string; database: string; operation: string };
  private historyRevision = 0;
  private historyPlan?: { id: string; project: string; database: string; config: string; report: any; expires: number };
  private historyReceipt?: { project: string; database: string; operation: string };
  private coreReads = 0;
  private starting = new Set<string>();
  private dataEpoch = 0;
  private dataDirty = false;
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
    this.historyReceipt = context.workspaceState.get("history.receipt.v1");
    this.bodyReceipt = context.workspaceState.get("records.receipt.v1");
    this.connectionReceipt = context.workspaceState.get("connection.receipt.v1");
    this.cleanupReceipt = context.workspaceState.get("cleanup.receipt.v1");
    let snapshot: any;
    try { snapshot = this.stateStore.load(); } catch (error) { this.persistenceError = String(error); }
    this.savedConfigs = { ...(snapshot?.configs || context.workspaceState.get<Configs>("core.settings.v2") || {}) }; this.savedVersion = snapshot?.version || "";
    this.savedConfigs = Object.fromEntries(Object.entries(this.savedConfigs).filter(([, value]) => value === null || typeof value === "string" && path.isAbsolute(value)).map(([project, value]) => [projectKey(project), value]));
    if (!snapshot || snapshot.schema === 1) for (const project of this.projects()) {
      const key = projectKey(project);
      if (Object.hasOwn(this.savedConfigs, key)) continue;
      const legacy = context.workspaceState.keys().find(k => k.startsWith("core.config.") && projectKey(k.slice("core.config.".length)) === key);
      const value = legacy ? context.workspaceState.get<string>(legacy) : undefined;
      if (value && path.isAbsolute(value)) this.savedConfigs[key] = value;
    }
    const board = restoreBoard(snapshot?.board || context.workspaceState.get("board.v1"), this.projects());
    this.core = new CoreClient(vscode.workspace.getConfiguration('patchport').get<string>('corePython', '') || defaultPython(), path.join(context.extensionPath, 'core'));
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
    return this.savedConfigs[projectKey(project)] || undefined;
  }
  private detached(project: string): boolean { return this.savedConfigs[projectKey(project)] === null; }
  private requireAttached(project: string): void {
    if (this.cleanupBusy || this.cleanupPlan) throw new Error("Finish the project cleanup stage and preview first.");
    if (this.connectionBusy || this.connectionPlan) throw new Error("Finish connection-file management and its preview first.");
    if (this.persistenceError) throw new Error("Check board persistence and reopen the window: " + this.persistenceError);
    if (this.dataBusy || this.historyBusy || this.bodyBusy || this.stateStore.pending) throw new Error("Finish data and settings management before starting work.");
    if (this.detached(project)) throw new Error("Configuration is detached in this workspace. Explicitly reconnect in Data & settings.");
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
      trusted: vscode.workspace.isTrusted, supported: supportedLocal(process.platform, process.arch, vscode.env.remoteName), defaultShell: defaultShell(), platform: process.platform, remote: vscode.env.remoteName || '',
      deliveries: this.router.deliveries.slice(-100).map(d => ({ ...d, text: d.text.slice(0, 2000) })), notices: this.router.notices, routes: this.router.board.edges.map(e => this.router.routeStatus(e)),
      external: [...this.external].map(([id, t]) => ({ id, name: t.name, capability: "focus-and-explicit-input" })), version: this.context.extension.packageJSON.version, core: this.coreView,
      projectData: { ...this.dataView, pending: this.stateStore.pending, busy: this.dataBusy }, detachedProjects: this.projects().filter(p => this.detached(p)),
      conversationData: { ...this.historyView, busy: this.historyBusy, receipt: this.historyReceipt },
      recordData: this.recordsView,
      recordBody: { ...this.bodyView, busy: this.bodyBusy, receipt: this.bodyReceipt },
      connectionData: { ...this.connectionView, busy: this.connectionBusy, receipt: this.connectionReceipt },
      cleanupData: { ...this.cleanupView, busy: this.cleanupBusy, receipt: this.cleanupReceipt },
      workflow: this.workflowView, lifecycle: { ...this.runtimeView, active: this.host.sessions.size, previousVersion: this.savedVersion || this.context.workspaceState.get("runtime.version"), restartRequired: (this.savedVersion || this.context.workspaceState.get("runtime.version")) !== this.context.extension.packageJSON.version, persistence: this.persistenceError || "READY" }
    };
  }
  private post(value: unknown): void { void this.panel?.webview.postMessage(value); }
  private changed(): void {
    if (this.disposed) return;
    if (this.dataBusy || this.stateStore.pending) this.dataDirty = true;
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
    if (this.dataBusy || this.stateStore.pending) return;
    try {
      this.stateStore.save(this.router.board, this.savedConfigs, this.context.extension.packageJSON.version); this.persistenceError = "";
      void this.mirrorSnapshot(this.stateStore.snapshot()!).catch(error => { this.persistenceError = String(error); this.post({ type: "state", value: this.getState() }); });
    }
    catch (error) { this.persistenceError = String(error); this.post({ type: "state", value: this.getState() }); }
  }
  private mirrorSnapshot(snapshot: Snapshot): Promise<void> {
    const next = this.mirrorChain.then(async () => {
      this.stateStore.assertUnchanged();
      await this.context.workspaceState.update("board.v1", snapshot.board);
      await this.context.workspaceState.update("core.settings.v2", snapshot.configs);
      for (const name of this.context.workspaceState.keys().filter(k => k.startsWith("core.config."))) {
        const project = projectKey(name.slice("core.config.".length));
        if (this.projects().some(p => projectKey(p) === project)) await this.context.workspaceState.update(name, undefined);
      }
      for (const [project, config] of Object.entries(snapshot.configs)) if (config) await this.context.workspaceState.update(`core.config.${projectKey(project)}`, config);
    });
    this.mirrorChain = next.catch(() => {}); return next;
  }
  private dataProject(project: string): string {
    this.requireWorkspace();
    const selected = this.projects().find(p => projectKey(p) === projectKey(project));
    if (!selected) throw new Error("Select an open project.");
    return selected;
  }
  private publishData(): void { this.post({ type: "state", value: this.getState() }); this.treeEmitter.fire(); }
  private dataIdle(project: string, connection = false, cleanup = false): void {
    if (!cleanup && (this.cleanupBusy || this.cleanupPlan)) throw new Error("Finish the project cleanup preview first.");
    if (!connection && (this.connectionBusy || this.connectionPlan)) throw new Error("Finish the connection-file preview first.");
    if (this.workflowBusy || this.workflowPlan || this.coreReads || this.starting.size || this.ledger.isBusy(project)) throw new Error("Finish active queries, work and shutdown records; discard previews before trying again.");
    const ids = new Set(this.router.board.nodes.filter(n => projectKey(n.project) === projectKey(project)).map(n => n.id));
    if ([...ids].some(id => this.host.isActive(id)) || this.router.deliveries.some(d => (ids.has(d.source) || ids.has(d.target)) && ["queued", "sent"].includes(d.status))) throw new Error("Stop this project's terminals and pending deliveries first.");
    const config = this.selectedConfig(project) || path.join(project, "bridge.json");
    if (this.dirtyFiles().some(file => projectKey(file) === projectKey(config))) throw new Error("Save or revert unsaved configuration edits first.");
    this.stateStore.assertUnchanged();
  }
  private async dataFacts(project: string, targetConfig?: string): Promise<any> {
    const capabilities = await this.core.request("capabilities");
    if (capabilities.project_data_schema !== 1 || !capabilities.actions?.includes("project.data.inspect")) throw new Error("This core does not support data inspection. Check the core and extension versions.");
    const inventory = await this.core.request("project.data.inspect", { project, config: this.selectedConfig(project) || path.join(project, "bridge.json"), databases: await this.ledger.candidates(project), detached: this.detached(project) });
    const target = targetConfig ? await this.core.request("project.data.inspect", { project, config: targetConfig }) : undefined;
    return { inventory, target, revision: JSON.stringify([inventory.revision, target?.revision || null]) };
  }
  async inspectProjectData(project: string): Promise<any> {
    project = this.dataProject(project); if (this.dataBusy) throw new Error("Finish the configuration change before inspecting.");
    const epoch = ++this.dataEpoch; this.dataPlan = undefined;
    this.dataView = { state: "loading", project }; this.publishData(); this.coreReads++;
    try {
      const facts = await this.dataFacts(project);
      const cards = this.router.board.nodes.filter(n => projectKey(n.project) === projectKey(project));
      if (epoch === this.dataEpoch) this.dataView = { state: "ready", project, inventory: facts.inventory, board: { cards: cards.length, snapshot: this.stateStore.file }, profiles: { scope: "user_shared", count: this.profiles().length }, binding: this.ledger.binding(project)?.database };
      return facts.inventory;
    } catch (error) { if (epoch === this.dataEpoch) this.dataView = { state: "error", project, error: String(error) }; throw error; }
    finally { this.coreReads--; this.publishData(); }
  }
  async previewProjectData(project: string, action: DataAction, config?: string): Promise<DataPlan> {
    project = this.dataProject(project);
    if (this.historyBusy || this.historyPlan || this.bodyBusy || this.bodyPlan) throw new Error("Finish body management or discard its preview.");
    if (this.dataBusy || this.stateStore.pending || !dataActions.has(action)) throw new Error("Finish the active configuration change first.");
    this.dataIdle(project); this.dataBusy = true; const epoch = ++this.dataEpoch; this.dataPlan = undefined;
    this.dataView = { ...this.dataView, project, state: "loading", plan: undefined, error: undefined }; this.publishData();
    try {
      const targetConfig = action === "select_config" ? config : action === "reset_config" ? path.join(project, "bridge.json") : undefined;
      if (action === "select_config" && (!config || !path.isAbsolute(config))) throw new Error("Select a configuration file.");
      if (targetConfig && this.dirtyFiles().some(file => projectKey(file) === projectKey(targetConfig))) throw new Error("Resolve unsaved edits in the selected configuration first.");
      const facts = await this.dataFacts(project, targetConfig);
      const blockers = [...facts.inventory.blockers, ...(facts.target?.blockers || [])];
      if (blockers.length) throw new Error(`Inspect saved data and preview again: ${[...new Set(blockers)].join(", ")}`);
      if (action === "select_config" && facts.target?.config?.status !== "present") throw new Error("Select a valid existing configuration file.");
      this.dataIdle(project);
      if (epoch !== this.dataEpoch) throw new Error("The preview was cancelled.");
      const plan = planProject(this.router.board, this.savedConfigs, project, action, facts.revision, config);
      this.dataPlan = plan; this.dataView = { state: "ready", project, inventory: facts.inventory, plan };
      return structuredClone(plan);
    } catch (error) { this.dataView = { state: "error", project, error: String(error) }; throw error; }
    finally { this.dataBusy = false; this.flushDataChanges(); this.publishData(); }
  }
  cancelProjectData(): void { this.dataEpoch++; this.dataPlan = undefined; this.dataView = { ...this.dataView, plan: undefined }; this.publishData(); }
  async applyProjectData(confirm = true): Promise<any> {
    if (this.bodyBusy || this.bodyPlan) throw new Error("Finish task body management first.");
    const plan = this.dataPlan;
    if (!plan || this.dataBusy || this.stateStore.pending) throw new Error("Create a new configuration preview.");
    this.dataProject(plan.project); this.dataIdle(plan.project);
    if (confirm && await vscode.window.showWarningMessage("Apply the reviewed configuration change in this workspace?", { modal: true, detail: `Cards: ${plan.report.cards} - routes: ${plan.report.connections} removed. Configuration: ${plan.report.config ?? "Disconnect"}. Records, files, profiles and group limits are preserved.` }, "Apply") !== "Apply") return;
    if (this.dataPlan?.id !== plan.id) throw new Error("The preview changed.");
    this.dataBusy = true; this.dataPlan = undefined; ++this.dataEpoch;
    if (this.saveTimer) { clearTimeout(this.saveTimer); this.saveTimer = undefined; }
    this.dataView = { ...this.dataView, state: "loading", plan: undefined }; this.publishData();
    try {
      await this.mirrorChain;
      const targetConfig = plan.action === "select_config" ? plan.config : plan.action === "reset_config" ? path.join(plan.project, "bridge.json") : undefined;
      const facts = await this.dataFacts(plan.project, targetConfig);
      this.dataProject(plan.project); this.dataIdle(plan.project);
      if (targetConfig && this.dirtyFiles().some(file => projectKey(file) === projectKey(targetConfig))) throw new Error("The selected configuration now has unsaved edits.");
      validateDataPlan(plan, this.router.board, this.savedConfigs, facts.revision);
      const next = changedProject(this.router.board, this.savedConfigs, plan.project, plan.action, plan.config);
      const snapshot = this.stateStore.beginManagement(next.board, next.configs, this.context.extension.packageJSON.version, { id: plan.id, project: plan.project, action: plan.action, removed: next.removed });
      this.router.board = next.board; this.savedConfigs = next.configs;
      for (const id of next.removed) { this.router.runtime.delete(id); this.outputBatches.delete(id); }
      this.ledger.forget(plan.project); this.coreView = { state: "idle", project: plan.project }; this.cancelRecords();
      await this.mirrorSnapshot(snapshot); this.stateStore.finishManagement(plan.id); this.persistenceError = "";
      this.dataView = { state: "ready", project: plan.project, result: { operation: plan.id, action: plan.action, cardsRemoved: next.removed.length, recordsPreserved: true } };
      return this.dataView.result;
    } catch (error) { this.dataView = { state: "error", project: plan.project, error: String(error) }; throw error; }
    finally { this.dataBusy = false; this.flushDataChanges(); this.publishData(); }
  }
  async resumeProjectData(): Promise<void> {
    const pending = this.stateStore.pending;
    if (!pending || this.dataBusy) throw new Error("There is no configuration change to finalize.");
    this.dataProject(pending.project); this.dataIdle(pending.project); this.dataBusy = true;
    try { await this.mirrorSnapshot(this.stateStore.snapshot()!); this.stateStore.finishManagement(pending.id); this.persistenceError = ""; this.dataView = { state: "ready", project: pending.project, result: { operation: pending.id, resumed: true, recordsPreserved: true } }; }
    catch (error) { this.dataView = { state: "error", project: pending.project, error: String(error) }; throw error; }
    finally { this.dataBusy = false; this.flushDataChanges(); this.publishData(); }
  }
  private flushDataChanges(): void { if (this.dataDirty && !this.stateStore.pending) { this.dataDirty = false; this.saveState(); } }
  private async selectDataConfig(project: string): Promise<boolean> {
    project = this.dataProject(project);
    this.cancelProjectData(); const epoch = this.dataEpoch;
    const files = await vscode.window.showOpenDialog({ canSelectMany: false, filters: { "Bridge configuration": ["json"] } });
    if (!files?.[0] || epoch !== this.dataEpoch) return false;
    await this.previewProjectData(project, "select_config", files[0].fsPath); return true;
  }
  private requireWorkspace(): void {
    if (!vscode.workspace.isTrusted) throw new Error("Trust the VS Code workspace before starting terminals.");
    if (!this.projects().length) throw new Error("Open a local project folder first.");
    if (vscode.env.remoteName) throw new Error("Use local VS Code. Remote and WSL extension sessions are not yet validated.");
  }
  private async refreshCore(project: string, config?: string, days?: number, groupBy = "endpoint"): Promise<void> {
    project = this.projects().find(p => projectKey(p) === projectKey(project)) || project;
    this.requireWorkspace(); if (!this.projects().includes(project)) throw new Error("Select an open project.");
    if (this.detached(project)) { this.coreView = { state: "detached", project }; this.publishData(); return; }
    this.requireAttached(project); this.coreReads++;
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
    finally { this.coreReads--; }
  }
  private historyIdle(project: string): void {
    if (this.bodyBusy || this.bodyPlan) throw new Error("Discard the task body preview before managing conversation bodies.");
    this.dataProject(project);
    if (this.dataBusy || this.stateStore.pending || this.dataPlan || this.detached(project)) throw new Error("Finish configuration changes and attachment first.");
    this.dataIdle(project);
  }
  cancelRecords(): void { this.recordsRevision++; this.recordsView = { state: "idle" }; void this.cancelRecordBody(); this.publishData(); }
  private bodyIdle(project: string): void {
    this.dataProject(project);
    if (this.dataBusy || this.dataPlan || this.stateStore.pending || this.detached(project) || this.historyBusy || this.historyPlan) throw new Error("Finish other configuration/body work and previews first.");
    this.dataIdle(project);
  }
  private connectionIdle(project: string): void {
    this.dataProject(project); this.dataIdle(project, true);
    if (this.dataBusy || this.dataPlan || this.historyBusy || this.historyPlan || this.bodyBusy || this.bodyPlan) throw new Error("Finish other data/body work first.");
  }
  private cleanupIdle(project: string): void {
    this.dataProject(project); this.dataIdle(project, false, true);
    if (this.dataBusy || this.dataPlan || this.historyBusy || this.historyPlan || this.bodyBusy || this.bodyPlan || this.connectionBusy || this.connectionPlan) throw new Error("Finish other data/body/connection work first.");
  }
  async cancelCleanup(): Promise<void> {
    this.cleanupEpoch++; const plan = this.cleanupPlan; this.cleanupPlan = undefined;
    if (plan) await this.core.request("cleanup.cancel", { plan_id: plan.id }).catch(() => {});
    this.cleanupView = { ...this.cleanupView, plan: false, preview: undefined }; this.publishData();
  }
  async cleanupInventory(project: string): Promise<any> {
    project = this.dataProject(project); this.cleanupIdle(project);
    if (this.cleanupBusy) throw new Error("Project cleanup is in progress.");
    this.cleanupBusy = true;
    try {
      await this.cancelCleanup();
      if ((await this.core.request("capabilities")).project_cleanup_schema !== 1) throw new Error("A core with guided project cleanup is required.");
      const inventory = await this.core.request("cleanup.inventory", { project, config: this.selectedConfig(project) || path.join(project, "bridge.json"), ...(this.cleanupReceipt?.project === project ? { database: this.cleanupReceipt.database } : {}), databases: await this.ledger.candidates(project), known_configs: this.projects().filter(p => !this.detached(p)).map(p => this.selectedConfig(p) || path.join(p, "bridge.json")) });
      const pending = inventory.operations?.find((item: any) => item.phase !== "DONE") || inventory.operations?.[0];
      if (pending) { this.cleanupReceipt = { project, database: inventory.database, operation: pending.operation }; await this.context.workspaceState.update("cleanup.receipt.v1", this.cleanupReceipt); }
      this.cleanupView = { state: "ready", project, inventory }; return inventory;
    } catch (error) { this.cleanupView = { state: "error", project, error: String(error) }; throw error;
    } finally { this.cleanupBusy = false; this.publishData(); }
  }
  private cleanupFiles(project: string, report: any): void {
    const next = report.next_report || {};
    if (report.next_kind === "connection") this.connectionDirty(project, next.paths || []);
    if (["tasks", "flow"].includes(report.next_kind)) this.recordBodyFiles(project, path.dirname(report.database), next);
  }
  async previewCleanup(project: string, selection?: any): Promise<any> {
    project = this.dataProject(project); this.cleanupIdle(project);
    if (this.cleanupBusy || this.stateStore.pending) throw new Error("Finish active work and configuration persistence first.");
    this.cleanupBusy = true; this.publishData();
    try {
      await this.cancelCleanup(); const epoch = this.cleanupEpoch;
      if (!selection && this.cleanupReceipt?.project !== project) throw new Error("Query this project's cleanup result first.");
      const args = selection ? { project, config: this.selectedConfig(project) || path.join(project, "bridge.json"), selection, databases: await this.ledger.candidates(project), known_configs: this.projects().filter(p => !this.detached(p)).map(p => this.selectedConfig(p) || path.join(p, "bridge.json")) } : this.cleanupReceipt!;
      const preview = await this.core.request(selection ? "cleanup.preview" : "cleanup.next.preview", args);
      if (epoch !== this.cleanupEpoch) { if (preview.plan_id) await this.core.request("cleanup.cancel", { plan_id: preview.plan_id }); throw new Error("Selection changed. Create a new preview."); }
      if (preview.plan_id) this.cleanupPlan = { id: preview.plan_id, project, database: preview.report.database, report: preview.report, expires: Date.now() + preview.expires_in * 1000 };
      if (preview.plan_id) this.cleanupFiles(project, preview.report);
      this.cleanupIdle(project); this.cleanupView = { state: "ready", project, inventory: this.cleanupView.inventory, preview: preview.report, plan: Boolean(this.cleanupPlan) }; return preview;
    } catch (error) { await this.cancelCleanup(); this.cleanupView = { state: "error", project, error: String(error) }; throw error;
    } finally { this.cleanupBusy = false; this.publishData(); }
  }
  private cleanupEffects(project: string, result: any): void {
    for (const effect of result.history_effects || []) this.clearHistoryBodies(project, { kind: "purge", body_deleted: true, ...effect });
    this.clearRecordCaches(project, []);
  }
  async applyCleanup(confirm = true): Promise<any> {
    const plan = this.cleanupPlan;
    if (!plan || this.cleanupBusy || Date.now() >= plan.expires) throw new Error("Preview the project cleanup stage again.");
    this.cleanupIdle(plan.project); this.cleanupFiles(plan.project, plan.report);
    if (confirm && await vscode.window.showWarningMessage("Apply this one reviewed cleanup stage?", { modal: true, detail: "Only selected scope is processed. Review the next stage separately. Ledgers, IDs, usage, originals and backups are preserved; settings are removed last." }, " hops Apply") !== " hops Apply") return;
    if (this.cleanupPlan?.id !== plan.id) throw new Error("The preview changed.");
    this.cleanupIdle(plan.project); this.cleanupFiles(plan.project, plan.report);
    this.cleanupBusy = true; this.cleanupPlan = undefined; this.cleanupEpoch++;
    this.cleanupView = { state: "loading", project: plan.project }; this.publishData();
    try {
      this.cleanupReceipt = { project: plan.project, database: plan.database, operation: plan.report.operation || plan.id };
      await this.context.workspaceState.update("cleanup.receipt.v1", this.cleanupReceipt);
      if (plan.report.next_kind === "connection") await this.connectionWorkspace(plan.project, { reconnected: false });
      this.clearRecordCaches(plan.project, []);
      const result = await this.core.request("cleanup.apply", { plan_id: plan.id, apply: true });
      this.cleanupEffects(plan.project, result); this.cleanupView = { state: "ready", project: plan.project, result }; return result;
    } catch (error) {
      const result = (error as { result?: any }).result;
      if (result) this.cleanupEffects(plan.project, result);
      this.cleanupView = { state: "error", project: plan.project, result, error: `${String(error)} - query the cleanup result. No automatic reapplication.` };
      await this.core.request("cleanup.cancel", { plan_id: plan.id }).catch(() => {}); throw error;
    } finally { this.cleanupBusy = false; this.flushDataChanges(); this.publishData(); }
  }
  async cleanupOperation(): Promise<any> {
    const receipt = this.cleanupReceipt;
    if (!receipt || this.cleanupBusy) throw new Error("Discover the cleanup receipt first.");
    this.cleanupIdle(receipt.project); this.cleanupBusy = true;
    try {
      const result = await this.core.request("cleanup.operation", receipt); this.cleanupEffects(receipt.project, result);
      if (result.connection_removed === true) await this.connectionWorkspace(receipt.project, { reconnected: false });
      this.cleanupView = { state: "ready", project: receipt.project, result }; return result;
    } finally { this.cleanupBusy = false; this.publishData(); }
  }
  async cancelConnection(): Promise<void> {
    this.connectionEpoch++; const plan = this.connectionPlan; this.connectionPlan = undefined;
    if (plan) await this.core.request("connection.cancel", { plan_id: plan.id }).catch(() => {});
    this.connectionView = { ...this.connectionView, plan: false, preview: undefined }; this.publishData();
  }
  async connectionInventory(project: string): Promise<any> {
    project = this.dataProject(project); this.connectionIdle(project);
    if (this.connectionBusy) throw new Error("Connection-file work is in progress.");
    this.connectionBusy = true;
    try {
      await this.cancelConnection();
      const capability = await this.core.request("capabilities");
      if (capability.connection_remove_schema !== 1) throw new Error("A core with connection-file management is required.");
      const inventory = await this.core.request("connection.inventory", { project, config: this.selectedConfig(project) || path.join(project, "bridge.json"), databases: await this.ledger.candidates(project), known_configs: this.projects().filter(p => !this.detached(p)).map(p => this.selectedConfig(p) || path.join(p, "bridge.json")) });
      if (inventory.receipt) { this.connectionReceipt = { ...inventory.receipt, project }; await this.context.workspaceState.update("connection.receipt.v1", this.connectionReceipt); }
      this.connectionView = { state: "ready", project, inventory }; return inventory;
    } catch (error) { this.connectionView = { state: "error", project, error: String(error) }; throw error; }
    finally { this.connectionBusy = false; this.publishData(); }
  }
  private connectionDirty(project: string, paths: string[]): void {
    if (dirtyConflicts([project], paths, this.dirtyFiles()).length) throw new Error("Files to remove have unsaved edits. Save or revert them first.");
  }
  async previewConnection(project: string, action: string, paths: string[] = []): Promise<any> {
    project = this.dataProject(project); this.connectionIdle(project);
    if (this.connectionBusy || this.stateStore.pending || !["remove", "resume", "reconnect"].includes(action)) throw new Error("Finish configuration persistence and active work first.");
    this.connectionBusy = true; this.publishData();
    try {
      await this.cancelConnection(); const epoch = this.connectionEpoch;
      if (action !== "remove" && this.connectionReceipt?.project !== project) throw new Error("Inspect this project's connection files and receipt first.");
      this.connectionDirty(project, paths);
      const args = action === "remove" ? { project, config: this.selectedConfig(project) || path.join(project, "bridge.json"), paths, databases: await this.ledger.candidates(project), known_configs: this.projects().filter(p => !this.detached(p)).map(p => this.selectedConfig(p) || path.join(p, "bridge.json")) } : this.connectionReceipt!;
      const preview = await this.core.request(`connection.${action}.preview`, args);
      if (epoch !== this.connectionEpoch) { if (preview.plan_id) await this.core.request("connection.cancel", { plan_id: preview.plan_id }); throw new Error("Selection changed. Create a new preview."); }
      if (preview.plan_id) this.connectionPlan = { id: preview.plan_id, project, database: preview.report.database, report: preview.report, expires: Date.now() + preview.expires_in * 1000 };
      this.connectionDirty(project, preview.report.paths || []); this.connectionIdle(project);
      this.connectionView = { state: "ready", project, inventory: this.connectionView.inventory, preview: preview.report, plan: Boolean(this.connectionPlan) }; return preview;
    } catch (error) { await this.cancelConnection(); this.connectionView = { state: "error", project, error: String(error) }; throw error; }
    finally { this.connectionBusy = false; this.publishData(); }
  }
  private async connectionWorkspace(project: string, result: any): Promise<void> {
    const action = result.reconnected ? "select_config" : "disconnect";
    if (!result.reconnected && this.detached(project) && !this.stateStore.pending) return;
    if (this.stateStore.pending) throw new Error("Finalize configuration persistence, then query the connection result again.");
    if (this.saveTimer) { clearTimeout(this.saveTimer); this.saveTimer = undefined; }
    await this.mirrorChain;
    const next = changedProject(this.router.board, this.savedConfigs, project, action, result.config);
    const id = randomUUID();
    const snapshot = this.stateStore.beginManagement(next.board, next.configs, this.context.extension.packageJSON.version, { id, project, action, removed: next.removed });
    this.router.board = next.board; this.savedConfigs = next.configs; this.dataEpoch++; this.dataView = { state: "idle", project }; this.ledger.forget(project); this.clearRecordCaches(project, []);
    await this.mirrorSnapshot(snapshot); this.stateStore.finishManagement(id); this.persistenceError = "";
  }
  async applyConnection(confirm = true): Promise<any> {
    const plan = this.connectionPlan;
    if (!plan || this.connectionBusy || Date.now() >= plan.expires) throw new Error("Create a new connection-file preview.");
    this.connectionIdle(plan.project); this.connectionDirty(plan.project, plan.report.paths || []);
    const reconnect = plan.report.kind === "reconnect";
    if (confirm && await vscode.window.showWarningMessage(reconnect ? "Release connection execution protection with this configuration?" : "Remove the selected files and detach the connection?", { modal: true, detail: reconnect ? "Deleted files are not restored; tasks and AI are not started automatically." : "Only selected files are removed, with settings last. Ledgers, IDs, usage, backups and other projects are preserved. Older writers are blocked; interruption requires explicit resume." }, "Apply") !== "Apply") return;
    if (this.connectionPlan?.id !== plan.id) throw new Error("The preview changed.");
    this.connectionIdle(plan.project); this.connectionDirty(plan.project, plan.report.paths || []);
    this.connectionBusy = true; this.connectionPlan = undefined; this.connectionEpoch++;
    this.connectionView = { state: "loading", project: plan.project }; this.publishData();
    try {
      this.connectionReceipt = { project: plan.project, database: plan.database, operation: plan.report.operation || plan.id };
      await this.context.workspaceState.update("connection.receipt.v1", this.connectionReceipt);
      if (!reconnect) await this.connectionWorkspace(plan.project, { reconnected: false });
      const result = await this.core.request("connection.apply", { plan_id: plan.id, apply: true });
      this.connectionView = { state: "ready", project: plan.project, result };
      if (reconnect) await this.connectionWorkspace(plan.project, result);
      return result;
    } catch (error) {
      const result = (error as { result?: any }).result || this.connectionView.result;
      this.connectionView = { state: "error", project: plan.project, result, error: `${String(error)} - query the connection result. No automatic retry.` };
      await this.core.request("connection.cancel", { plan_id: plan.id }).catch(() => {}); throw error;
    } finally { this.connectionBusy = false; this.flushDataChanges(); this.publishData(); }
  }
  async connectionOperation(): Promise<any> {
    const receipt = this.connectionReceipt;
    if (!receipt || this.connectionBusy) throw new Error("There is no connection-removal receipt to query.");
    this.connectionIdle(receipt.project); this.connectionBusy = true;
    try {
      const result = await this.core.request("connection.operation", receipt);
      this.connectionView = { state: "ready", project: receipt.project, result };
      await this.connectionWorkspace(receipt.project, result); return result;
    } finally { this.connectionBusy = false; this.publishData(); }
  }
  async cancelRecordBody(): Promise<void> {
    this.bodyRevision++; const plan = this.bodyPlan; this.bodyPlan = undefined;
    if (plan) await this.core.request("records.cancel", { plan_id: plan.id }).catch(() => {});
    this.bodyView = { ...this.bodyView, plan: false, preview: undefined, result: undefined }; this.publishData();
  }
  private recordBodyFiles(project: string, state: string, report: any): void {
    const roots = report.flow ? [path.join(project, ".role-flows", report.flow)] : (report.task_ids || []).map((id: string) => path.join(state, "workspaces", id));
    if (report.operation) roots.push(path.join(state, "record-bodies", report.operation));
    const dirty = this.dirtyFiles();
    if (dirtyConflicts(roots, ["."], dirty).length || (report.flow && dirty.some(file => [".json", ".txt", ".log"].some(suffix => projectKey(file) === projectKey(path.join(state, "p1-jobs", report.flow + suffix)))))) throw new Error("Bodies/copies to delete have unsaved edits. Save or revert them first.");
  }
  async previewRecordBody(project: string, action: string, ids: string[] = []): Promise<any> {
    project = this.dataProject(project); this.requireAttached(project); this.bodyIdle(project);
    if (!["prepare", "purge", "resume", "roles.prepare", "flow.purge"].includes(action)) throw new Error("Choose a supported task body action.");
    this.bodyBusy = true; this.publishData();
    try {
      await this.cancelRecordBody(); const revision = this.bodyRevision;
      const capability = await this.core.request("capabilities");
      if (capability.records_body_schema !== 1) throw new Error("A core with task body management is required.");
      if (["roles.prepare", "flow.purge", "resume"].includes(action) && capability.records_flow_schema !== 1) throw new Error("A core with role body management is required.");
      if (action === "flow.purge" && (ids.length !== 1 || !ids[0])) throw new Error("Explicitly select one flow.");
      const binding = await this.ledger.inspect(project, this.selectedConfig(project));
      if (!binding.linked || !binding.state) throw new Error("Attach an existing bridge.json first.");
      const known = this.projects().filter(p => !this.detached(p)).map(p => this.selectedConfig(p) || path.join(p, "bridge.json"));
      if (known.length > 16) throw new Error("Reduce scope to at most 16 known configurations.");
      if (action === "resume" && this.bodyReceipt?.project !== project) throw new Error("Discover this project's interrupted operation first.");
      const result = await this.core.request(`records.${action}.preview`, { project, config: binding.config, known_configs: known, ...(action === "flow.purge" ? { flow_id: ids[0] } : action === "purge" ? { task_ids: ids } : action === "resume" ? { operation: this.bodyReceipt!.operation } : {}) });
      if (revision !== this.bodyRevision) { if (result.plan_id) await this.core.request("records.cancel", { plan_id: result.plan_id }); throw new Error("Selection changed. Create a new preview."); }
      if (result.plan_id) this.bodyPlan = { id: result.plan_id, project, config: binding.config, database: path.join(binding.state, "queue.sqlite3"), report: result.report, expires: Date.now() + result.expires_in * 1000 };
      this.recordBodyFiles(project, binding.state, result.report);
      this.bodyIdle(project);
      this.bodyView = { state: "ready", project, plan: Boolean(this.bodyPlan), preview: result.report }; return result;
    } catch (error) { await this.cancelRecordBody(); this.bodyView = { state: "error", project, error: String(error) }; throw error; }
    finally { this.bodyBusy = false; this.publishData(); }
  }
  private clearRecordCaches(project: string, ids: string[]): void {
    if (projectKey(this.coreView.project || "") === projectKey(project)) this.coreView = { state: "idle", project };
    if (projectKey(this.workflowView.project || "") === projectKey(project)) this.workflowView = { state: "idle", project };
    this.recordsRevision++; this.recordsView = { state: "ready", project, kind: "task" };
    this.post({ type: "recordsPurged", project, taskIds: ids });
  }
  async applyRecordBody(confirm = true): Promise<any> {
    const plan = this.bodyPlan;
    if (!plan || this.bodyBusy || Date.now() >= plan.expires) throw new Error("Create a new task body preview.");
    this.bodyIdle(plan.project);
    this.recordBodyFiles(plan.project, path.dirname(plan.database), plan.report);
    const detail = plan.report.kind === "roles_prepare" ? "Enable role protection. Older cores cannot use this ledger. Only new shared flows become manageable; existing IDs, usage and limits remain." : plan.report.kind === "prepare" ? "Prepare ordinary-task protection. Existing records/copies stay read-only and are not adopted for deletion. Older writers and role starts are blocked; execution logs retain progress rather than bodies." : `${plan.report.flow ? `flow ${plan.report.flow} in full - ` : ""}Tasks: ${plan.report.tasks}${plan.report.files === undefined ? "" : ` - files: ${plan.report.files}`}. Bodies/copies${plan.report.flow ? "/archives/receipt bodies" : ""} cannot be recovered. Usage, limits, IDs and originals remain. Resume only remaining I/O after interruption.`;
    const question = plan.report.kind === "roles_prepare" ? "Prepare role ledger protection?" : plan.report.kind === "prepare" ? "Prepare ordinary-task ledger protection?" : ["resume", "flow_resume"].includes(plan.report.kind) ? "Continue interrupted body deletion?" : plan.report.flow ? "Delete the selected flow's complete bodies?" : "Delete selected ordinary-task bodies and copies?";
    if (confirm && await vscode.window.showWarningMessage(question, { modal: true, detail }, "Apply") !== "Apply") return;
    if (this.bodyPlan?.id !== plan.id) throw new Error("The body preview changed.");
    this.recordBodyFiles(plan.project, path.dirname(plan.database), plan.report);
    this.bodyIdle(plan.project); this.bodyBusy = true; this.bodyPlan = undefined; this.bodyRevision++;
    this.bodyView = { state: "loading", project: plan.project, plan: false }; this.publishData();
    try {
      this.bodyReceipt = { project: plan.project, database: plan.database, operation: plan.report.operation || plan.id };
      await this.context.workspaceState.update("records.receipt.v1", this.bodyReceipt);
      if (!["prepare", "roles_prepare"].includes(plan.report.kind)) this.clearRecordCaches(plan.project, plan.report.task_ids || []);
      const result = await this.core.request("records.apply", { plan_id: plan.id, apply: true });
      this.bodyView = { state: "ready", project: plan.project, result }; return result;
    } catch (error) {
      const result = (error as { result?: any }).result;
      await this.core.request("records.cancel", { plan_id: plan.id }).catch(() => {});
      this.bodyView = { state: "error", project: plan.project, result, error: `${String(error)} - query the recorded result. No automatic retry.` }; throw error;
    } finally { this.bodyBusy = false; this.publishData(); }
  }
  async recordBodyOperation(): Promise<any> {
    const receipt = this.bodyReceipt;
    if (!receipt || this.bodyBusy) throw new Error("There is no task body operation to query.");
    this.dataProject(receipt.project); this.bodyBusy = true; this.publishData();
    try {
      const result = await this.core.request("records.operation", receipt);
      if (result.found && ["purge", "flow_purge"].includes(result.kind)) this.clearRecordCaches(receipt.project, result.task_ids || []);
      this.bodyView = { state: "ready", project: receipt.project, result }; return result;
    } catch (error) { this.bodyView = { state: "error", project: receipt.project, error: String(error) }; throw error;
    } finally { this.bodyBusy = false; this.publishData(); }
  }
  async discoverRecordBody(project: string): Promise<any> {
    project = this.dataProject(project); this.requireAttached(project); this.bodyBusy = true; this.publishData();
    try {
      const binding = await this.ledger.inspect(project, this.selectedConfig(project));
      if (!binding.linked || !binding.state) throw new Error("Attach the original configuration before discovering interrupted work.");
      const listed = await this.core.request("records.inventory", { project, config: binding.config, kind: "task", limit: 1 });
      if (listed.pending_body_operations?.length !== 1) throw new Error("The current ledger does not identify one interrupted operation.");
      this.bodyReceipt = { project, database: path.join(binding.state, "queue.sqlite3"), operation: listed.pending_body_operations[0].operation };
      await this.context.workspaceState.update("records.receipt.v1", this.bodyReceipt);
      const result = await this.core.request("records.operation", this.bodyReceipt);
      this.clearRecordCaches(project, result.task_ids || []); this.bodyView = { state: "ready", project, result }; return result;
    } catch (error) { this.bodyView = { state: "error", project, error: String(error) }; throw error;
    } finally { this.bodyBusy = false; this.publishData(); }
  }
  async records(project: string, kind = "task", recordId?: string, cursor = ""): Promise<any> {
    project = this.dataProject(project); this.requireAttached(project);
    if (this.workflowBusy) throw new Error("Finish active work before inspecting references.");
    if (!["task", "flow"].includes(kind)) throw new Error("Select a task or flow.");
    const revision = ++this.recordsRevision;
    const previous = this.recordsView.project === project && this.recordsView.kind === kind ? this.recordsView.list : undefined;
    this.recordsView = { state: "loading", project, kind, list: recordId ? previous : undefined }; this.publishData(); this.coreReads++; this.recordsReads++;
    try {
      const capabilities = await this.core.request("capabilities");
      if (capabilities.records_schema !== 1) throw new Error("A core with reference inspection is required.");
      const known = this.projects().filter(p => !this.detached(p)).map(p => this.selectedConfig(p) || path.join(p, "bridge.json"));
      if (known.length > 16) throw new Error("More than 16 open configurations. Reduce the workspace inspection scope.");
      const result = await this.core.request(recordId ? "records.inspect" : "records.inventory", { project, config: this.selectedConfig(project) || path.join(project, "bridge.json"), kind, cursor, limit: 25, known_configs: known, ...(recordId ? { record_id: recordId } : {}) });
      if (revision !== this.recordsRevision) return result;
      this.recordsView = { state: "ready", project, kind, list: recordId ? previous : result, detail: recordId ? result : undefined, recordId };
      return result;
    } catch (error) { if (revision === this.recordsRevision) this.recordsView = { state: "error", project, kind, error: String(error) }; throw error; }
    finally { this.coreReads--; this.recordsReads--; this.publishData(); }
  }
  async cancelHistory(): Promise<void> {
    this.historyRevision++;
    const plan = this.historyPlan; this.historyPlan = undefined;
    if (plan) await this.core.request("history.cancel", { plan_id: plan.id }).catch(() => {});
    this.historyView = { ...this.historyView, plan: false, preview: undefined }; this.publishData();
  }
  async history(project: string, action = "list", sessionIds: string[] = [], cursor = ""): Promise<any> {
    project = this.dataProject(project); this.requireAttached(project);
    if (!["list", "prepare", "purge"].includes(action) || this.historyBusy) throw new Error("Choose a supported body management action.");
    if (action !== "list") this.historyIdle(project);
    this.historyBusy = true;
    if (action === "list") this.historyView = { state: "loading", project };
    this.publishData();
    try {
      await this.cancelHistory(); const revision = this.historyRevision;
      const binding = await this.ledger.inspect(project, this.selectedConfig(project));
      const capabilities = await this.core.request("capabilities");
      if (capabilities.history_schema !== 1 || capabilities.interactive_writer_protocol !== 2) throw new Error("A core with conversation body management is required.");
      const args = { database: binding.database, project };
      const result = await this.core.request(action === "list" ? "history.list" : `history.${action}.preview`, { ...args, ...(action === "list" ? { cursor, limit: 25 } : action === "purge" ? { session_ids: sessionIds } : {}) });
      if (revision !== this.historyRevision) { if (result.plan_id) await this.core.request("history.cancel", { plan_id: result.plan_id }); throw new Error("Selection changed. Inspect again."); }
      if (result.plan_id) this.historyPlan = { id: result.plan_id, project, database: binding.database, config: binding.config, report: result.report, expires: Date.now() + result.expires_in * 1000 };
      if (action !== "list") this.historyIdle(project);
      this.historyView = { ...(this.historyView.project === project ? this.historyView : {}), state: "ready", project, database: binding.database, error: undefined, plan: Boolean(this.historyPlan), expires: this.historyPlan?.expires, ...(action === "list" ? { list: result, preview: undefined, result: undefined } : { preview: result.report, result: undefined }) };
      return result;
    } catch (error) { await this.cancelHistory(); this.historyView = { ...this.historyView, project, state: "error", error: String(error) }; throw error; }
    finally { this.historyBusy = false; this.publishData(); }
  }
  private clearHistoryBodies(project: string, result: any): void {
    if (result.kind !== "purge" || !result.body_deleted) return;
    const nodes = new Set<string>((result.node_ids || []).filter((id: string) => {
      const node = this.router.board.nodes.find(n => n.id === id);
      return (!node || projectKey(node.project) === projectKey(project)) && !this.host.isActive(id) && !this.starting.has(id);
    })), deliveries = new Set<string>(result.delivery_ids || []);
    for (const id of nodes) { const runtime = this.router.runtime.get(id); if (runtime) { runtime.lastMessage = ""; runtime.reason = ""; } this.outputBatches.delete(id); }
    for (let index = this.router.deliveries.length - 1; index >= 0; index--) if (deliveries.has(this.router.deliveries[index].id)) this.router.deliveries.splice(index, 1);
    for (const id of deliveries) this.deliverySnapshots.delete(id);
    if (projectKey(this.coreView.project || "") === projectKey(project)) this.coreView = { state: "idle", project };
    this.historyView = { state: "ready", project, result };
    this.post({ type: "historyPurged", nodes: [...nodes], deliveries: [...deliveries], project });
  }
  async applyHistory(confirm = true): Promise<any> {
    const plan = this.historyPlan;
    if (!plan || this.historyBusy || Date.now() >= plan.expires) throw new Error("Create a new body management preview.");
    this.historyIdle(plan.project);
    const detail = plan.report.kind === "prepare" ? "Stop all older managed terminals. Existing records remain and are not adopted for deletion. Older extensions cannot write to this ledger after preparation." : `Sessions: ${plan.report.sessions} - events: ${plan.report.events} files - Delivery ${plan.report.deliveries}. Bodies and provider resume information are removed. Usage, delivery counts, IDs and provider originals remain. No body backup is created.`;
    if (confirm && await vscode.window.showWarningMessage(plan.report.kind === "prepare" ? "Prepare conversation ledger protection?" : "Delete selected conversation bodies?", { modal: true, detail }, "Apply") !== "Apply") return;
    if (this.historyPlan?.id !== plan.id) throw new Error("The preview changed.");
    this.historyIdle(plan.project); this.historyBusy = true; this.historyPlan = undefined; this.historyRevision++;
    this.historyView = { ...this.historyView, state: "loading", plan: false }; this.publishData();
    try {
      const binding = await this.ledger.inspect(plan.project, this.selectedConfig(plan.project));
      if (projectKey(binding.database) !== projectKey(plan.database) || projectKey(binding.config) !== projectKey(plan.config)) throw new Error("Ledger binding changed. Create a new preview.");
      this.historyIdle(plan.project);
      this.historyReceipt = { project: plan.project, database: plan.database, operation: plan.id };
      await this.context.workspaceState.update("history.receipt.v1", this.historyReceipt);
      const result = await this.core.request("history.apply", { plan_id: plan.id, apply: true });
      this.clearHistoryBodies(plan.project, result);
      this.historyView = { state: "ready", project: plan.project, result };
      return result;
    } catch (error) {
      await this.core.request("history.cancel", { plan_id: plan.id }).catch(() => {});
      this.historyView = { state: "error", project: plan.project, error: `${String(error)} - query the completion receipt. No automatic retry.` }; throw error;
    } finally { this.historyBusy = false; this.publishData(); }
  }
  async historyOperation(): Promise<any> {
    const receipt = this.historyReceipt;
    if (!receipt || this.historyBusy) throw new Error("There is no body management operation to query.");
    this.dataProject(receipt.project); this.historyBusy = true; this.publishData();
    try {
      const result = await this.core.request("history.operation", receipt);
      this.clearHistoryBodies(receipt.project, result);
      this.historyView = { state: "ready", project: receipt.project, result }; return result;
    } finally { this.historyBusy = false; this.publishData(); }
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
    const copies = cleanupRoots(plan.action, plan.report);
    if (copies.length && dirtyConflicts(copies, ["."], this.dirtyFiles()).length) throw new Error("Copies to clean have unsaved edits. Save/review and preview again.");
    if (copies.length && (this.ledger.isBusy(plan.project) || this.router.board.nodes.some(n => projectKey(n.project) === projectKey(plan.project) && (this.host.isActive(n.id) || this.starting.has(n.id))))) throw new Error("Finish this project's managed terminals and shutdown records first.");
    if (typeof plan.args.config === "string" && this.dirtyFiles().some(file => projectKey(file) === projectKey(plan.args.config))) throw new Error("Resolve unsaved edits in the selected configuration first.");
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
    if (this.historyPlan || this.bodyPlan) throw new Error("Discard the body management preview before continuing.");
    project = this.projects().find(p => projectKey(p) === projectKey(project)) || project;
    this.requireWorkspace();
    this.requireAttached(project);
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
      if ((action.startsWith("copies.") || action === "roles.prune.preview") && capabilities.copy_cleanup_schema !== 1) throw new Error("This core does not support copy cleanup. Check core/extension versions.");
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
    if (this.recordsReads) throw new Error("Finish reference inspection before applying.");
    this.requireWorkspace(); const plan = this.workflowPlan;
    if (!plan || this.workflowBusy || Date.now() >= plan.expires) throw new Error("Make a new, valid preview.");
    this.requireAttached(plan.project);
    this.checkDirty(plan);
    if (confirm && await vscode.window.showWarningMessage(plan.report.operation === "copy_cleanup" ? "Delete selected task copies and logs. DB answers, usage, lifetime counts and originals remain. Copy deletion cannot be undone." : plan.action === "roles.preview" ? "Start the role workflow? It runs at most two provider tasks and the configured test commands." : "Apply the reviewed preview?", { modal: true, detail: JSON.stringify(plan.report, null, 2).slice(0, 4000) }, "Apply") !== "Apply") return;
    if (this.workflowPlan?.id !== plan.id || !this.projects().includes(plan.project)) throw new Error("The preview changed. Review a new plan.");
    this.requireAttached(plan.project);
    if (this.recordsReads) throw new Error("Finish reference inspection before applying.");
    this.checkDirty(plan); this.workflowBusy = true; this.workflowPlan = undefined;
    this.workflowView = { ...this.workflowView, state: "loading", plan: false }; this.changed();
    try {
      const result = await this.core.request("operation.apply", { plan_id: plan.id, apply: true });
      // A refresh failure must not discard an already confirmed mutation result.
      await this.refreshCore(plan.project).catch(error => { this.coreView = { state: "error", project: plan.project, error: String(error) }; });
      if (result.operation === "copy_cleanup") await this.inspectProjectData(plan.project).catch(() => {});
      this.workflowView = { state: "ready", project: plan.project, action: "operation.apply", result, plan: false };
      this.changed(); return result;
    } catch (error) {
      const result = (error as { result?: any }).result;
      if (result?.operation === "copy_cleanup") await this.inspectProjectData(plan.project).catch(() => {});
      this.workflowView = { state: "error", project: plan.project, action: plan.action, plan: false, result, error: `${String(error)} - inspect results, backups and archives first.` }; this.changed(); throw error;
    }
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
    this.requireAttached(project);
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
    if (this.historyPlan || this.bodyPlan) throw new Error("Discard the body management preview before starting a session.");
    this.requireAttached(node.project); this.starting.add(node.id);
    try {
      await this.checkRuntime();
      if (this.runtimeView.state === "error") throw new Error(this.runtimeView.error);
      await this.refreshCore(node.project);
      if (this.coreView.state === "error") throw new Error(this.coreView.error);
      this.requireAttached(node.project); await this.host.start(node, profile);
    } finally { this.starting.delete(node.id); }
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
    if ((this.dataBusy || this.historyBusy || this.bodyBusy) && !["recordBodyCancel", "recordsCancel", "historyCancel", "dataCancel", "input", "interrupt", "stop", "stopAll", "focus", "resize", "activity"].includes(m.type)) throw new Error("Data and settings management is in progress.");
    if (this.stateStore.pending && !["dataInspect", "dataResume", "dataCancel", "workflowCancel", "runtimeRefresh", "stop", "stopAll", "interrupt"].includes(m.type)) throw new Error("Finalize the previous configuration change in Data & settings.");
    switch (m.type) {
      case "recordBodyPreview": await this.previewRecordBody(String(m.project), String(m.action), (m.taskIds || []) as string[]); break;
      case "connectionInventory": await this.connectionInventory(String(m.project)); break;
      case "cleanupInventory": await this.cleanupInventory(String(m.project)); break;
      case "cleanupPreview": await this.previewCleanup(String(m.project), m.selection); break;
      case "cleanupApply": await this.applyCleanup(); break;
      case "cleanupCancel": await this.cancelCleanup(); break;
      case "cleanupOperation": await this.cleanupOperation(); break;
      case "connectionPreview": await this.previewConnection(String(m.project), String(m.action), (m.paths || []) as string[]); break;
      case "connectionApply": await this.applyConnection(); break;
      case "connectionCancel": await this.cancelConnection(); break;
      case "connectionOperation": await this.connectionOperation(); break;
      case "recordBodyApply": await this.applyRecordBody(); break;
      case "recordBodyCancel": await this.cancelRecordBody(); break;
      case "recordBodyOperation": await this.recordBodyOperation(); break;
      case "recordBodyDiscover": await this.discoverRecordBody(String(m.project)); break;
      case "recordsList": await this.records(String(m.project), String(m.kind || "task"), undefined, String(m.cursor || "")); break;
      case "recordsInspect": await this.records(String(m.project), String(m.kind || "task"), String(m.recordId || ""), String(m.cursor || "")); break;
      case "recordsCancel": this.cancelRecords(); break;
      case "historyList": await this.history(String(m.project), "list", [], String(m.cursor || "")); break;
      case "historyPreview": await this.history(String(m.project), String(m.action), m.sessionIds as string[]); break;
      case "historyApply": await this.applyHistory(); break;
      case "historyCancel": await this.cancelHistory(); break;
      case "historyOperation": await this.historyOperation(); break;
      case "dataInspect": await this.inspectProjectData(String(m.project || this.projects()[0])); break;
      case "dataPreview": await this.previewProjectData(String(m.project || this.projects()[0]), m.action as DataAction); break;
      case "dataSelectConfig": await this.selectDataConfig(String(m.project || this.projects()[0])); break;
      case "dataApply": await this.applyProjectData(); break;
      case "dataResume": await this.resumeProjectData(); break;
      case "dataCancel": this.cancelProjectData(); break;
      case "workflow": if (typeof m.project === "string" && typeof m.action === "string" && m.args && typeof m.args === "object") await this.workflow(m.project, m.action, m.args as Record<string, unknown>); break;
      case "workflowApply": await this.applyWorkflow(); break;
      case "workflowCancel": await this.cancelWorkflow(); break;
      case "runtimeRefresh": await this.checkRuntime(); break;
      case "resumeSession": {
        if (typeof m.project !== "string" || !this.projects().includes(m.project) || typeof m.sessionId !== "string") break;
        this.requireAttached(m.project);
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
        this.requireAttached(project);
        const binding = await this.ledger.inspect(project, this.selectedConfig(project));
        if (!binding.linked || !["start", "stop"].includes(String(m.action))) break;
        if (await vscode.window.showWarningMessage(m.action === "start" ? "Start the queue runner? Pending tasks may execute." : "Stop the selected runner?", { modal: true }, "Apply") !== "Apply") break;
        this.requireAttached(project); if (this.ledger.binding(project) !== binding) throw new Error("Configuration attachment changed. Inspect again.");
        const status = await this.core.request("runner.status", { config: binding.config });
        const runId = status.management?.run_id;
        const result = await this.core.request(`runner.${m.action}`, { config: binding.config, apply: true, ...(m.action === "stop" ? { run_id: runId, cancel_active: m.cancelActive === true } : {}) });
        this.workflowView = { state: "ready", project, action: `runner.${m.action}`, result }; await this.refreshCore(project); this.changed(); break;
      }
      case "coreRefresh": await this.refreshCore(String(m.project || this.projects()[0]), undefined, typeof m.days === "number" ? m.days : undefined, typeof m.groupBy === "string" ? m.groupBy : "endpoint"); break;
      case "coreConfig": {
        const project = String(m.project || this.projects()[0]);
        if (await this.selectDataConfig(project)) await this.applyProjectData();
        if (!this.dataPlan && !this.detached(project)) await this.refreshCore(project);
        break;
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
        this.requireAttached(binding.project);
        const action = String(m.action);
        if (await vscode.window.showWarningMessage("Change existing batch queue controls? These do not limit interactive terminals or token charges.", { modal: true }, "Apply") !== "Apply") break;
        this.requireAttached(binding.project); if (this.ledger.binding(binding.project) !== binding) throw new Error("Configuration attachment changed. Inspect again.");
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
  private shutdownPromise?: Promise<void>;
  shutdown(): Promise<void> {
    if (this.shutdownPromise) return this.shutdownPromise;
    this.disposed = true; if (this.saveTimer) clearTimeout(this.saveTimer); if (this.stateTimer) clearTimeout(this.stateTimer); if (this.outputTimer) clearTimeout(this.outputTimer);
    this.saveState();
    this.panel?.dispose(); this.treeEmitter.dispose(); this.subscriptions.forEach(s => s.dispose());
    this.shutdownPromise = this.host.shutdown().then(() => this.ledger.end()).catch(() => {}).finally(() => this.core.dispose());
    return this.shutdownPromise;
  }
  dispose(): void { void this.shutdown(); }
}

export function activate(context: vscode.ExtensionContext) {
  controller = new Controller(context);
  context.subscriptions.push(controller, vscode.window.registerTreeDataProvider("patchport.sessions", controller));
  for (const [name, action] of Object.entries({ openBoard: () => controller!.open(), addTerminal: () => controller!.showPage("add"), manageProfiles: () => controller!.showPage("profiles"), connectExisting: () => controller!.showPage("external"), stopAll: () => controller!.stopAll() })) context.subscriptions.push(vscode.commands.registerCommand(`patchport.${name}`, action));
  return { version: context.extension.packageJSON.version, openBoard: () => controller!.open(), getState: () => controller!.getState(), refreshProject: (project: string) => controller!.refreshProject(project), workflow: (project: string, action: string, args?: Record<string, unknown>) => controller!.workflow(project, action, args), applyWorkflow: () => controller!.applyWorkflow(false), cancelWorkflow: () => controller!.cancelWorkflow(),
    inspectProjectData: (project: string) => controller!.inspectProjectData(project), previewProjectData: (project: string, action: DataAction, config?: string) => controller!.previewProjectData(project, action, config), applyProjectData: () => controller!.applyProjectData(false), resumeProjectData: () => controller!.resumeProjectData(), cancelProjectData: () => controller!.cancelProjectData(),
    history: (project: string, action?: string, ids?: string[], cursor?: string) => controller!.history(project, action, ids, cursor), applyHistory: () => controller!.applyHistory(false), cancelHistory: () => controller!.cancelHistory(), historyOperation: () => controller!.historyOperation(),
    records: (project: string, kind?: string, id?: string, cursor?: string) => controller!.records(project, kind, id, cursor), cancelRecords: () => controller!.cancelRecords(),
    previewRecordBody: (project: string, action: string, ids?: string[]) => controller!.previewRecordBody(project, action, ids), applyRecordBody: () => controller!.applyRecordBody(false), cancelRecordBody: () => controller!.cancelRecordBody(), recordBodyOperation: () => controller!.recordBodyOperation(), discoverRecordBody: (project: string) => controller!.discoverRecordBody(project),
    connectionInventory: (project: string) => controller!.connectionInventory(project), previewConnection: (project: string, action: string, paths?: string[]) => controller!.previewConnection(project, action, paths), applyConnection: () => controller!.applyConnection(false), cancelConnection: () => controller!.cancelConnection(), connectionOperation: () => controller!.connectionOperation(),
    cleanupInventory: (project: string) => controller!.cleanupInventory(project), previewCleanup: (project: string, selection?: any) => controller!.previewCleanup(project, selection), applyCleanup: () => controller!.applyCleanup(false), cancelCleanup: () => controller!.cancelCleanup(), cleanupOperation: () => controller!.cleanupOperation() };
}
export function deactivate(): Promise<void> | undefined { const work = controller?.shutdown(); controller = undefined; return work; }
