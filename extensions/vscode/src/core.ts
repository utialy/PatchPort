import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID, createHash } from "node:crypto";
import * as path from "node:path";
import { promises as fs } from "node:fs";
import type { Delivery, NodeConfig, Profile } from "./model";
import { projectKey } from "./model";

export class CoreClient {
  private child?: ChildProcessWithoutNullStreams;
  private buffer = "";
  private chain: Promise<unknown> = Promise.resolve();
  private idle?: NodeJS.Timeout;
  private pending?: { id: string; resolve(value: unknown): void; reject(error: Error): void; timer: NodeJS.Timeout };
  private plans = new Set<string>();
  private holds = new Set<string>();
  epoch = 0;
  retain(id: string): void { this.holds.add(id); if (this.idle) clearTimeout(this.idle); }
  release(id: string): void { this.holds.delete(id); this.scheduleIdle(); }
  constructor(readonly python: string, readonly source: string) {}
  request(action: string, args: Record<string, unknown> = {}): Promise<any> {
    const next = this.chain.then(() => this.exchange(action, args));
    this.chain = next.catch(() => {}); return next;
  }
  private open(): ChildProcessWithoutNullStreams {
    if (this.child) return this.child;
    const env = { ...process.env, PYTHONPATH: this.source, PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" };
    delete (env as NodeJS.ProcessEnv).PYTHONHOME;
    const child = spawn(this.python, ["-B", "-X", "utf8", "-m", "agent_bridge", "manage"], { env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child; this.buffer = ""; this.epoch++;
    child.unref();
    for (const stream of [child.stdin, child.stdout, child.stderr]) (stream as unknown as { unref?: () => void }).unref?.();
    const lost = (message: string) => { if (this.child !== child) return; this.child = undefined; this.epoch++; this.plans.clear(); this.fail(new Error(message)); };
    child.on("error", () => lost("Check the Python 3.11+ executable in patchport.corePython."));
    child.on("exit", () => lost("Core connection ended. Preview again; mutations are never retried automatically."));
    child.stdin.on("error", () => lost("Core input disconnected. Inspect the outcome before making a new preview."));
    child.stderr.on("data", () => {});
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      if (this.child !== child) return;
      this.buffer += chunk;
      if (Buffer.byteLength(this.buffer) > 1024 * 1024) { this.fail(new Error("The core response is too large.")); this.close(); return; }
      const line = this.buffer.indexOf("\n"); if (line < 0) return;
      const raw = this.buffer.slice(0, line); this.buffer = this.buffer.slice(line + 1);
      const pending = this.pending;
      if (!pending) { this.close(); return; }
      clearTimeout(pending.timer); this.pending = undefined;
      try {
        const reply = JSON.parse(raw);
        if (reply.protocol !== 1 || reply.id !== pending.id || typeof reply.ok !== "boolean") throw new Error("Unexpected core response format.");
        if (!reply.ok) throw Object.assign(new Error(`Core: ${reply.problem?.code || "OPERATION_FAILED"} \u00b7 ${reply.problem?.next_action || ""}`), { result: reply.result });
        if (typeof reply.result?.plan_id === "string") this.plans.add(reply.result.plan_id);
        pending.resolve(reply.result);
      } catch (error) { pending.reject(error instanceof Error ? error : new Error(String(error))); }
      this.scheduleIdle();
    });
    return child;
  }
  private fail(error: Error): void { const pending = this.pending; this.pending = undefined; if (pending) { clearTimeout(pending.timer); pending.reject(error); } }
  private scheduleIdle(): void { if (this.idle) clearTimeout(this.idle); if (this.holds.size) return; this.idle = setTimeout(() => { if (!this.pending) this.close(); }, this.plans.size ? 600000 : 2000); this.idle.unref(); }
  private close(): void { const child = this.child; this.child = undefined; this.epoch++; this.plans.clear(); this.buffer = ""; if (this.idle) clearTimeout(this.idle); child?.stdin.end(); }
  dispose(): void { this.fail(new Error("Core connection closed. Inspect any unconfirmed mutation.")); this.close(); }
  private exchange(action: string, args: Record<string, unknown>): Promise<any> {
    const id = randomUUID();
    const encoded = JSON.stringify({ protocol: 1, id, action, args }) + "\n";
    if (Buffer.byteLength(encoded) > 256 * 1024) return Promise.reject(new Error("The core request exceeds its size limit."));
    return new Promise((resolve, reject) => {
      if (this.idle) clearTimeout(this.idle);
      const child = this.open();
      if (typeof args.plan_id === "string" && ["operation.apply", "operation.cancel", "setup.apply", "setup.cancel", "history.apply", "history.cancel", "records.apply", "records.cancel", "connection.apply", "connection.cancel", "cleanup.apply", "cleanup.cancel", "local.schema.apply", "local.schema.cancel"].includes(action)) this.plans.delete(args.plan_id);
      const timer = setTimeout(() => { this.fail(new Error("Core response unconfirmed. Inspect results, backups and archives; do not retry automatically.")); this.close(); }, 45000);
      this.pending = { id, resolve, reject, timer };
      child.stdin.write(encoded);
    });
  }
}

export interface Binding { project: string; config: string; state?: string; database: string; linked: boolean; endpoints: Record<string, any>; context_budget?: unknown; control?: any }
export class Ledger {
  readonly owner = randomUUID();
  readonly sessions = new Map<string, { id: string; database: string; project: string; owner: string }>();
  private owners = new Map<string, { id: string; database: string; project: string; epoch: number }>();
  private associations = new Map<string, { database: string; project: string; owner: string; source_session: string; target_session: string }>();
  private chain: Promise<unknown> = Promise.resolve();
  private bindings = new Map<string, Binding>();
  private databases = new Set<string>();
  private pending = 0;
  constructor(readonly core: CoreClient, readonly storage: string, readonly changed: () => void = () => {}) {}
  private serial<T>(work: () => Promise<T>): Promise<T> { this.pending++; const next = this.chain.then(work).finally(() => this.pending--); this.chain = next.catch(() => {}); return next; }
  isBusy(project: string): boolean { return this.pending > 0 || [...this.sessions.values()].some(s => projectKey(s.project) === projectKey(project)); }
  forget(project: string): void { if (this.isBusy(project)) throw new Error("Finish recording session shutdown before trying again."); this.bindings.delete(projectKey(project)); }
  async candidates(project: string): Promise<string[]> {
    const values = [projectKey(project), project, await fs.realpath(project)];
    const paths = values.map(value => path.join(this.storage, createHash("sha256").update(value).digest("hex").slice(0, 24), "interactive.sqlite3"));
    const bound = this.binding(project)?.database; if (bound) paths.unshift(bound);
    return [...new Map(paths.map(value => [projectKey(value), value])).values()];
  }
  async inspect(project: string, config?: string): Promise<Binding> {
    const capabilities = await this.core.request("capabilities");
    const [major, minor] = String(capabilities.python).split(".").map(Number);
    if (major < 3 || major === 3 && minor < 11) throw new Error("Python 3.11 or later is required.");
    if (!capabilities.actions?.includes("interactive.record") || !capabilities.actions.includes("usage")) throw new Error("The selected core lacks the required management features.");
    const facts = await this.core.request("project.inspect", { project, ...(config ? { config } : {}) });
    const key = projectKey(project);
    const existing = this.bindings.get(key);
    const fallbackPath = (value: string) => path.join(this.storage, createHash("sha256").update(value).digest("hex").slice(0, 24), "interactive.sqlite3");
    let fallback = fallbackPath(key);
    const exists = async (file?: string) => Boolean(file && await fs.stat(file).then(s => s.isFile(), () => false));
    for (const legacy of [fallbackPath(project), fallbackPath(await fs.realpath(project))]) if (!await exists(fallback) && await exists(legacy)) fallback = legacy;
    const retained = existing && projectKey(existing.config) === projectKey(facts.config) && await exists(existing.database) ? existing.database : undefined;
    const database = retained || (await exists(fallback) ? fallback : facts.database || fallback);
    const binding = { ...facts, project, database } as Binding;
    if ([...this.sessions.values()].some(s => projectKey(s.project) === key && projectKey(s.database) !== projectKey(database))) throw new Error("Stop managed terminals before changing their database binding.");
    this.bindings.set(key, binding); return binding;
  }
  binding(project: string): Binding | undefined { return this.bindings.get(projectKey(project)); }
  async localSchema(project: string): Promise<any> {
    const binding = this.binding(project) || await this.inspect(project);
    const caps = await this.core.request("capabilities");
    if (caps.local_request_schema !== 1) throw new Error("Local request ledger support is required.");
    return this.core.request("local.schema.preview", { database: binding.database, project });
  }
  localIdentity(node: string): { id: string; database: string; project: string; owner: string; epoch: number } {
    const session = this.sessions.get(node);
    if (!session) throw new Error("The local managed session is missing.");
    this.checkOwner(session.project, session.owner);
    return { ...session, epoch: this.core.epoch };
  }
  localContext(node: string): Promise<any> {
    const s = this.localIdentity(node);
    return this.serial(async () => { this.checkLocalSession(node, s); return this.core.request("local.context", { database: s.database, project: s.project, session_id: s.id }); });
  }
  localPrepare(node: string, requestId: string, binding: Record<string, unknown>, requestBody: string): Promise<any> {
    return this.localMutation(node, "local.prepare", requestId, { binding, request_body: requestBody });
  }
  localDispatch(node: string, requestId: string, hash: string): Promise<any> { return this.localMutation(node, "local.dispatch", requestId, { binding_sha256: hash }); }
  localFinish(node: string, requestId: string, hash: string, outcome: Record<string, unknown>): Promise<any> { return this.localMutation(node, "local.finish", requestId, { binding_sha256: hash, outcome }); }
  private localMutation(node: string, action: string, requestId: string, extra: Record<string, unknown>): Promise<any> {
    const s = this.localIdentity(node);
    return this.serial(async () => {
      this.checkLocalSession(node, s);
      const result = await this.core.request(action, { database: s.database, owner: s.owner, session_id: s.id, request_id: requestId, ...extra, apply: true });
      this.changed(); return result;
    });
  }
  private checkLocalSession(node: string, expected: ReturnType<Ledger["localIdentity"]>): void {
    const current = this.localIdentity(node);
    if (current.id !== expected.id || current.owner !== expected.owner || current.epoch !== expected.epoch) throw new Error("The local managed session changed before recording.");
  }
  async start(node: NodeConfig): Promise<string> {
    const binding = this.binding(node.project) || await this.inspect(node.project);
    const id = randomUUID();
    await this.serial(async () => {
      const key = projectKey(node.project);
      let owner = this.owners.get(key);
      if (!owner) {
        const ownerId = randomUUID();
        const opened = await this.core.request("interactive.owner.open", { database: binding.database, project: node.project, owner: ownerId, apply: true });
        if (opened.writer_protocol !== 2) throw new Error("The current interactive writer protocol is required.");
        owner = { id: ownerId, database: binding.database, project: node.project, epoch: this.core.epoch };
        this.owners.set(key, owner); this.core.retain(ownerId);
      }
      this.checkOwner(node.project, owner.id);
      try { await this.core.request("interactive.begin", { database: binding.database, apply: true, session_id: id, node: node.id, project: node.project, provider: node.provider, name: node.name, owner: owner.id }); }
      catch (error) { if (![...this.sessions.values()].some(s => s.owner === owner!.id)) await this.closeOwner(node.project).catch(() => {}); throw error; }
      this.sessions.set(node.id, { id, database: binding.database, project: node.project, owner: owner.id });
      this.databases.add(binding.database);
    });
    return id;
  }
  record(node: string, kind: string, data: Record<string, unknown> = {}, turn?: string, eventId: string = randomUUID()): Promise<void> {
    const session = this.sessions.get(node); if (!session) return Promise.resolve();
    const identity = createHash("sha256").update(`${session.id}:${eventId}`).digest("hex");
    return this.serial(async () => { this.checkOwner(session.project, session.owner); await this.core.request("interactive.record", { database: session.database, owner: session.owner, session_id: session.id, event_id: identity, kind, data, ...(turn ? { turn } : {}), apply: true }); this.changed(); });
  }
  delivery(delivery: Delivery, project?: string): Promise<void> {
    let association = this.associations.get(delivery.id);
    if (!association) {
      const source = this.sessions.get(delivery.source), target = this.sessions.get(delivery.target);
      if (!source || !target) return Promise.reject(new Error("Both managed sessions must have durable records before delivery."));
      if (source.owner !== target.owner || projectKey(source.database) !== projectKey(target.database)) return Promise.reject(new Error("Delivery ledger ownership does not match."));
      association = { database: target.database, project: target.project, owner: target.owner, source_session: source.id, target_session: target.id };
      this.associations.set(delivery.id, association);
    }
    const owner = this.owners.get(projectKey(association.project));
    if (!owner || owner.id !== association.owner) return Promise.resolve();
    const { database, ...identity } = association;
    const data = { ...delivery, ...identity };
    return this.serial(async () => { this.checkOwner(identity.project, identity.owner); await this.core.request("interactive.delivery", { database, owner: identity.owner, data, apply: true }); this.changed(); });
  }
  private checkOwner(project: string, ownerId: string): void {
    const owner = this.owners.get(projectKey(project));
    if (!owner || owner.id !== ownerId || owner.epoch !== this.core.epoch) throw new Error("The writer connection was lost. Stop the terminal and start a new managed session.");
  }
  private async closeOwner(project: string): Promise<void> {
    const key = projectKey(project), owner = this.owners.get(key); if (!owner) return;
    try { this.checkOwner(project, owner.id); await this.core.request("interactive.end", { database: owner.database, owner: owner.id, apply: true }); }
    finally { this.owners.delete(key); this.core.release(owner.id); }
  }
  async end(): Promise<void> {
    for (const owner of [...this.owners.values()]) await this.serial(() => this.closeOwner(owner.project)).catch(() => {});
  }
  async finish(node: string, data: Record<string, unknown> = {}): Promise<void> {
    const session = this.sessions.get(node); if (!session) return;
    try { await this.record(node, "session_stopped", data); }
    finally {
      if (this.sessions.get(node)?.id === session.id) this.sessions.delete(node);
      if (![...this.sessions.values()].some(s => s.owner === session.owner)) await this.serial(() => this.closeOwner(session.project));
    }
  }
  async report(project: string, days?: number): Promise<any> {
    const binding = this.binding(project) || await this.inspect(project);
    await this.chain;
    return this.core.request("interactive.report", { database: binding.database, project, owner: this.owners.get(projectKey(project))?.id || this.owner, ...(days ? { days } : {}) });
  }
}

export function endpointProfiles(binding: Binding): Profile[] {
  return Object.entries(binding.endpoints || {}).filter(([, e]) => ["claude", "codex"].includes(e.adapter)).map(([id, e]) => {
    const args = e.command.slice(1);
    if (typeof e.model === "string" && !args.some((arg: string) => ["--model", "-m"].includes(arg))) args.push("--model", e.model);
    if (e.adapter === "codex" && !args.includes("--sandbox") && !args.includes("-s")) args.push("--sandbox", e.sandbox || "read-only");
    return {
    id: `core-${createHash("sha256").update(binding.config + id).digest("hex").slice(0, 12)}`, name: `${id} - existing configuration`, provider: e.adapter,
    command: e.command[0], args, shell: e.launch?.shell || "direct", shellPath: e.launch?.executable, profilePath: e.launch?.profile
  }; });
}
