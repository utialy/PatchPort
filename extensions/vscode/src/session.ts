import * as pty from "node-pty";
import { createServer, Server } from "node:http";
import { randomBytes, createHash, timingSafeEqual } from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { EventEmitter } from "node:events";
import { Terminal as HeadlessTerminal, ITerminalAddon } from "@xterm/headless";
import { SerializeAddon } from "@xterm/addon-serialize";
import { NodeConfig, Profile, Router, cleanMessage, inputIntent, MAX_TEXT } from "./model";
import { Ledger } from "./core";
import { Sample, claudeUsage, codexUsage, statusCost, unknownMetrics } from "./providerUsage";

export interface Running { process: pty.IPty; token: string; node: NodeConfig; profile: Profile; output: string; screen: HeadlessTerminal; serializer: SerializeAddon; sequence: number; turn: number; eventSession?: string; disposed: boolean; pasteTimer?: NodeJS.Timeout; startedAt: number; usageSequence: number; samples: Map<string, Sample>; transcript?: string; codexHome?: string; costBaseline: number | null; lastCost: number | null; cost: number | null; usageKey?: string; seen: Set<string> }
const psQuote = (s: string) => `'${s.replace(/'/g, "''")}'`;
const shQuote = (s: string) => `'${s.replace(/'/g, "'\\''")}'`;

export function launch(profile: Profile, args: string[], cwd: string): { file: string; args: string[] } {
  if (profile.shell === "direct") {
    if (process.platform === "win32" && /\.(cmd|bat|ps1)$/i.test(profile.command)) throw new Error("Run scripts and aliases through an explicit PowerShell profile.");
    return { file: profile.command, args };
  }
  if (profile.shell === "powershell" || profile.shell === "pwsh") {
    const file = profile.shellPath || (profile.shell === "powershell" ? path.join(process.env.SystemRoot || "C:\\Windows", "System32/WindowsPowerShell/v1.0/powershell.exe") : "pwsh.exe");
    let script = "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[Console]::OutputEncoding=$OutputEncoding=[System.Text.UTF8Encoding]::new($false); try { ";
    if (profile.profilePath) script += `. ${psQuote(profile.profilePath)}; `;
    script += `Set-Location -LiteralPath ${psQuote(cwd)}; $global:LASTEXITCODE=0; $patchportArgs=@(${args.map(psQuote).join(",")}); `;
    // Windows PowerShell's legacy native argument binder consumes unescaped quotes.
    script += "if ($PSVersionTable.PSVersion.Major -lt 7) { $patchportArgs=@($patchportArgs | ForEach-Object { $_.Replace('\"','\\\"') }) }; ";
    script += `& ${psQuote(profile.command)} @patchportArgs; if (-not $?) { exit 1 }; exit $global:LASTEXITCODE } catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }`;
    return { file, args: ["-NoLogo", ...(profile.profilePath ? ["-NoProfile"] : []), "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")] };
  }
  let script = profile.shell === "bash" ? "shopt -s expand_aliases; " : "";
  if (profile.profilePath) script += `. ${shQuote(profile.profilePath)} || exit $?; `;
  script += `cd -- ${shQuote(cwd)} || exit $?; eval ${shQuote([profile.command, ...args].map(shQuote).join(" "))}; exit $?`;
  return { file: profile.shellPath || profile.shell, args: profile.profilePath ? [...(profile.shell === "bash" ? ["--noprofile", "--norc"] : ["-f"]), "-c", script] : ["-lic", script] };
}

