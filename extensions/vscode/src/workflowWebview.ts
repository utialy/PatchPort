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
  private copyList: any;
  private copyKind = "batch";
  private copyDays = "30";
  private copySelected: string[] = [];
  private historySelected: string[] = [];
  private recordKind = "task";
  private bodySelected: string[] = [];
  private bodyScope = "";
  private connectionSelected: string[] = [];
  private connectionScope = "";
  private cleanupScope = "";
  private cleanupChoice: { history: Record<string,string[]>; task_ids: string[]; flow_ids: string[]; paths: string[] } = { history: {}, task_ids: [], flow_ids: [], paths: [] };
  private signature = "";
  private seenResult: any;
  private invalidate(): void { if (this.state?.workflow?.plan || this.state?.workflow?.state === "loading") { this.state.workflow.plan = false; this.post("workflowCancel"); } }
  private task = "";
  private flow = "";
  private role = "developer";
  private draft: any = { prompt: "", batch: "", flow: "", summaries: "", days: "30", modes: {}, summaryIds: {}, selected: [] };
  constructor(private post: Sender, private openModal: (title: string) => HTMLElement) {}
  private button(text: string, action: () => void): HTMLButtonElement { const b = element("button", text); b.type = "button"; b.onclick = action; b.disabled = this.state?.workflow?.state === "loading" || this.state?.projectData?.busy === true || this.state?.conversationData?.busy === true || this.state?.recordBody?.busy === true || this.state?.connectionData?.busy === true || this.state?.cleanupData?.busy === true; return b; }
  private input(name: string, hint: string, multiline = false): HTMLInputElement | HTMLTextAreaElement { const input = multiline ? element("textarea") : element("input"); input.value = this.draft[name] || ""; input.placeholder = hint; input.setAttribute("aria-label", hint); input.oninput = () => { this.draft[name] = input.value; this.invalidate(); }; return input; }
  private field(body: HTMLElement, label: string, input: HTMLElement): void { const wrapper = element("label"); wrapper.className = "field"; wrapper.append(element("span", label), input); body.append(wrapper); }
  private request(action: string, args: Record<string, unknown> = {}): void { this.post("workflow", { project: this.project, action, args }); }
  private loadData(): void { this.post("dataInspect", { project: this.project }); if (!this.state?.detachedProjects?.includes(this.project) && !this.state?.projectData?.pending) this.post("historyList", { project: this.project }); }
  open(project: string): void { this.opened = true; this.project = project; this.historySelected = []; this.post("recordsCancel"); this.post("connectionCancel"); this.post("cleanupCancel"); if (this.state) this.state.recordData = undefined; if (this.state?.conversationData) this.state.conversationData.list = undefined; if (this.state?.detachedProjects?.includes(project) || this.state?.projectData?.pending) this.tab = "data"; this.openModal("Project work"); this.render(); if (this.tab === "data") this.loadData(); else { this.post("coreRefresh", { project }); this.request("context.inventory"); } }
  close(): void { this.opened = false; this.historySelected = []; this.post("workflowCancel"); this.post("dataCancel"); this.post("historyCancel"); this.post("recordsCancel"); this.post("connectionCancel"); this.post("cleanupCancel"); }
  update(state: any): void {
    this.state = state;
    if (state.recordData?.state === "idle" || state.recordBody?.result?.kind === "purge") this.bodySelected = [];
    if (state.conversationData?.result?.body_deleted) this.historySelected = [];
    const view = state.workflow;
    if (view?.project === this.project && view.action === "copies.list" && view.result?.kind === this.copyKind && view.result?.days === Number(this.copyDays)) this.copyList = view.result;
    if (view?.project === this.project && view.result && view.result !== this.seenResult && JSON.stringify(view.result) !== JSON.stringify(this.seenResult)) {
      this.seenResult = view.result;
      if (view.action === "context.inventory") this.inventory = view.result;
      if (view.action === "proposal.inspect") this.proposal = view.result;
      if (view.action === "roles.overview") this.roles = view.result;
      if (view.action === "roles.inspect") this.roleView = view.result;
      if (view.result.operation === "copy_cleanup" && !view.plan) { this.copyList = undefined; this.copySelected = []; }
    }
    const signature = JSON.stringify([view, state.lifecycle, state.core, state.projectData, state.conversationData, state.recordData, state.recordBody, state.connectionData, state.cleanupData, state.detachedProjects]);
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
    projects.disabled = Boolean(this.state.cleanupData?.busy) || Boolean(this.state.connectionData?.busy) || this.state.projectData?.busy === true || this.state.conversationData?.busy === true || this.state.recordBody?.busy === true;
    projects.onchange = () => { this.project = projects.value; this.inventory = this.proposal = this.roles = this.roleView = this.copyList = undefined; this.copySelected = []; this.historySelected = []; this.task = this.flow = ""; this.draft.selected = []; this.post("workflowCancel"); this.post("dataCancel"); this.post("historyCancel"); this.post("recordsCancel"); this.post("connectionCancel"); this.post("cleanupCancel"); this.state.recordData = undefined; if (this.tab === "data") this.loadData(); else { this.post("coreRefresh", { project: this.project }); this.request("context.inventory"); } this.render(); };
    nav.append(projects);
    for (const [tab, label] of [["submit", "Submit work"], ["proposal", "Copies & recovery"], ["roles", "Roles & archives"], ["lifecycle", "Sessions & updates"], ["data", "Data & settings"]]) nav.append(this.button(label, () => { this.tab = tab; this.historySelected = []; this.post("workflowCancel"); this.post("dataCancel"); this.post("historyCancel"); this.post("recordsCancel"); this.post("connectionCancel"); this.post("cleanupCancel"); this.state.recordData = undefined; if (this.state.conversationData) this.state.conversationData.list = undefined; this.render(); if (tab === "data") this.loadData(); if (tab === "roles") this.request("roles.overview"); if (tab === "lifecycle") { this.post("runtimeRefresh"); this.post("coreRefresh", { project: this.project }); } }));
    body.append(nav);
    const section = element("section"); section.className = "workflow-form"; body.append(section);
    if (this.tab === "submit") this.submit(section);
    if (this.tab === "proposal") this.proposals(section);
    if (this.tab === "roles") this.rolePanel(section);
    if (this.tab === "lifecycle") this.lifecycle(section);
    if (this.tab === "data") this.dataPanel(section);
    const view = this.state.workflow;
    if (this.tab !== "data" && view?.project === this.project) {
      if (view.state === "loading") body.append(element("p", "Processing management action..."));
      if (view.error) { const error = element("p", view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
      if (view.plan) { const controls = element("div"); controls.className = "core-controls"; controls.append(this.button("Apply reviewed preview", () => this.post("workflowApply")), this.button("Discard preview", () => this.post("workflowCancel")), element("span", `Apply once - valid until ${new Date(view.expires).toLocaleTimeString()}`)); body.append(controls); }
      if (view.result && !["context.inventory", "proposal.inspect", "roles.overview", "roles.inspect"].includes(view.action)) { const details = element("details"); details.open = true; details.append(element("summary", view.plan ? "Plan to apply" : "Action result"), element("pre", JSON.stringify(view.result.report || view.result, null, 2))); body.append(details); }
    }
    if (focusLabel) { const field = Array.from(body.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[aria-label]")).find(i => i.getAttribute("aria-label") === focusLabel); if (field) { field.focus(); if (selection && field.tagName !== "SELECT" && field.type !== "number") field.setSelectionRange(selection[0], selection[1]); } }
  }
  private connectionPanel(body: HTMLElement): void {
    if (this.connectionScope !== this.project) { this.connectionScope = this.project; this.connectionSelected = []; }
    const view = this.state.connectionData || {}, current = view.project === this.project;
    const preview = (action: string) => this.post("connectionPreview", { project: this.project, action, paths: this.connectionSelected });
    body.append(element("h3", "Remove connection files and settings"), element("p", "Clean bodies first, then explicitly select owned connection files and user settings. Settings are removed last; ledgers, IDs, usage, backups and shared profiles remain."));
    body.append(this.button("Inspect connection files", () => { this.connectionSelected = []; this.post("connectionInventory", { project: this.project }); }));
    if (view.receipt?.project === this.project) body.append(this.button("Query connection-removal result", () => this.post("connectionOperation")));
    if (!current) return;
    if (view.error) { const error = element("p", view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
    const labels: Record<string,string> = { RECORDS_PREPARATION_REQUIRED:"Prepare ordinary-task ledger protection first",HISTORY_PREPARATION_REQUIRED:"Prepare conversation ledger protection first",BODY_CLEANUP_REQUIRED:"Clean remaining task/flow/conversation bodies first",BODY_PURGE_PENDING:"Finish interrupted body cleanup first",CONNECTION_REMOVED:"Connection removal recorded - query/resume or explicitly reconnect",CONNECTION_SHARED_REFERENCE:"Another configuration references these files/ledgers",CONNECTION_SHARED_LEDGER:"Another project uses the same conversation ledger",KNOWN_LEDGER_WRITER_ACTIVE:"A known ledger has an unfinished writer",SERVICE_REGISTRATION_PRESENT:"Explicitly unregister the service before continuing",RUNNER_OR_MANAGEMENT_ACTIVE:"Runner/management work is active",CONNECTION_FILE_NOT_OWNED:"Modified/pre-existing/unowned files cannot be removed",PLAN_CHANGED:"Data changed after preview",CONNECTION_REMOVE_PENDING:"Finish file removal first" };
    const blockers = view.preview?.blockers || view.inventory?.blockers;
    if (blockers?.length) body.append(element("p", blockers.map((code:string) => labels[code] || code).join(" \u00b7 ")));
    if (view.inventory?.items?.length) {
      const table=element("table"); table.className="usage-table record-inventory connection-inventory"; const header=element("tr");
      for (const title of ["Select","File","Ownership/protection"]) header.append(element("th",title)); table.append(header);
      for (const item of view.inventory.items) {
        const row=element("tr"),cell=element("td"),check=element("input"); check.type="checkbox"; check.setAttribute("aria-label",`${item.path} select for removal`);
        check.checked=this.connectionSelected.includes(item.path); check.disabled=Boolean(view.busy)||!item.selectable;
        check.onchange=()=>{this.connectionSelected=check.checked?[...this.connectionSelected,item.path]:this.connectionSelected.filter(p=>p!==item.path);this.post("connectionCancel"); this.post("cleanupCancel");this.render();};cell.append(check);
        row.append(cell,element("td",item.path),element("td",item.kind==="managed"?(item.selectable?"Managed ownership - hash matches":"Modified/missing - preserved"):item.kind==="user_config"?"User configuration - select separately":item.kind==="user_file"?"User file - select separately":"Unconfirmed ownership - preserved"));table.append(row);
      }
      const remove=this.button("Preview selected file removal",()=>preview("remove"));remove.disabled ||= !this.connectionSelected.length;body.append(table,remove);
    }
    if (view.preview) {
      const report=view.preview;
      body.append(element("h4",report.kind==="reconnect"?"Review explicit reconnection":"Review connection removal"),element("p",report.kind==="reconnect"?"Release execution protection using a valid configuration at the original path/state. No file restore, reinstall or AI start.":"Detach and block older writers. Only selected files are removed; interrupted results remain queryable/resumable without settings."));
      if (report.paths?.length) body.append(element("pre",report.paths.join("\n")));
      if (view.plan) body.append(this.button(report.kind==="reconnect"?"Apply reviewed reconnection":report.kind==="resume"?"Resume reviewed file removal":"Apply reviewed file removal",()=>this.post("connectionApply")),this.button("Discard connection-file preview",()=>this.post("connectionCancel")));
    }
    const result=view.result;
    if (result) {
      body.append(element("p",result.reconnected?"Reconnected - no file restore/automatic start":`Connection files: ${result.complete?"removal complete":"partial"} \u00b7 ${result.deleted}/${result.files} - ledger/usage preserved`));
      if (!result.complete && result.deleted === result.files) body.append(element("p","Selected files are removed, but the connection cleanup receipt is not finalized. Use resume below."));
      for (const item of result.items || []) body.append(element("p",`${item.path} \u00b7 ${item.status==="DELETED"?"Removed":"Files remain - explicit resume required"}`));
      if (!result.complete) body.append(this.button("Preview interrupted file-removal resume",()=>preview("resume")));
      if (result.complete&&!result.reconnected) body.append(element("p","Prepare a valid configuration at the original path/state, then explicitly reconnect."),this.button("Preview reconnection after removal",()=>preview("reconnect")));
    }
  }
  private cleanupPanel(body: HTMLElement): void {
    if (this.cleanupScope !== this.project) { this.cleanupScope=this.project; this.cleanupChoice={history:{},task_ids:[],flow_ids:[],paths:[]}; }
    const view=this.state.cleanupData || {}, current=view.project===this.project;
    body.append(element("h3","Guided project cleanup"),element("p","Clean conversations > ordinary tasks > flows > connection files/settings. Preview and explicitly apply each stage; settings are removed last."));
    body.append(this.button("Inspect project cleanup targets",()=>{this.cleanupChoice={history:{},task_ids:[],flow_ids:[],paths:[]};this.post("cleanupInventory",{project:this.project});}));
    if (view.receipt?.project===this.project) body.append(this.button("Query project cleanup result",()=>this.post("cleanupOperation")));
    if (!current) return;
    if (view.error) { const error=element("p",view.error);error.className="core-error";error.setAttribute("role","alert");body.append(error); }
    const names: Record<string,string>={history:"Conversation bodies",tasks:"Ordinary-task bodies",flow:"Complete flow bodies",connection:"Connection files/settings",finalize:"Finalize completion receipt"};
    const labels: Record<string,string>={DONE:"Complete",WAITING:"Awaiting review",RUNNING:"Result needs confirmation",PARTIAL:"Cleanup remains",NOT_ACCEPTED:"Not accepted - new preview required"};
    const inventory=view.inventory;
    if (inventory) {
      const table=element("table");table.className="usage-table record-inventory cleanup-inventory";const header=element("tr");
      for(const label of ["Select","Kind","ID / file"])header.append(element("th",label));table.append(header);
      const add=(id:string,title:string,selected:string[],limit:number,selectable:boolean,update:(values:string[])=>void)=>{
        const row=element("tr"),cell=element("td"),check=element("input");check.type="checkbox";check.setAttribute("aria-label",`${title} ${id} select for cleanup`);check.checked=selected.includes(id);check.disabled=Boolean(view.busy)||!selectable||(!check.checked&&selected.length>=limit);
        check.onchange=()=>{update(check.checked?[...selected,id]:selected.filter(value=>value!==id));this.post("cleanupCancel");this.render();};cell.append(check);row.append(cell,element("td",title),element("td",id));table.append(row);
      };
      for (const group of inventory.history_items || []) for (const item of group.items || []) add(item.id,"Conversation",this.cleanupChoice.history[group.database]||[],100,item.selectable,values=>{this.cleanupChoice.history[group.database]=values;});
      for (const item of inventory.task_items || []) add(item.id,"Ordinary task",this.cleanupChoice.task_ids,25,item.body_selectable,values=>{this.cleanupChoice.task_ids=values;});
      for (const item of inventory.flow_items || []) add(item.id,"flow",this.cleanupChoice.flow_ids,8,item.body_selectable,values=>{this.cleanupChoice.flow_ids=values;});
      for (const item of inventory.file_items || []) add(item.path,item.kind==="managed"?"Connection file":"User settings/file",this.cleanupChoice.paths,64,item.selectable,values=>{this.cleanupChoice.paths=values;});
      body.append(table);
      if (inventory.task_items_more || inventory.flow_items_more || inventory.history_items?.some((g:any)=>g.more)) body.append(element("p","This is a partial listing. Only selected scope is processed; unlisted records remain. Individual paginated lists are also available."));
      const history=Object.entries(this.cleanupChoice.history).filter(([,ids])=>ids.length).map(([database,session_ids])=>({database,session_ids}));
      const selected=this.cleanupChoice.task_ids.length+this.cleanupChoice.flow_ids.length+this.cleanupChoice.paths.length+history.reduce((n,g)=>n+g.session_ids.length,0);
      const preview=this.button("Preview selected cleanup scope",()=>this.post("cleanupPreview",{project:this.project,selection:{history,task_ids:this.cleanupChoice.task_ids,flow_ids:this.cleanupChoice.flow_ids,paths:this.cleanupChoice.paths}}));
      preview.disabled ||= !selected || Boolean(inventory.operations?.some((o:any)=>o.phase!=="DONE"));body.append(preview);
    }
    const summary=view.preview || view.result;
    if (summary) {
      body.append(element("p",`Cleanup stages: ${summary.completed_stages||0}/${summary.total_stages||summary.stages?.length||0} \u00b7 ${summary.scope_complete?"Selected scope complete":"Review next stage/remaining cleanup"}`));
      for(const stage of summary.stages || []) body.append(element("p",`${names[stage.kind]||stage.kind} \u00b7 ${labels[stage.status]||stage.status}${stage.flow_id?" \u00b7 "+stage.flow_id:""}`));
      if(summary.blockers?.length)body.append(element("p","Protection reasons: "+summary.blockers.join(" \u00b7 ")));
      if(summary.next_kind)body.append(element("h4","Review now: "+(names[summary.next_kind]||summary.next_kind)),element("p","The next stage is never applied automatically. Target or ledger changes after preview require another review."));
      const detail=summary.next_report;
      if(detail?.task_ids?.length)body.append(element("pre",detail.task_ids.join("\n")));
      if(detail?.session_ids?.length)body.append(element("pre",detail.session_ids.join("\n")));
      if(detail?.flow)body.append(element("p","Selected flow: "+detail.flow));
      if(detail?.paths?.length)body.append(element("pre",detail.paths.join("\n")));
      if(detail?.files!==undefined)body.append(element("p",`Files in this stage: ${detail.files} files - ${(detail.bytes||0).toLocaleString()} bytes`));
      if(view.plan)body.append(this.button("Apply reviewed cleanup stage",()=>this.post("cleanupApply")),this.button("Discard project cleanup preview",()=>this.post("cleanupCancel")));
      if(summary.residual){const r=summary.residual;body.append(element("h4","Remaining data and preserved scope"),element("p",`Bodies: tasks ${r.task_bodies??"Unknown"} \u00b7 flow ${r.flow_bodies??"Unknown"} - conversations ${r.history_bodies??"Unknown"} - connection backup files ${r.backup_files??"Unknown"}`),element("p","Control ledgers, usage, IDs, originals and backups remain. Provider history and other workspaces are outside scope; this does not erase every byte on disk."));if(r.unknown?.length)body.append(element("p","Residual inspection incomplete - unknown does not mean zero."));}
    }
    if (view.result && !view.result.scope_complete) body.append(this.button(view.result.state==="FINALIZE_PENDING"?"Preview completion-receipt finalization":"Preview next/interrupted cleanup stage",()=>this.post("cleanupPreview",{project:this.project})));
  }
  private dataPanel(body: HTMLElement): void {
    this.cleanupPanel(body);
    this.connectionPanel(body);
    const view = this.state.projectData || {}, current = view.project === this.project;
    const labels: Record<string, string> = { reset_board: "Reset project cards and routes", reset_config: "Attach default configuration", disconnect: "Settings Disconnect", select_config: "Attach selected configuration" };
    const status = (item: any): string => item?.status === "absent" ? "Absent" : item?.status === "present" ? "Confirmed" : "Needs attention";
    const count = (value: unknown): string => typeof value === "number" ? value.toLocaleString() : "Unknown";
    body.append(element("h3", "Project data and settings"), element("p", "Manage this workspace's configuration. Execution records, conversation bodies, original files, shared profiles and group limits remain."));
    body.append(this.button("Refresh saved-data inventory", () => this.post("dataInspect", { project: this.project })));
    if (view.pending) { body.append(element("p", "Configuration is saved, but workspace mirroring is unfinished. Finalization does not rerun records or AI work.")); body.append(this.button("Finalize configuration persistence", () => this.post("dataResume"))); }
    if (!current) return;
    if (view.state === "loading") body.append(element("p", "Inspecting saved data..."));
    if (view.error) { const error = element("p", view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
    const inventory = view.inventory;
    if (inventory) {
      body.append(element("p", inventory.detached ? "Configuration detached - explicit reconnection required. Previous task/conversation records remain and are not inspected here." : "Selected configuration: " + status(inventory.config)));
      const table = element("table"); table.className = "usage-table data-inventory";
      const header = element("tr"); for (const title of ["Data", "Status", "Count", "Logical size"]) header.append(element("th", title)); table.append(header);
      const visibleDatabases = (inventory.databases || []).filter((db: any) => db.status !== "absent");
      for (const db of visibleDatabases) {
        const row = element("tr");
        const title = db.batch && db.interactive ? "Task/conversation ledger" : db.batch ? "Task ledger" : db.interactive ? "Conversation ledger" : "Ledger needs inspection";
        const totals = [db.batch ? `Tasks: ${count(db.batch.tasks)}` : "", db.interactive ? `Sessions: ${count(db.interactive.sessions)} - events ${count(db.interactive.events)}` : ""].filter(Boolean).join(" \u00b7 ");
        row.append(element("td", title), element("td", status(db)), element("td", totals || "Unknown"), element("td", `${count(db.bytes)} bytes`)); table.append(row);
      }
      const kinds: Record<string, string> = { role_flows: "Role work/archive evidence", connection_backups: "Connection backups", task_copies: "Task copies/logs", role_jobs: "Role launch receipts/logs" };
      for (const item of inventory.artifacts || []) { const row = element("tr"); row.append(element("td", kinds[item.kind] || item.kind), element("td", status(item)), element("td", `${count(item.files)} files`), element("td", `${count(item.bytes)} bytes`)); table.append(row); }
      body.append(table, element("p", "Sizes are logical file sizes, not reclaimable space. Other workspaces/configurations and original provider conversations are outside inspection scope."));
      if (!visibleDatabases.length) body.append(element("p", "No saved ledger was found in this inspection scope."));
      const locations = element("details"); locations.append(element("summary", "Inspected configuration/storage paths"), element("pre", [inventory.config, ...(inventory.databases || []), ...(inventory.artifacts || [])].filter(Boolean).map((item: any) => `${status(item)} \u00b7 ${item.path}${item.reason ? " \u00b7 " + item.reason : ""}`).join("\n"))); body.append(locations);
      if (inventory.blockers?.length) body.append(element("p", `Check before changing: ${inventory.blockers.join(", ")}`));
      if (view.board) body.append(element("p", `Current board cards: ${view.board.cards} - shared profiles: ${view.profiles?.count ?? "?"} (preserved)`));
    }
    if (!view.pending && view.state !== "loading") {
      const controls = element("div"); controls.className = "core-controls";
      for (const action of ["reset_board", "reset_config", "disconnect"]) controls.append(this.button(`${labels[action]} preview`, () => this.post("dataPreview", { project: this.project, action })));
      controls.append(this.button("Select configuration and reconnect", () => this.post("dataSelectConfig", { project: this.project }))); body.append(controls);
    }
    if (view.plan) {
      body.append(element("h4", labels[view.plan.action]), element("p", `Remove cards: ${view.plan.report.cards}, routes: ${view.plan.report.connections}. Shared groups: ${view.plan.report.groupsPreserved} and records remain.`), element("p", `Configuration after apply: ${view.plan.report.config ?? "Detached - automatic discovery disabled"}`));
      body.append(this.button("Apply reviewed configuration change", () => this.post("dataApply")), this.button("Discard configuration preview", () => this.post("dataCancel")));
    }
    if (view.result) body.append(element("p", `Configuration saved - ${labels[view.result.action] || "Persistence finalization"} - execution records preserved`));
    this.copyPanel(body);
    body.append(element("p", "Copy cleanup removes task files/logs only. DB answers, usage and lifetime counts remain."));
    this.historyPanel(body);
    this.recordsPanel(body);
  }
  private recordsPanel(body: HTMLElement): void {
    if (this.bodyScope !== this.project+":"+this.recordKind) { this.bodyScope = this.project+":"+this.recordKind; this.bodySelected = []; }
    body.append(element("h3", "Task/flow references and ownership"), element("p", "Inspect storage paths, references and protection reasons without showing bodies. Inspection does not delete files or records."));
    if (this.state.detachedProjects?.includes(this.project) || this.state.projectData?.pending) { body.append(element("p", "Finish configuration attachment and persistence first.")); return; }
    const view = this.state.recordData || {}, current = view.project === this.project && view.kind === this.recordKind, busy = current && view.state === "loading";
    const kind = list([{ value: "task", label: "Task ledger" }, { value: "flow", label: "Role flows" }], this.recordKind); kind.setAttribute("aria-label", "Reference kind");
    kind.onchange = () => { this.recordKind = kind.value; this.bodySelected = []; this.post("recordsCancel"); this.post("connectionCancel"); this.post("cleanupCancel"); this.render(); };
    kind.disabled = Boolean(this.state.recordBody?.busy);
    const load = (cursor = "") => { if (!cursor) this.bodySelected = []; this.post("recordBodyCancel"); this.post("recordsList", { project: this.project, kind: this.recordKind, cursor }); };
    const controls = element("div"); controls.className = "core-controls";
    const refresh = this.button("Inspect references from first page", () => load()); refresh.disabled ||= busy;
    controls.append(kind, refresh); body.append(controls);
    if (!current) { this.bodyPanel(body); return; }
    if (busy) body.append(element("p", "Inspecting references and ownership..."));
    if (view.error) { const error = element("p", view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
    const labels: Record<string, string> = { LEGACY_UNATTRIBUTED: "Historical record - exclusive ownership unconfirmed", ROLE_REFERENCE: "Role reference - inspect the complete flow", ROLE_LINK_VERIFIED: "Role/ledger binding verified", UNCONFIRMED: "Ownership needs confirmation", READ_ONLY_STAGE: "Read-only inspection - no deletion", SHARED_STATE_UNCONFIRMED: "State overlaps another configuration", LEGACY_OWNERSHIP_UNCONFIRMED: "Historical task lacks exclusive ownership evidence", TASK_NOT_TERMINAL: "Task execution/exit needs confirmation", WRITER_ACTIVE: "Execution/management lock is in use", RECOVERY_DATA_PRESERVED: "Recovery/connection backups preserved", REFERENCE_SCOPE_UNKNOWN: "Unknown data/reference scope", ROLE_SCOPE_UNKNOWN: "Role ownership verification failed", ROLE_JOB_UNCONFIRMED: "Role launch completion unconfirmed", ROLE_JOB_SCOPE_UNKNOWN: "Role launch scope unconfirmed", FLOW_METADATA_MISSING: "Flow metadata missing", ARCHIVE_UNKNOWN: "Archive verification unconfirmed", ARCHIVE_INCOMPLETE: "Archive in progress/failed", STAGING_SCOPE_UNKNOWN: "Staging ownership unconfirmed", CONFIG_UNKNOWN: "Selected configuration needs inspection", KNOWN_CONFIG_UNKNOWN: "Known configuration needs inspection", INVENTORY_LIMIT: "Reference/file inventory limit exceeded", READ_LIMIT: "Read limit exceeded", METADATA_LIMIT: "Metadata limit exceeded", DATABASE_SIZE_LIMIT: "Database size limit exceeded", DATABASE_UNREADABLE: "Database damaged/WAL/read unconfirmed", DATA_CHANGED: "Data changed during inspection", SERVICE_REGISTRATION_PRESENT: "Service registration data preserved", CONTROL_UNKNOWN: "Control records need inspection", LEGACY_SCHEMA: "Historical schema - some records unsupported", RECORD_MISSING: "Selected record missing" };
    const reason = (values: string[]) => values.map(v => labels[v] || v).join(" \u00b7 ");
    Object.assign(labels, { CONNECTION_ASSET_CHANGED: "Connection assets changed/missing - no automatic adoption", CONNECTION_MANIFEST_UNKNOWN: "Routes manifest Needs attention", MANIFEST_HASH_MATCH: "Managed manifest hash matches", MODIFIED_OR_MISSING: "Modified or missing", RELATED_TASK_REFERENCES: "Other tasks retain summary references", FLOW_NOT_TERMINAL: "flow Stop Needs attention", UNEXPECTED_EXCLUDED_DATA: "Excluded data/path boundary needs inspection" });
    Object.assign(labels, { PREPARED_OWNED: "Protected project record", LEGACY: "Historical record preserved", NORMAL: "Body preserved", PURGING: "Body deletion interrupted/in progress", DELETED: "Body deleted" });
    const summary = (value: any) => {
      if (!value) return;
      if (!value.complete) body.append(element("p", "Inspection is incomplete. Do not treat unknown totals/sizes as zero."));
      if (value.blockers?.length) body.append(element("p", reason(value.blockers)));
    };
    summary(view.detail || view.list);
    if (view.list) {
      const table = element("table"); table.className = "usage-table record-inventory"; const header = element("tr");
      for (const title of [...(this.recordKind === "task" ? ["Select body"] : []), "Task/flow ID", "Status", "Ownership evidence", "References"]) header.append(element("th", title)); table.append(header);
      for (const item of view.list.items || []) {
        const row = element("tr"), cell = element("td"); const inspect = this.button("Reference details", () => this.post("recordsInspect", { project: this.project, kind: this.recordKind, recordId: item.id })); inspect.disabled ||= busy; cell.append(inspect);
        if (this.recordKind === "flow") { const purge = this.button("Preview flow body deletion", () => this.post("recordBodyPreview", { project: this.project, action: "flow.purge", taskIds: [item.id] })); purge.disabled ||= busy || Boolean(this.state.recordBody?.busy) || !item.body_selectable; cell.append(purge); }
        if (this.recordKind === "task") { const selected = element("td"), check = element("input"); check.type = "checkbox"; check.setAttribute("aria-label", `${item.id} select task body`); check.checked = this.bodySelected.includes(item.id); check.disabled = busy || Boolean(this.state.recordBody?.busy) || !item.body_selectable || (!check.checked && this.bodySelected.length >= 25); check.onchange = () => { this.bodySelected = check.checked ? [...this.bodySelected,item.id] : this.bodySelected.filter(id => id !== item.id); this.post("recordBodyCancel"); this.render(); }; selected.append(check); row.append(selected); }
        row.append(element("td", item.id), element("td", item.state+(item.body_state ? " \u00b7 "+(labels[item.body_state] || item.body_state) : "")), element("td", labels[item.ownership] || item.ownership), cell); table.append(row);
      }
      body.append(table, element("p", `Currently returned: ${view.list.items.length} files - ${!view.list.complete ? "Inspection incomplete - total scope unknown" : view.list.more ? "More pages" : "End of list"}`));
      if (view.list.more) body.append(this.button("Next reference-list page", () => load(view.list.next_cursor)));
    }
    this.bodyPanel(body, view.list);
    const detail = view.detail;
    if (!detail) return;
    const categories: Record<string, string> = { body: "Body copies", control: "Execution/deduplication records", external: "User originals/settings/external references", recovery: "Recovery/connection backups", unknown: "Classification needs inspection" };
    const count = (n: any) => typeof n === "number" ? n.toLocaleString() : "Unknown";
    body.append(element("h4", `Reference details - ${view.recordId}`), element("p", `${labels[detail.selection?.ownership] || "Ownership needs confirmation"}${detail.selection?.required_scope === "whole_flow" ? " - role tasks show the complete flow's references." : ""}`));
    for (const [key, item] of Object.entries(detail.summary?.categories || {}) as [string, any][]) body.append(element("p", `${categories[key]}: references ${count(item.references)} - logical size ${count(item.bytes)} bytes`));
    body.append(element("p", "DB payloads and DB file sizes are separate references. Their sum is not reclaimable space. Paths and IDs may contain sensitive names."));
    const table = element("table"); table.className = "usage-table record-references"; const header = element("tr"); for (const title of ["Classification", "Location", "Observation/size"]) header.append(element("th", title)); table.append(header);
    for (const ref of detail.references || []) { const row = element("tr"), location = element("td", ref.location); location.style.overflowWrap = "anywhere"; row.append(element("td", categories[ref.category] || ref.category), location, element("td", `${ref.status === "present" ? "Present" : ref.status === "absent" ? "Absent" : "Unknown"} \u00b7 ${count(ref.bytes)} bytes${ref.link_count > 1 ? ` - logical references ${ref.link_count}` : ""}${ref.ownership ? " \u00b7 "+(labels[ref.ownership] || ref.ownership) : ""}${ref.reason ? " \u00b7 "+(labels[ref.reason] || ref.reason) : ""}`)); table.append(row); }
    body.append(table);
    if (detail.more) body.append(this.button("Next reference-details page", () => this.post("recordsInspect", { project: this.project, kind: this.recordKind, recordId: view.recordId, cursor: detail.next_cursor })));
    body.append(element("p", "Only the selected configuration and known references were inspected. Other workspaces, unregistered configurations, provider originals, authentication and shared profiles are outside scope."));
  }
  private bodyPanel(body: HTMLElement, list?: any): void {
    const view = this.state.recordBody || {}, current = view.project === this.project;
    body.append(element("h4", "Task/flow body management"), element("p", "Select up to 25 finished tasks submitted after protection preparation. For roles, select one entire flow created after additional role preparation. Usage, limits, IDs and originals remain after removing both roles, copies, archives and receipt bodies. Historical/private data remain."));
    const preview = (action: string) => this.post("recordBodyPreview", { project: this.project, action, taskIds: this.bodySelected });
    if (list?.queue_body_schema === 0) body.append(this.button("Preview ordinary-task ledger protection", () => preview("prepare")));
    if (list?.queue_body_schema === 1) body.append(this.button("Preview role ledger protection", () => preview("roles.prepare")));
    if (list?.queue_body_schema >= 1 && this.recordKind === "task") { const button = this.button(`Selected: ${this.bodySelected.length} task bodies - preview deletion`, () => preview("purge")); button.disabled ||= !this.bodySelected.length; body.append(button); }
    if (view.receipt?.project === this.project) body.append(this.button("Query task body result", () => this.post("recordBodyOperation")));
    if (list?.pending_body_operations?.length) body.append(this.button("Discover interrupted operation in this ledger", () => this.post("recordBodyDiscover", { project: this.project })));
    if (!current) return;
    const reasons: Record<string,string> = { ALREADY_PREPARED:"Ledger already protected",RECORDS_PREPARATION_REQUIRED:"Ledger protection preparation required",LEGACY_OWNERSHIP_UNCONFIRMED:"Historical record - ownership unconfirmed",ROLE_QUEUE_UPGRADE_REQUIRED:"Role data blocks preparation/deletion",RECORDS_WRITER_ACTIVE:"Runner writer has not exited",RECORDS_WRITER_LOST:"Writer exit unconfirmed",RECORDS_WRITER_UNCONFIRMED:"Actual process-exit evidence unconfirmed",TASK_NOT_TERMINAL:"Task completion needs confirmation",RUNNER_OR_MANAGEMENT_ACTIVE:"Runner/management lock in use",RUNNER_UNCONFIRMED:"Runner launch/exit unconfirmed",SERVICE_REGISTRATION_PRESENT:"Service registration protection",RELATED_TASKS_REQUIRED:"Explicitly select referencing tasks too",BODY_DELETED:"Body already deleted",BODY_PURGE_PENDING:"Finish previous body deletion first",SHARED_STATE_UNCONFIRMED:"State overlaps another configuration",RECORDS_PLATFORM_UNSUPPORTED:"Body deletion is not supported on this OS",OPERATION_ALREADY_COMPLETE:"Operation already complete",PLAN_CHANGED:"Changed after preview",OPERATION_IDENTITY_CHANGED:"Ledger/operation identity changed" };
    if (view.busy) body.append(element("p","Managing task bodies..."));
    Object.assign(reasons, { WHOLE_FLOW_REQUIRED:"Select the whole flow for role data",FLOW_WRITER_ACTIVE:"Flow execution has not ended",FLOW_WRITER_ACCEPTED:"Flow launch receipt needs inspection",FLOW_WRITER_LOST:"Flow exit unconfirmed",FLOW_WRITER_UNCONFIRMED:"Actual flow-exit evidence unconfirmed",ROLE_SCOPE_UNKNOWN:"Role ownership/references need inspection",PRIVATE_QUEUE_UNCONFIRMED:"Historical private ledger preserved",ARCHIVE_INCOMPLETE:"Inspect interrupted archive first",ARCHIVE_STAGING_UNCONFIRMED:"Archive staging needs inspection",ROLE_JOB_UNCONFIRMED:"Role job final receipt needs inspection",FLOW_NOT_TERMINAL:"flow Stop Needs attention" });
    if (view.error) { const error = element("p",view.error); error.className = "core-error"; error.setAttribute("role","alert"); body.append(error); }
    const report = view.preview;
    if (report) {
      if (report.kind === "roles_prepare") body.append(element("p","Older cores cannot use the ledger after enabling role protection. Only new shared flows become manageable; existing IDs, usage and limits remain."));
      if (report.flow) body.append(element("p",`Complete selected flow: ${report.flow} - includes both roles/archive/receipt bodies`));
      if (report.kind !== "roles_prepare") body.append(element("p",report.kind === "prepare" ? "Preparation blocks older writers and role execution. Existing records/copies stay read-only; progress logs retain counts/sizes rather than bodies." : `Target tasks: ${report.tasks}${report.files === undefined ? "" : ` - files: ${report.files} files - ${report.bytes.toLocaleString()} bytes`}`));
      if (report.blockers?.length) body.append(element("p",report.blockers.map((code:string) => reasons[code] || code).join(" \u00b7 ")));
      if (report.required_ids?.length) body.append(element("p","Task IDs to select together"),element("pre",report.required_ids.join("\n")));
      if (this.bodySelected.length) { const chosen = element("details"); chosen.append(element("summary","Selected task IDs"),element("pre",this.bodySelected.join("\n"))); body.append(chosen); }
      if (view.plan) body.append(this.button(report.kind === "roles_prepare" ? "Apply reviewed role ledger protection" : report.kind === "prepare" ? "Apply reviewed ordinary-task ledger protection" : ["resume","flow_resume"].includes(report.kind) ? "Resume reviewed task body deletion" : report.flow ? "Apply reviewed complete flow body deletion" : "Apply reviewed task body deletion",() => this.post("recordBodyApply")),this.button("Discard task body preview",() => this.post("recordBodyCancel")));
    }
    const result = view.result;
    if (result) {
      body.append(element("p",result.found === false ? "Completion receipt not found. Inspect the state." : result.kind === "roles_prepare" ? "Role ledger protection ready - refresh the list." : result.kind === "prepare" ? "Ordinary-task ledger protection ready - refresh the list." : `Task bodies: ${result.state === "DONE" ? "deletion complete" : "partial - cleanup remains"} \u00b7 ${(result.items || []).filter((item:any) => item.state === "DELETED").length}/${(result.items || []).length}`));
      for (const item of result.items || []) body.append(element("p",`${item.task} \u00b7 ${item.state === "DELETED" ? "Body deleted" : "Body access blocked - copy cleanup unfinished"}`));
      if (["purge","flow_purge"].includes(result.kind) && result.state !== "DONE") body.append(this.button("Preview interrupted task body deletion resume",() => preview("resume")));
    }
    body.append(element("p","Deletion cannot be undone. Interrupted file cleanup retains isolated data and receipts without automatic retry. Provider history, external copies and forensic disk erasure are outside scope."));
  }
  purged(project: string): void { if (this.project !== project) return; this.proposal = this.roleView = this.roles = this.copyList = this.inventory = undefined; this.bodySelected = []; this.draft.selected = []; this.draft.summaryIds = {}; if (this.opened) this.render(); }
  private historyPanel(body: HTMLElement): void {
    body.append(element("h3", "Conversation body management"), element("p", "Select up to 100 managed sessions with confirmed exit. Only bodies, conversation names and provider resume information are removed; usage, delivery counts and IDs remain."));
    if (this.state.detachedProjects?.includes(this.project) || this.state.projectData?.pending) { body.append(element("p", "Finish configuration attachment and persistence first.")); return; }
    body.append(this.button("Inspect conversations from first page", () => { this.historySelected = []; this.post("historyList", { project: this.project }); }));
    const view = this.state.conversationData || {};
    if (view.receipt?.project === this.project) body.append(this.button("Query last body management result", () => this.post("historyOperation")));
    if (view.project !== this.project) return;
    const labels: Record<string, string> = { READY: "Selectable", BODY_DELETED: "Body deleted - cannot resume", LEGACY_SESSION: "Historical record - no exit evidence", SESSION_EXIT_UNCONFIRMED: "Stop Needs attention", WRITER_ACTIVE: "Managed session is running", WRITER_LOST: "Connection lost - exit cannot be confirmed", OWNER_CLOSE_UNCONFIRMED: "Writer connection closure needs confirmation", DELIVERY_SCOPE_UNKNOWN: "Delivery ownership cannot be confirmed", RELATED_SESSIONS_REQUIRED: "Select both sessions of a shared delivery", DELIVERY_PENDING: "Delivery in progress", ALREADY_DELETED: "Session already deleted", LEGACY_WRITER_UNCONFIRMED: "Older managed sessions need confirmed shutdown", ALREADY_READY: "Ledger protection ready", UNSUPPORTED_SCHEMA: "Unsupported ledger" };
    const reasons = (values: string[]) => values.map(v => labels[v] || v).join(" \u00b7 ");
    if (view.busy) body.append(element("p", "Inspecting conversation ledger..."));
    if (view.error) { const error = element("p", view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
    const page = view.list;
    if (page) {
      if (page.preparation_required) body.append(element("p", "Existing ledgers require protection preparation first. Stop all older managed terminals. Existing records remain and are not adopted for deletion."), this.button("Preview ledger protection", () => this.post("historyPreview", { project: this.project, action: "prepare" })));
      if (page.blockers?.length) body.append(element("p", reasons(page.blockers)));
      const table = element("table"); table.className = "usage-table history-inventory";
      const header = element("tr"); for (const label of ["Select", "Session", "Exit/protection state"]) header.append(element("th", label)); table.append(header);
      for (const item of page.items) {
        const row = element("tr"), cell = element("td"), check = element("input"); check.type = "checkbox"; check.checked = this.historySelected.includes(item.id);
        check.disabled = view.busy || !item.selectable || Boolean(page.blockers?.length) || (!check.checked && this.historySelected.length >= 100);
        check.setAttribute("aria-label", `${item.id} select conversation`);
        check.onchange = () => { this.historySelected = check.checked ? [...this.historySelected, item.id] : this.historySelected.filter(id => id !== item.id); this.post("historyCancel"); this.render(); };
        cell.append(check); row.append(cell, element("td", `${item.name || item.provider}\n${item.id}\n${new Date(item.started * 1000).toLocaleString()}`), element("td", labels[item.reason] || item.reason)); table.append(row);
      }
      body.append(table, element("p", `Current page: ${page.items.length} - total selected: ${this.historySelected.length} (retained across pages)`));
      if (page.more) body.append(this.button("Next conversation page", () => this.post("historyList", { project: this.project, cursor: page.next_cursor })));
      const preview = this.button("Preview selected conversation body deletion", () => this.post("historyPreview", { project: this.project, action: "purge", sessionIds: this.historySelected })); preview.disabled ||= !this.historySelected.length; body.append(preview);
      if (this.historySelected.length) { const selected = element("details"); selected.append(element("summary", "Selected session IDs"), element("pre", this.historySelected.join("\n")), this.button("Clear conversation selection", () => { this.historySelected = []; this.post("historyCancel"); this.render(); })); body.append(selected); }
    }
    if (view.preview) {
      const preview = view.preview;
      body.append(element("h4", preview.kind === "prepare" ? "Review ledger protection" : "Review conversation body deletion"));
      if (preview.kind === "purge") body.append(element("p", `Sessions: ${preview.sessions} - events: ${preview.events} - shared deliveries: ${preview.deliveries}`));
      if (preview.blockers?.length) body.append(element("p", reasons(preview.blockers)));
      if (preview.required_sessions?.length) body.append(element("p", "Explicitly select these sessions in the list to remove shared deliveries together."), element("pre", preview.required_sessions.join("\n")));
      if (view.plan) body.append(this.button(preview.kind === "prepare" ? "Apply reviewed ledger protection" : "Apply reviewed conversation body deletion", () => this.post("historyApply")), this.button("Discard body management preview", () => this.post("historyCancel")));
    }
    if (view.result) body.append(element("p", view.result.found === false ? "No completion receipt. Inspect the ledger state again." : view.result.kind === "prepare" ? "Ledger protection ready - historical records preserved" : `Body deletion complete - sessions: ${view.result.sessions} - events: ${view.result.events} files - Delivery ${view.result.deliveries} - usage/counts preserved`));
    body.append(element("p", "Deleted bodies cannot be recovered or resumed as provider conversations here. Provider originals, OS backups, other windows' previous views and batch/flow bodies are outside this action's scope."));
  }
  private loadCopies(cursor = ""): void {
    this.copySelected = []; this.copyList = undefined;
    this.request("copies.list", { kind: this.copyKind, days: Number(this.copyDays), cursor, limit: 25 });
  }
  private copyPanel(body: HTMLElement): void {
    body.append(element("h3", "Task copy cleanup"), element("p", "Select up to 25 ordinary tasks on this page or one role copy. All copy contents and recovery protection are rechecked before deletion."));
    if (this.state.detachedProjects?.includes(this.project) || this.state.projectData?.pending) { body.append(element("p", "Finish configuration attachment/persistence before listing copies.")); return; }
    const kind = list([{ value: "batch", label: "Ordinary-task copies" }, { value: "role", label: "Role copies (shared/historical queues)" }], this.copyKind);
    kind.setAttribute("aria-label", "Copy kind");
    kind.onchange = () => { this.copyKind = kind.value; this.copyList = undefined; this.copySelected = []; this.post("workflowCancel"); this.render(); };
    const days = element("input"); days.type = "number"; days.min = "1"; days.max = "3650"; days.value = this.copyDays; days.setAttribute("aria-label", "Copy retention days");
    days.oninput = () => { this.copyDays = days.value; this.copyList = undefined; this.copySelected = []; this.post("workflowCancel"); this.render(); };
    const busy = this.state.workflow?.state === "loading"; kind.disabled = busy; days.disabled = busy;
    const controls = element("div"); controls.className = "core-controls";
    const refresh = this.button("Inspect copies from first page", () => this.loadCopies());
    refresh.disabled ||= !Number.isInteger(Number(this.copyDays)) || Number(this.copyDays) < 1 || Number(this.copyDays) > 3650;
    controls.append(kind, element("span", "Retention days"), days, refresh); body.append(controls);
    const reason: Record<string, string> = { ELIGIBLE: "Eligible for cleanup", DELETED: "deletion complete", ALREADY_DELETED: "Already cleaned", TASK_NOT_TERMINAL: "Task in progress", RETENTION_PERIOD: "Retention period not met", RECOVERY_DATA_PROTECTED: "Recovery backup protected", ARTIFACTS_MISSING: "Copy missing", UNSAFE_OR_INCOMPLETE: "Path/recovery data need inspection", UNSAFE_OR_CHANGED: "Copy changes/path need inspection", RUNNER_OR_MANAGEMENT_ACTIVE: "Runner/management work active", FLOW_ACTIVE_OR_INCOMPLETE: "Role execution/state needs inspection", FLOW_ARCHIVE_PENDING: "Archive state needs inspection", ROLE_NOT_READY_OR_UNSAFE: "Role state/ownership needs inspection", DELETE_FAILED: "Deletion failed - inspect state", PLAN_CHANGED: "Changed after preview" };
    Object.assign(reason, { CLEANUP_RECORD_FAILED: "Cleanup receipt failed - nothing deleted", DELETE_UNCONFIRMED: "Deletion completion receipt unconfirmed" });
    Object.assign(reason, { BODY_DELETED: "Body deleted", BODY_PURGE_PENDING: "Body deletion needs finalization", LEGACY_OWNERSHIP_UNCONFIRMED: "Historical copy - unconfirmed ownership preserved", RECORDS_WRITER_UNCONFIRMED: "Writer exit evidence needs inspection" });
    const label = (artifact: any): string => (reason[artifact?.reason] || artifact?.reason || "Needs attention") + (artifact?.cleanup_state === "DELETE_FAILED" ? " - previous deletion failed" : artifact?.cleanup_state === "DELETING" ? " - interrupted cleanup needs inspection" : "");
    const bytes = (value: unknown): string => typeof value === "number" ? `${value.toLocaleString()} bytes` : "Size unknown";
    if (this.copyList) {
      const table = element("table"); table.className = "usage-table copy-inventory";
      const header = element("tr"); for (const title of ["Select", "Task/role", "Protection/cleanup state", "Logical size", ...(this.copyKind === "role" ? ["Archive evidence"] : [])]) header.append(element("th", title)); table.append(header);
      for (const item of this.copyList.items) {
        const row = element("tr"), selectCell = element("td");
        if (item.kind === "batch") {
          const check = element("input"); check.type = "checkbox"; check.checked = this.copySelected.includes(item.task_id); check.disabled = busy || !item.artifact?.eligible;
          check.setAttribute("aria-label", `${item.task_id} select copy`);
          check.onchange = () => { this.copySelected = check.checked ? [...this.copySelected, item.task_id] : this.copySelected.filter(id => id !== item.task_id); this.post("workflowCancel"); this.render(); }; selectCell.append(check);
        } else if (item.artifact?.eligible) selectCell.append(this.button("Preview", () => this.request("roles.prune.preview", { flow_id: item.flow_id, role: item.role, task_id: item.task_id, days: Number(this.copyDays) })));
        row.append(selectCell, element("td", item.kind === "role" ? `${item.flow_id} \u00b7 ${item.role}\n${item.task_id || "Task unconfirmed"}` : item.task_id), element("td", label(item.artifact)), element("td", bytes(item.artifact?.bytes)));
        if (item.kind === "role") row.append(element("td", item.archive === "ARCHIVE_READY" ? "Verified archive preserved" : item.archive === "ABSENT" ? "No archive" : "Archive unconfirmed"));
        table.append(row);
      }
      body.append(table, element("p", `Current page: ${this.copyList.items.length} files - ${this.copyList.more ? "More pages" : "End of list"} - items outside this page are not selected.`));
      if (this.copyList.more) body.append(this.button("Next copies page", () => this.loadCopies(this.copyList.next_cursor)));
      if (this.copyKind === "batch") { const preview = this.button(`Selected: ${this.copySelected.length} copies - preview cleanup`, () => this.request("copies.prune.preview", { ids: this.copySelected, days: Number(this.copyDays) })); preview.disabled ||= this.copySelected.length === 0; body.append(preview); }
    }
    const view = this.state.workflow;
    if (view?.project === this.project && (view.action?.startsWith("copies.") || view.action === "roles.prune.preview" || view.result?.operation === "copy_cleanup")) {
      if (view.state === "loading") body.append(element("p", "Processing copy cleanup..."));
      const report = view.result?.report || view.result;
      if (view.error) { const error = element("p", report?.operation === "copy_cleanup" && report.apply ? "Some copies were not deleted or their completion receipts are unconfirmed. Inspect each result below." : view.error); error.className = "core-error"; error.setAttribute("role", "alert"); body.append(error); }
      if (report?.operation === "copy_cleanup") {
        const items = report.items || report.tasks?.map((artifact: any) => ({ task_id: artifact.id, artifact })) || (report.artifact ? [{ task_id: report.task, artifact: report.artifact }] : []);
        body.append(element("h4", view.plan ? "Copy deletion preview" : report.apply ? "Copy cleanup result" : "Copy protection check"));
        for (const item of items) body.append(element("p", `${item.task_id} \u00b7 ${label(item.artifact)} \u00b7 ${bytes(item.artifact.bytes)}`));
        if (report.apply) body.append(element("p", `Confirmed deletion: ${items.filter((item: any) => item.artifact.deleted === true).length}/${items.length}${items.some((item: any) => item.artifact.deleted === null) ? " - some completion receipts unconfirmed" : ""}. Inspect failed/undeleted items and create a new plan. No automatic retry.`));
        body.append(element("p", "Copy/log deletion cannot be undone. DB answers, usage, control records, originals and existing archive evidence remain."));
        if (view.plan) body.append(this.button("Apply reviewed copy deletion", () => this.post("workflowApply")), this.button("Discard copy preview", () => this.post("workflowCancel")));
      }
    }
    body.append(this.button("Open roles and archives", () => { this.tab = "roles"; this.post("workflowCancel"); this.request("roles.overview"); this.render(); }));
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
    for (const session of this.state.core?.history?.sessions || []) { body.append(element("p", `${session.body_deleted ? "Body deleted - cannot resume" : session.name} \u00b7 ${session.state} \u00b7 ${new Date(session.started * 1000).toLocaleString()} \u00b7 ${session.id}`)); if (!session.body_deleted && ["claude", "codex"].includes(session.provider)) body.append(this.button("Resume this provider conversation", () => this.post("resumeSession", { project: this.project, sessionId: session.id }))); }
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
