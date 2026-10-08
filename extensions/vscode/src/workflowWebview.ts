type Sender = (type: string, values?: Record<string, unknown>) => void;
const element = <K extends keyof HTMLElementTagNameMap>(tag: K, text = ""): HTMLElementTagNameMap[K] => { const value = document.createElement(tag); value.textContent = text; return value; };
const list = (items: { value: string; label: string }[], value = ""): HTMLSelectElement => { const select = element("select"); for (const item of items) { const option = element("option", item.label); option.value = item.value; select.append(option); } if (value) select.value = value; return select; };
export class WorkflowPanel {
  private state: any;
  private opened = false;
  private tab = "submit";
  private project = "";
  private inventory: any;
  private proposal: any;
  private roles: any;
  private roleView: any;
  private signature = "";
  private seenResult: any;
  private invalidate(): void { if (this.state?.workflow?.plan || this.state?.workflow?.state === "loading") { this.state.workflow.plan = false; this.post("workflowCancel"); } }
  private task = "";
  private flow = "";
  private role = "developer";
  private draft: any = { prompt: "", batch: "", flow: "", summaries: "", days: "30", modes: {}, summaryIds: {}, selected: [] };
  constructor(private post: Sender, private openModal: (title: string) => HTMLElement) {}
  private button(text: string, action: () => void): HTMLButtonElement { const b = element("button", text); b.type = "button"; b.onclick = action; b.disabled = this.state?.workflow?.state === "loading"; return b; }
  private input(name: string, hint: string, multiline = false): HTMLInputElement | HTMLTextAreaElement { const input = multiline ? element("textarea") : element("input"); input.value = this.draft[name] || ""; input.placeholder = hint; input.setAttribute("aria-label", hint); input.oninput = () => { this.draft[name] = input.value; this.invalidate(); }; return input; }
  private field(body: HTMLElement, label: string, input: HTMLElement): void { const wrapper = element("label"); wrapper.className = "field"; wrapper.append(element("span", label), input); body.append(wrapper); }
  private request(action: string, args: Record<string, unknown> = {}): void { this.post("workflow", { project: this.project, action, args }); }
  open(project: string): void { this.opened = true; this.project = project; this.openModal("Project work"); this.render(); this.post("coreRefresh", { project }); this.request("context.inventory"); }
  close(): void { this.opened = false; this.post("workflowCancel"); }
  update(state: any): void {
    this.state = state;
    const view = state.workflow;
    if (view?.project === this.project && view.result && view.result !== this.seenResult && JSON.stringify(view.result) !== JSON.stringify(this.seenResult)) {
      this.seenResult = view.result;
      if (view.action === "context.inventory") this.inventory = view.result;
      if (view.action === "proposal.inspect") this.proposal = view.result;
      if (view.action === "roles.overview") this.roles = view.result;
      if (view.action === "roles.inspect") this.roleView = view.result;
    }
    const signature = JSON.stringify([view, state.lifecycle, state.core?.tasks]);
    if (signature !== this.signature) { this.signature = signature; if (this.opened) this.render(); }
  }
  private render(): void {
    if (!this.opened || !this.state) return;
    const focused = document.activeElement as HTMLInputElement | HTMLTextAreaElement;
    const focusLabel = focused?.getAttribute("aria-label");
    const selection = focused && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : undefined;
    const body = document.querySelector<HTMLElement>("#modal-body")!; body.replaceChildren(); document.querySelector("#modal .modal-card")!.classList.add("core-modal");
    const nav = element("div"); nav.className = "core-controls";
    const projects = list(this.state.projects.map((p: string) => ({ value: p, label: p })), this.project);
    projects.onchange = () => { this.project = projects.value; this.inventory = this.proposal = this.roles = this.roleView = undefined; this.task = this.flow = ""; this.draft.selected = []; this.post("workflowCancel"); this.post("coreRefresh", { project: this.project }); this.request("context.inventory"); this.render(); };
    nav.append(projects);
    for (const [tab, label] of [["submit", "Submit work"], ["proposal", "Copies & recovery"], ["roles", "Roles & archives"], ["lifecycle", "Sessions & updates"]]) nav.append(this.button(label, () => { this.tab = tab; this.post("workflowCancel"); this.render(); if (tab === "roles") this.request("roles.overview"); if (tab === "lifecycle") { this.post("runtimeRefresh"); this.post("coreRefresh", { project: this.project }); } }));
    body.append(nav);
    const section = element("section"); section.className = "workflow-form"; body.append(section);
    if (this.tab === "submit") this.submit(section);
    if (this.tab === "proposal") this.proposals(section);
    if (this.tab === "roles") this.rolePanel(section);
    if (this.tab === "lifecycle") this.lifecycle(section);
    const view = this.state.workflow;
    if (view?.project === this.project) {
      if (view.state === "loading") body.append(element("p", "Processing management action..."));
      if (view.error) { const error = element("p", view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
      if (view.plan) { const controls = element("div"); controls.className = "core-controls"; controls.append(this.button("Apply reviewed preview", () => this.post("workflowApply")), this.button("Discard preview", () => this.post("workflowCancel")), element("span", `Apply once - valid until ${new Date(view.expires).toLocaleTimeString()}`)); body.append(controls); }
      if (view.result && !["context.inventory", "proposal.inspect", "roles.overview", "roles.inspect"].includes(view.action)) { const details = element("details"); details.open = true; details.append(element("summary", view.plan ? "Plan to apply" : "Action result"), element("pre", JSON.stringify(view.result.report || view.result, null, 2))); body.append(details); }
    }
    if (focusLabel) { const field = Array.from(body.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[aria-label]")).find(i => i.getAttribute("aria-label") === focusLabel); if (field) { field.focus(); if (selection && field.tagName !== "SELECT" && field.type !== "number") field.setSelectionRange(selection[0], selection[1]); } }
  }
  private submit(body: HTMLElement): void {
    body.append(element("h3", "Submit a working-copy task with selected input"), element("p", "Submission queues the task. Runner start or resume controls execution separately."));
    this.field(body, "New batch ID", this.input("batch", "Example: change-001"));
    this.field(body, "Original request", this.input("prompt", "Enter the task request", true));
    const endpoints = element("div"); endpoints.className = "core-controls";
    this.draft.targets ||= this.inventory?.endpoints?.slice(0, 1) || [];
    for (const endpoint of this.inventory?.endpoints || []) { const label = element("label"); const check = element("input"); check.type = "checkbox"; check.checked = this.draft.targets.includes(endpoint); check.onchange = () => { this.draft.targets = check.checked ? [...this.draft.targets, endpoint] : this.draft.targets.filter((v: string) => v !== endpoint); this.post("workflowCancel"); }; label.append(check, element("span", endpoint)); endpoints.append(label); } body.append(endpoints);
    body.append(this.button("Refresh input & budget", () => this.request("context.inventory", { prompt: this.draft.prompt })));
    if (!this.inventory) { body.append(element("p", "Inspect the input candidates in the connected bridge.json.")); return; }
    body.append(element("p", `Current candidates: ${this.inventory.files.length} files - ${this.inventory.summary.total_bytes} bytes - estimated ${this.inventory.summary.estimated_tokens} tokens - configured limits ${JSON.stringify(this.inventory.limits)}`));
    const required = new Set(this.inventory.required?.map((i: any) => i.path));
    const table = element("table"); table.className = "usage-table";
    for (const file of this.inventory.files) {
      const row = element("tr"), modeCell = element("td");
      const mode = list(["full", "omit", "summary", "metadata"].map(value => ({ value, label: value })), this.draft.modes[file.path] || "full");
      mode.disabled = required.has(file.path); mode.setAttribute("aria-label", `${file.path} input mode`);
      mode.onchange = () => { this.draft.modes[file.path] = mode.value; this.post("workflowCancel"); };
      const sid = element("input"); sid.value = this.draft.summaryIds[file.path] || ""; sid.placeholder = "summary ID"; sid.setAttribute("aria-label", `${file.path} summary ID`); sid.oninput = () => { this.draft.summaryIds[file.path] = sid.value; this.post("workflowCancel"); };
      modeCell.append(mode, sid); row.append(element("td", file.path + (required.has(file.path) ? " - required" : "")), element("td", String(file.bytes)), modeCell); table.append(row);
    } body.append(table);
    this.field(body, "Reviewed summary record paths (one per line or comma-separated)", this.input("summaries", "docs/summary.json", true));
    body.append(this.button("Preview input & submission", () => {
      const summaries = this.draft.summaries.split(/[\n,]/).map((v: string) => v.trim()).filter(Boolean);
      const files = this.inventory.files.map((i: any) => { const mode = required.has(i.path) ? "full" : this.draft.modes[i.path] || "full"; return { path: i.path, mode, reason: "Explicit user selection", ...(mode === "summary" ? { summary_id: this.draft.summaryIds[i.path] || "" } : {}) }; });
      this.request("batch.preview", { batch_id: this.draft.batch, prompt: this.draft.prompt, targets: this.draft.targets, plan: { schema: summaries.length ? 2 : 1, files, ...(summaries.length ? { summaries } : {}) } });
    }));
    this.runner(body);
  }
  private runner(body: HTMLElement): void { const controls = element("div"); controls.className = "core-controls"; controls.append(this.button("Start queue runner", () => this.post("runnerControl", { project: this.project, action: "start" })), this.button("Drain & stop runner", () => this.post("runnerControl", { project: this.project, action: "stop" })), this.button("Cancel active work & stop", () => this.post("runnerControl", { project: this.project, action: "stop", cancelActive: true }))); body.append(controls); }
  private proposals(body: HTMLElement): void {
    body.append(element("h3", "Review completed task changes before explicit promotion"));
    const tasks = (this.state.core?.tasks?.tasks || []).filter((t: any) => ["DONE", "ERROR", "FAILED", "INTERRUPTED"].includes(t.state));
    const select = list([{ value: "", label: "Select task" }, ...tasks.map((t: any) => ({ value: t.id, label: `${t.state} \u00b7 ${t.id}` }))], this.task);
    select.onchange = () => { this.task = select.value; this.draft.selected = []; this.proposal = undefined; if (this.task) this.request("proposal.inspect", { task_id: this.task }); else { this.post("workflowCancel"); this.render(); } }; body.append(select);
    if (!this.proposal) return;
    this.changes(body, this.proposal, path => this.request("proposal.inspect", { task_id: this.task, path }));
    body.append(this.button("Preview selected promotion", () => this.request("proposal.preview", { task_id: this.task, paths: this.draft.selected })));
    for (const backup of this.proposal.backups || []) body.append(this.button(`${backup.backup} recovery preview`, () => this.request("recovery.preview", { task_id: this.task, backup: backup.backup })));
  }
  private changes(body: HTMLElement, view: any, diff: (path: string) => void): void {
    const changes = element("div"); changes.className = "workflow-changes";
    for (const change of view.changes || []) { const line = element("label"); const check = element("input"); check.type = "checkbox"; check.checked = this.draft.selected.includes(change.path); check.disabled = !change.allowed; check.onchange = () => { this.draft.selected = check.checked ? [...this.draft.selected, change.path] : this.draft.selected.filter((v: string) => v !== change.path); this.post("workflowCancel"); }; line.append(check, element("span", `${change.path} \u00b7 ${change.allowed ? "Promotion allowed" : "Promotion forbidden"}`), this.button("diff", () => diff(change.path))); changes.append(line); } body.append(changes);
    if (view.diff) { body.append(element("h4", view.diff.path), element("p", view.diff.original_matches ? "Original matches its baseline hash" : "Original changed; promotion conflict"), element("p", view.diff.binary ? "Binary file; inspect its path and hashes." : view.diff.too_large ? "Large file; inspect the full file in the saved copy." : "Current original and proposed copy")); const columns = element("div"); columns.className = "core-columns workflow-diff"; columns.append(element("pre", view.diff.before ?? ""), element("pre", view.diff.after ?? "")); body.append(columns); }
  }
  private rolePanel(body: HTMLElement): void {
    body.append(element("h3", "Develop -> test -> independent review"), element("p", "Use the developer, reviewer and test commands in role_workflow.json. At most two provider tasks run; promotion is never automatic."));
    this.field(body, "New flow ID", this.input("flow", "Example: flow-change-001"));
    this.field(body, "Role workflow request", this.input("prompt", "Enter the development and review request", true));
    body.append(this.button("Preview role execution", () => this.request("roles.preview", { flow_id: this.draft.flow, prompt: this.draft.prompt })), this.button("Refresh roles & tests", () => this.request("roles.overview")));
    for (const launch of this.roles?.launches || []) body.append(element("p", `Launch receipt ${launch.flow} \u00b7 ${launch.state} - no automatic retry`));
    for (const flow of this.roles?.flows || []) {
      const section = element("details"); section.open = flow.id === this.flow; section.append(element("summary", `${flow.id} \u00b7 ${flow.saved_phase} \u00b7 ${flow.artifact_state || "LIVE"}`));
      for (const check of flow.checks || []) section.append(element("p", `Test ${JSON.stringify(check.argv)} \u00b7 exit ${check.exit_code}`), this.button(check.log, () => this.request("roles.log", { flow_id: flow.id, log: check.log })));
      for (const [role, record] of Object.entries<any>(flow.roles || {})) for (const task of record.tasks || []) section.append(this.button(`${role} \u00b7 ${task.state} \u00b7 ${task.id}`, () => { this.flow = flow.id; this.role = role; this.task = task.id; this.draft.selected = []; this.roleView = undefined; this.request("roles.inspect", { flow_id: this.flow, role }); }));
      const days = element("input"); days.type = "number"; days.min = "1"; days.max = "3650"; days.value = this.draft.days; days.setAttribute("aria-label", `${flow.id} retention days`); days.oninput = () => { this.draft.days = days.value; this.post("workflowCancel"); };
      section.append(days, this.button("Preview archive (no deletion)", () => this.request("archive.preview", { flow_id: flow.id, days: Number(this.draft.days) })));
      body.append(section);
    }
    this.field(body, "Interrupted archive flow ID", this.input("archiveFlow", "flow ID"));
    this.field(body, "Archive operation ID", this.input("operation", "Operation from cleanup.json"));
    body.append(this.button("Preview archive resume", () => this.request("archive.resume.preview", { flow_id: this.draft.archiveFlow, operation: this.draft.operation })));
    if (!this.roleView) return;
    body.append(element("h4", `${this.flow} \u00b7 ${this.role} \u00b7 ${this.task}`)); this.changes(body, this.roleView, path => this.request("roles.inspect", { flow_id: this.flow, role: this.role, path }));
    if (this.role === "developer") body.append(this.button("Preview selected promotion", () => this.request("roles.promote.preview", { flow_id: this.flow, role: this.role, task_id: this.task, paths: this.draft.selected })));
    for (const backup of this.roleView.backups || []) body.append(this.button(`${backup.backup} recovery preview`, () => this.request("roles.recover.preview", { flow_id: this.flow, role: this.role, task_id: this.task, backup: backup.backup })));
    body.append(this.button("Preview role-copy cleanup", () => this.request("roles.prune.preview", { flow_id: this.flow, role: this.role, task_id: this.task, days: Number(this.draft.days) })));
    this.field(body, "Archived logical evidence path", this.input("evidence", "Example: src/app.py"));
    this.field(body, "Archived evidence source", this.input("evidenceSource", "developer_workspace"));
    body.append(this.button("Read selected evidence", () => this.request("roles.inspect", { flow_id: this.flow, role: this.role, evidence_path: this.draft.evidence, evidence_source: this.draft.evidenceSource || this.role + "_workspace" })));
    if (this.roleView.evidence) body.append(element("pre", JSON.stringify(this.roleView.evidence, null, 2)));
  }
  private lifecycle(body: HTMLElement): void {
    body.append(element("h3", "Recorded history and session recovery"), element("p", "Unconfirmed deliveries are not resent. Explicitly start managed sessions from their cards. Restored groups remain paused."));
    for (const session of this.state.core?.history?.sessions || []) { body.append(element("p", `${session.name} \u00b7 ${session.state} \u00b7 ${new Date(session.started * 1000).toLocaleString()} \u00b7 ${session.id}`)); if (["claude", "codex"].includes(session.provider)) body.append(this.button("Resume this provider conversation", () => this.post("resumeSession", { project: this.project, sessionId: session.id }))); }
    for (const delivery of this.state.core?.history?.deliveries || []) body.append(element("p", `Delivery ${delivery.id} \u00b7 ${delivery.status} \u00b7 ${(delivery.text || "").slice(0, 120)}`));
    for (const node of this.state.board.nodes.filter((n: any) => n.project === this.project)) body.append(this.button(`${node.name}: start new managed session`, () => this.post("start", { id: node.id })));
    const runtime = this.state.lifecycle || {};
    body.append(element("h3", "Runtime & updates"), element("p", `Extension ${this.state.version} \u00b7 Python ${runtime.capabilities?.python || "Check executable path"} - core/tools ${runtime.integrity === "VERIFIED" ? "Integrity verified (VERIFIED)" : "Check required"} - active terminals ${runtime.active || 0}`));
    if (runtime.error) body.append(element("p", runtime.error));
    if (runtime.persistence && runtime.persistence !== "READY") body.append(element("p", `Check board persistence: ${runtime.persistence}`));
    if (runtime.restartRequired) body.append(element("p", "The previous extension version differs. Finish your terminals, then reload the window."));
    body.append(element("p", "The VSIX includes core and workflow tools. Configure a Python 3.11+ executable. Finish active terminals before reloading after an update."));
    body.append(this.button("Check core & bundle integrity", () => this.post("runtimeRefresh")), this.button("Reload after finishing work", () => this.post("runtimeReload"))); this.runner(body);
  }
}