export class SessionHost extends EventEmitter {
  readonly sessions = new Map<string, Running>();
  private server?: Server;
  private port = 0;
  private starting = new Set<string>();
  private closed = false;
  private cancelledStarts = new Set<string>();
  private initializing?: Promise<void>;
  private sizes = new Map<string, { cols: number; rows: number }>();
  constructor(readonly router: Router, readonly storage: string, readonly extensionPath: string, readonly ledger?: Ledger) { super(); }
  async initialize(): Promise<void> {
    if (!this.initializing) this.initializing = this.openServer().catch(error => { this.server?.close(() => {}); this.server = undefined; this.initializing = undefined; throw error; });
    await this.initializing;
  }
  private async openServer(): Promise<void> {
    await fs.mkdir(this.storage, { recursive: true });
    this.server = createServer((req, res) => {
      res.setHeader("Content-Type", "application/json");
      const id = req.url?.match(/^\/events\/([a-zA-Z0-9-]+)$/)?.[1];
      const session = id ? this.sessions.get(id) : undefined;
      const expected = session ? `Bearer ${session.token}` : "";
      const supplied = req.headers.authorization || "";
      if (req.method !== "POST" || req.headers.origin || !session || supplied.length !== expected.length || !timingSafeEqual(Buffer.from(supplied), Buffer.from(expected))) { res.writeHead(403); res.end("{}"); return; }
      let size = 0; const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => { size += chunk.length; if (size > 256 * 1024) { res.writeHead(413); res.end("{}"); req.destroy(); } else chunks.push(chunk); });
      req.on("end", async () => {
        if (size > 256 * 1024) return;
        try { const response = await this.event(session, JSON.parse(Buffer.concat(chunks).toString("utf8"))); res.end(JSON.stringify(response || {})); }
        catch { res.writeHead(400); res.end("{}"); }
      });
      req.on("error", () => { if (!res.writableEnded) res.end("{}"); });
    });
    this.server.requestTimeout = 5000;
    await new Promise<void>((resolve, reject) => { this.server!.once("error", reject); this.server!.listen(0, "127.0.0.1", () => resolve()); });
    this.port = (this.server.address() as { port: number }).port;
  }
  async start(node: NodeConfig, profile: Profile): Promise<void> {
    if (this.closed || this.sessions.has(node.id) || this.starting.has(node.id)) throw new Error("Already running, or the extension is shutting down.");
    if (this.sessions.size + this.starting.size >= 12) throw new Error("At most 12 managed terminals can run concurrently.");
    this.starting.add(node.id);
    this.cancelledStarts.delete(node.id);
    this.router.started(node.id);
    try {
      await this.initialize();
      if (!(await fs.stat(node.project)).isDirectory()) throw new Error("The project folder is missing.");
      if (profile.profilePath && !(await fs.stat(profile.profilePath)).isFile()) throw new Error("The profile file is missing.");
      if (this.ledger) await this.ledger.start(node);
      const token = randomBytes(32).toString("hex");
      const endpoint = `http://127.0.0.1:${this.port}/events/${node.id}`;
      const args = [...profile.args];
      if (profile.provider === "claude") {
        if (args.some(a => ["--settings", "-p", "--print"].includes(a))) throw new Error("Managed Claude profiles are interactive. PatchPort owns --settings for its hooks.");
        const hooks: Record<string, unknown> = {};
        for (const event of ["SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "PermissionRequest", "PostToolUse", "Notification", "SessionEnd"]) hooks[event] = [{ hooks: [{ type: "http", url: endpoint, headers: { Authorization: "Bearer $PATCHPORT_TOKEN" }, allowedEnvVars: ["PATCHPORT_TOKEN"], timeout: 3 }] }];
        const settings = path.join(this.storage, `${node.id}-claude.json`);
        const statusCommand = process.platform === "win32"
          ? `powershell.exe -NoLogo -NoProfile -File \"${path.join(this.extensionPath, "media/statusline.ps1")}\"`
          : `/usr/bin/env ELECTRON_RUN_AS_NODE=1 ${shQuote(process.execPath)} ${shQuote(path.join(this.extensionPath, "media/statusline.cjs"))}`;
        await fs.writeFile(settings, JSON.stringify({ hooks, ...(this.ledger ? { statusLine: { type: "command", command: statusCommand } } : {}) }), { mode: 0o600 });
        args.push("--settings", settings);
      } else if (profile.provider === "codex") {
        const notifier = process.platform === "win32"
          ? [path.join(process.env.SystemRoot || "C:\\Windows", "System32/WindowsPowerShell/v1.0/powershell.exe"), "-NoLogo", "-NoProfile", "-File", path.join(this.extensionPath, "media/notify.ps1")]
          : ["/usr/bin/env", "ELECTRON_RUN_AS_NODE=1", process.execPath, path.join(this.extensionPath, "media/notify.cjs")];
        args.push("-c", `notify=${JSON.stringify(notifier)}`, "--no-daemon");
      }
      const invocation = launch(profile, args, node.project);
      const env: Record<string, string> = {};
      for (const [key, value] of Object.entries(process.env)) if (value !== undefined && !["ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "VSCODE_INSPECTOR_OPTIONS"].includes(key)) env[key] = value;
      Object.assign(env, { TERM: "xterm-256color", COLORTERM: "truecolor", PATCHPORT_TOKEN: token, PATCHPORT_ENDPOINT: endpoint });
      if (this.closed || this.cancelledStarts.has(node.id)) throw new Error("Terminal start cancelled.");
      const size = this.sizes.get(node.id) || { cols: 90, rows: 22 };
      const child = pty.spawn(invocation.file, invocation.args, { name: "xterm-256color", ...size, cwd: node.project, env, useConpty: true });
      const screen = new HeadlessTerminal({ ...size, scrollback: 2000, allowProposedApi: true });
      const serializer = new SerializeAddon(); screen.loadAddon(serializer as unknown as ITerminalAddon);
      const session: Running = { process: child, token, node, profile, output: "", screen, serializer, sequence: 0, turn: 0, disposed: false, startedAt: Date.now(), usageSequence: 0, samples: new Map(), costBaseline: 0, lastCost: null, cost: null, seen: new Set() };
      this.sessions.set(node.id, session);
      screen.onData(data => { if (!session.disposed) { try { child.write(data); } catch { /* Process exit can race a terminal status query. */ } } });
      child.onData(data => {
        if (this.sessions.get(node.id) !== session) return;
        session.output = (session.output + data).slice(-256 * 1024);
        const sequence = ++session.sequence;
        session.screen.write(data, () => { if (!session.disposed) this.emit("output", node.id, data, sequence); });
      });
      child.onExit(({ exitCode }) => {
        session.disposed = true; if (session.pasteTimer) clearTimeout(session.pasteTimer);
        if (this.sessions.get(node.id) !== session) return;
        this.sessions.delete(node.id);
        session.screen.dispose();
        this.router.stopped(node.id);
        void this.ledger?.finish(node.id, { exitCode }).catch(() => this.router.note("Session-end recording failed; no automatic resend."));
        this.router.note(`${node.name}: terminal exited (${exitCode}).`);
        this.emit("output", node.id, `\r\n\u001b[90m[PatchPort: process exited ${exitCode}]\u001b[0m\r\n`, ++session.sequence);
      });
      if (profile.provider === "terminal") this.router.setPhase(node.id, "ready", "Ordinary terminal; no automatic AI routing");
      this.emit("started", node.id);
      await this.ledger?.record(node.id, "session_started", { provider: profile.provider });
    } catch (error) { this.router.setPhase(node.id, "error", String(error)); await this.ledger?.record(node.id, "failed", { stage: "launch" }).catch(() => {}); await this.ledger?.finish(node.id, { stage: "launch_failed" }).catch(() => {}); throw error; }
    finally { this.starting.delete(node.id); }
  }
  private async event(session: Running, payload: unknown): Promise<Record<string, unknown> | void> {
    if (session.disposed || !payload || typeof payload !== "object") return;
    const p = payload as Record<string, unknown>;
    const id = session.node.id;
    if (typeof p.cwd === "string" && path.resolve(p.cwd).toLowerCase() !== path.resolve(session.node.project).toLowerCase()) { this.router.setPhase(id, "attention", "Working directory changed; automatic delivery stopped."); return; }
    if (session.profile.provider === "claude") {
      if (typeof p.session_id !== "string") throw new Error("Missing session identity");
      if (session.eventSession && session.eventSession !== p.session_id) {
        if (p.hook_event_name !== "SessionStart") return;
        this.router.started(id);
      }
      session.eventSession = p.session_id;
      if (typeof p.transcript_path === "string") session.transcript = p.transcript_path;
      if (p.patchport_statusline === true) {
        const nextCost = (p.cost as any)?.total_cost_usd;
        if (session.lastCost !== null && typeof nextCost === "number" && nextCost < session.lastCost) session.costBaseline = null;
        session.cost = statusCost(p, session.costBaseline, session.lastCost);
        const raw = (p.cost as any)?.total_cost_usd; if (typeof raw === "number" && Number.isFinite(raw) && raw >= 0) session.lastCost = raw;
        await this.collectUsage(session); return;
      }
      switch (p.hook_event_name) {
        case "SessionStart": session.costBaseline = p.source === "resume" ? null : 0; session.lastCost = null; session.cost = null; await this.ledger?.record(id, "session_started", { provider_session: p.session_id, source: p.source || "unknown" }); this.router.ready(id); return { hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: this.router.connectionContext(id) } };
        case "UserPromptSubmit": session.turn++; await this.ledger?.record(id, "accepted", { provider_session: p.session_id, prompt: typeof p.prompt === "string" ? cleanMessage(p.prompt).slice(0, MAX_TEXT) : null }, String(session.turn)); this.router.acceptedPrompt(id); return { hookSpecificOutput: { hookEventName: "UserPromptSubmit", additionalContext: this.router.connectionContext(id) } };
        case "PermissionRequest": this.router.setPhase(id, "attention", "Check the permission prompt in the terminal."); break;
        case "PostToolUse": this.router.setPhase(id, "busy"); break;
        case "Notification": if (p.notification_type === "permission_prompt" || p.notification_type === "elicitation_dialog") this.router.setPhase(id, "attention", "The terminal is waiting for input."); break;
        case "StopFailure": await this.ledger?.record(id, "failed", { provider_session: p.session_id }, String(session.turn)); await this.collectUsage(session); this.router.setPhase(id, "error", "AI response error; no automatic retry."); break;
        case "SessionEnd": await this.collectUsage(session); this.router.setPhase(id, "attention", "The AI session ended."); break;
        case "Stop":
          if (p.stop_hook_active || (Array.isArray(p.background_tasks) && p.background_tasks.length) || (Array.isArray(p.session_crons) && p.session_crons.length)) { this.router.setPhase(id, "attention", "Background or continuing work; automatic delivery waits"); break; }
          if (typeof p.last_assistant_message === "string") {
            const key = createHash("sha256").update(`${p.session_id}:${session.turn}:${p.last_assistant_message}`).digest("hex");
            if (session.seen.has(key)) break;
            await this.ledger?.record(id, "completed", { answer: cleanMessage(p.last_assistant_message).slice(0, MAX_TEXT), provider_session: p.session_id }, String(session.turn), key);
            session.seen.add(key); await this.collectUsage(session);
            this.router.complete(id, p.last_assistant_message, `${p.session_id}:${session.turn}:${key}`);
          }
          break;
      }
    } else if (session.profile.provider === "codex" && p.type === "agent-turn-complete") {
      if (typeof p["thread-id"] !== "string" || typeof p["turn-id"] !== "string" || typeof p["last-assistant-message"] !== "string") throw new Error("Incomplete Codex notification");
      if (session.eventSession && session.eventSession !== p["thread-id"]) { session.eventSession = p["thread-id"]; this.router.started(id); this.router.setPhase(id, "attention", "AI conversation changed. Confirm readiness again."); return; }
      session.eventSession = p["thread-id"];
      const key = createHash("sha256").update(`${p["thread-id"]}:${p["turn-id"]}`).digest("hex");
      if (session.seen.has(key)) return;
      await this.ledger?.record(id, "completed", { answer: cleanMessage(p["last-assistant-message"]).slice(0, MAX_TEXT), provider_session: p["thread-id"], prompt: Array.isArray(p["input-messages"]) ? cleanMessage(p["input-messages"].filter(x => typeof x === "string").join("\n")).slice(0, MAX_TEXT) : null }, p["turn-id"], key);
      session.seen.add(key);
      if (typeof p.patchport_codex_home === "string") session.codexHome = p.patchport_codex_home;
      await this.collectUsage(session);
      this.router.complete(id, p["last-assistant-message"], `${p["thread-id"]}:${p["turn-id"]}`);
    }
  }
  private async collectUsage(session: Running): Promise<void> {
    if (!this.ledger || !session.eventSession) return;
    let sample: Sample = { metrics: unknownMetrics(), model: "UNKNOWN", source: "UNKNOWN", coverage: "provider usage unavailable" };
    try {
      if (session.profile.provider === "claude" && session.transcript) sample = await claudeUsage(session.transcript, session.eventSession, session.startedAt);
      if (session.profile.provider === "codex" && session.codexHome) sample = await codexUsage(session.codexHome, session.eventSession, session.node.project, session.startedAt);
    } catch { sample.coverage = "usage missing or unsupported; not zero"; }
    if (session.profile.provider === "claude") sample.metrics.cost_usd = session.cost;
    session.samples.set(session.eventSession, sample);
    const parts = [...session.samples.values()], metrics = unknownMetrics();
    for (const key of Object.keys(metrics) as (keyof typeof metrics)[]) {
      const values = parts.map(p => p.metrics[key]); metrics[key] = values.every(v => v !== null) ? values.reduce<number>((sum, v) => sum + v!, 0) : null;
    }
    const data = { metrics, model: new Set(parts.map(p => p.model)).size === 1 ? sample.model : "MULTIPLE", source: sample.source, coverage: sample.coverage };
    const key = JSON.stringify(data); if (key === session.usageKey) return;
    session.usageKey = key;
    await this.ledger.record(session.node.id, "usage", { ...data, sequence: ++session.usageSequence });
  }
  activity(id: string, kind: "edit" | "submit" | "interrupt"): void {
    const session = this.sessions.get(id); if (!session || session.disposed) return;
    if (session.pasteTimer) { clearTimeout(session.pasteTimer); session.pasteTimer = undefined; this.router.setPhase(id, "attention", "Human input interrupted automatic delivery. Inspect the input."); }
    this.router.input(id, kind === "submit" ? "\r" : kind === "interrupt" ? "\u0003" : "x");
    if (kind !== "edit") void this.ledger?.record(id, kind === "submit" ? "submitted" : "interrupted", { origin: "human" }).catch(() => { this.router.setPhase(id, "error", "Ledger recording failed"); });
  }
  input(id: string, data: string, human = true): void {
    const session = this.sessions.get(id); if (!session || session.disposed || typeof data !== "string" || data.length > 64 * 1024) return;
    // The host emulator answers status/color queries once, including while the board is closed.
    if (/^(?:\x1b\[[?>]?[0-9;]*[Rcnty]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1bP.*\x1b\\)+$/su.test(data)) return;
    if (!human || inputIntent(data) === "protocol") { session.process.write(data); return; }
    if (session.pasteTimer) { clearTimeout(session.pasteTimer); session.pasteTimer = undefined; this.router.setPhase(id, "attention", "Human input interrupted automatic delivery. Inspect the input."); }
    this.router.input(id, data); session.process.write(data);
    const intent = inputIntent(data); if (intent === "submit" || intent === "interrupt") void this.ledger?.record(id, intent === "submit" ? "submitted" : "interrupted", { origin: "human" }).catch(() => this.router.setPhase(id, "error", "Ledger recording failed"));
  }
  send(id: string, text: string): void {
    const session = this.sessions.get(id);
    if (!session || session.disposed || session.profile.provider === "terminal") throw new Error("The AI session is unavailable.");
    const safe = cleanMessage(text);
    if (!safe || safe.length > MAX_TEXT + 5000) throw new Error("Check the delivery message size.");
    session.process.write(`\u001b[200~${safe}\u001b[201~`);
    // Keep submission outside the CLI's paste-burst detection window.
    session.pasteTimer = setTimeout(() => { session.pasteTimer = undefined; if (!session.disposed) { try { session.process.write("\r"); } catch { this.router.setPhase(id, "attention", "Delivery outcome uncertain"); } } }, 750);
  }
  snapshot(id: string): Promise<{ data: string; sequence: number; cols: number; rows: number } | undefined> {
    const session = this.sessions.get(id); if (!session || session.disposed) return Promise.resolve(undefined);
    const sequence = session.sequence;
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(undefined), 1500);
      session.screen.write("", () => {
        clearTimeout(timer);
        if (session.disposed) { resolve(undefined); return; }
        resolve({ data: session.serializer.serialize({ scrollback: 1000 }), sequence, cols: session.screen.cols, rows: session.screen.rows });
      });
    });
  }
  resize(id: string, cols: number, rows: number): void {
    if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 10 || cols > 400 || rows < 3 || rows > 150) return;
    this.sizes.set(id, { cols, rows });
    const session = this.sessions.get(id);
    if (session && !session.disposed) { try { session.screen.resize(cols, rows); session.process.resize(cols, rows); } catch { /* Exiting terminals may reject resize. */ } }
  }
  isActive(id: string): boolean { return this.sessions.has(id) || this.starting.has(id); }
  stop(id: string): void { if (this.starting.has(id)) this.cancelledStarts.add(id); const session = this.sessions.get(id); this.router.stopped(id); if (!session) return; session.disposed = true; if (session.pasteTimer) clearTimeout(session.pasteTimer); try { session.process.kill(); } catch { this.router.note(`${session.node.name}: inspect exit status.`); } session.screen.dispose(); this.sessions.delete(id); void this.ledger?.finish(id).catch(() => this.router.note("Session-end outcome uncertain")); }
  dispose(): void { this.closed = true; for (const id of [...this.sessions.keys(), ...this.starting]) this.stop(id); this.server?.close(); this.server?.closeAllConnections(); }
}
