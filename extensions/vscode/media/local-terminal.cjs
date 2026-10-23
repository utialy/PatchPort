"use strict";

// Human input stays on the PTY. Peer prompts and grants use the authenticated host channel.
const http = require("node:http");
const { randomUUID } = require("node:crypto");
const { StringDecoder } = require("node:string_decoder");
const { TextDecoder } = require("node:util");
const { createLocalClient, LocalApiError, LIMITS } = require("./local-api.cjs");

function localInvocation(executable, helper, runtime = process) {
  const path = require("node:path");
  const paths = runtime.platform === "win32" ? path.win32 : path.posix;
  const absolute = value => typeof value === "string" && paths.isAbsolute(value) &&
    (runtime.platform !== "win32" || /^(?:[a-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+[\\/])/i.test(value)) && !/[\x00-\x1f"]/.test(value);
  if (![executable, helper].every(absolute)) throw error("INVALID_HELPER_PATH");
  if (runtime.platform !== "win32" || !runtime.versions.electron) return { file: executable, args: [helper], env: {} };
  const systemRoot = runtime.env.SystemRoot || runtime.env.SYSTEMROOT || "C:\\Windows";
  if (!absolute(systemRoot)) throw error("INVALID_HELPER_PATH");
  // A fixed console parent is required for the Windows GUI executable's TTY handles.
  return { file: paths.join(systemRoot, "System32", "cmd.exe"),
    args: "/d /v:off /s /c \"\"%PATCHPORT_LOCAL_EXECUTABLE%\" \"%PATCHPORT_LOCAL_HELPER%\"\"",
    env: { PATCHPORT_LOCAL_EXECUTABLE: executable, PATCHPORT_LOCAL_HELPER: helper } };
}

function terminalStreams() {
  if (process.platform !== "win32" || !process.versions.electron || (process.stdin.isTTY && process.stdout.isTTY)) return { input: process.stdin, output: process.stdout, close() {} };
  const fs = require("node:fs"), tty = require("node:tty");
  let inputFd, outputFd, input, output;
  try {
    inputFd = fs.openSync("\\\\.\\CONIN$", "r+");
    outputFd = fs.openSync("\\\\.\\CONOUT$", "r+");
    input = new tty.ReadStream(inputFd); output = new tty.WriteStream(outputFd);
    const resized = () => output.emit("resize"); process.on("SIGWINCH", resized);
    return { input, output, close() { process.off("SIGWINCH", resized); input.destroy(); output.end(() => output.destroy()); } };
  } catch (failure) {
    if (input) input.destroy(); else if (inputFd !== undefined) fs.closeSync(inputFd);
    if (output) output.destroy(); else if (outputFd !== undefined) fs.closeSync(outputFd);
    throw failure;
  }
}

const FRAME_BYTES = 192 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
const safeText = value => String(value).replace(/\r\n?/g, "\n").replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, "");
const error = code => new LocalApiError(code);
const cellWidth = char => /\p{Mark}/u.test(char) || /[\u200d\ufe00-\ufe0f]/u.test(char) ? 0 :
  /[\u1100-\u115f\u2329\u232a\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6\u{1f000}-\u{1ffff}\u{20000}-\u{3ffff}]/u.test(char) ? 2 : 1;

function controlClient(endpoint, token, timeoutMs = 10000) {
  if (typeof endpoint !== "string" || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}\/events\/[a-zA-Z0-9_-]+$/.test(endpoint) ||
      typeof token !== "string" || token.length < 16 || token.length > 4096 || !/^[\x21-\x7e]+$/.test(token)) throw error("INVALID_CONTROL_CONFIG");
  const url = new URL(endpoint);
  if (Number(url.port) > 65535) throw error("INVALID_CONTROL_CONFIG");
  return payload => new Promise((resolve, reject) => {
    const body = JSON.stringify({ local: 1, ...payload });
    if (Buffer.byteLength(body) > FRAME_BYTES) { reject(error("CONTROL_FRAME_LIMIT")); return; }
    let settled = false, response, timer;
    const finish = (failure, result) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      if (failure) { response?.destroy(); req.destroy(); reject(failure); } else resolve(result);
    };
    const req = http.request(url, { method: "POST", agent: false, headers: {
      Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body)
    } }, res => {
      response = res;
      if (res.statusCode !== 200) { finish(error("CONTROL_REJECTED")); return; }
      const chunks = []; let bytes = 0;
      res.on("data", chunk => {
        bytes += chunk.length;
        if (bytes > FRAME_BYTES) finish(error("CONTROL_FRAME_LIMIT")); else chunks.push(chunk);
      });
      res.on("error", () => finish(error("CONTROL_LOST")));
      res.on("aborted", () => finish(error("CONTROL_LOST")));
      res.on("end", () => {
        if (settled) return;
        let result;
        try { result = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks))); }
        catch { finish(error("INVALID_CONTROL_RESPONSE")); return; }
        if (!record(result) || typeof result.ok !== "boolean") { finish(error("INVALID_CONTROL_RESPONSE")); return; }
        finish(null, result);
      });
    });
    req.on("error", () => finish(error("CONTROL_LOST")));
    timer = setTimeout(() => finish(error("CONTROL_TIMEOUT")), timeoutMs);
    req.end(body);
  });
}

class LineEditor {
  constructor({ onSubmit = () => {}, onChange = () => {}, onCancel = () => {}, onExit = () => {}, onNotice = () => {}, maxCharacters = 32000 } = {}) {
    this.text = ""; this.cursor = 0; this.version = 0; this.pending = ""; this.paste = false;
    this.decoder = new StringDecoder("utf8"); this.maxCharacters = maxCharacters;
    Object.assign(this, { onSubmit, onChange, onCancel, onExit, onNotice });
  }
  get dirty() { return this.text.length > 0 || this.paste; }
  changed() { this.version++; this.onChange(this); }
  insert(value) {
    const clean = safeText(value);
    if (!clean) return;
    if (this.text.length + clean.length > this.maxCharacters || Buffer.byteLength(this.text) + Buffer.byteLength(clean) > LIMITS.requestBytes) {
      this.onNotice("INPUT_LIMIT"); return;
    }
    this.text = this.text.slice(0, this.cursor) + clean + this.text.slice(this.cursor);
    this.cursor += clean.length; this.changed();
  }
  clear() { if (this.text || this.paste) { this.text = ""; this.cursor = 0; this.paste = false; this.changed(); } }
  left() { if (this.cursor) this.cursor -= /[\uDC00-\uDFFF]/.test(this.text[this.cursor - 1]) && this.cursor > 1 ? 2 : 1; this.onChange(this); }
  right() { if (this.cursor < this.text.length) this.cursor += this.text.codePointAt(this.cursor) > 0xffff ? 2 : 1; this.onChange(this); }
  feed(chunk) {
    this.pending += Buffer.isBuffer(chunk) ? this.decoder.write(chunk) : chunk;
    while (this.pending) {
      if (this.paste) {
        const end = this.pending.indexOf("\u001b[201~");
        if (end >= 0) {
          this.insert(this.pending.slice(0, end)); this.pending = this.pending.slice(end + 6);
          this.paste = false; this.changed(); continue;
        }
        let held = 0;
        for (let size = 1; size < 6; size++) if (this.pending.endsWith("\u001b[201~".slice(0, size))) held = size;
        this.insert(this.pending.slice(0, this.pending.length - held));
        this.pending = held ? this.pending.slice(-held) : ""; break;
      }
      if (this.pending[0] === "\u001b") {
        const sequences = ["\u001b[200~", "\u001b[D", "\u001b[C", "\u001b[H", "\u001b[F", "\u001b[1~", "\u001b[4~", "\u001b[3~", "\u001b[13;2u", "\u001b\r"];
        const sequence = sequences.find(value => this.pending.startsWith(value));
        if (sequence) {
          this.pending = this.pending.slice(sequence.length);
          if (sequence === "\u001b[200~") { this.paste = true; this.changed(); }
          else if (sequence === "\u001b[D") this.left();
          else if (sequence === "\u001b[C") this.right();
          else if (["\u001b[H", "\u001b[1~"].includes(sequence)) { this.cursor = 0; this.onChange(this); }
          else if (["\u001b[F", "\u001b[4~"].includes(sequence)) { this.cursor = this.text.length; this.onChange(this); }
          else if (sequence === "\u001b[3~") {
            const count = this.text.codePointAt(this.cursor) > 0xffff ? 2 : 1;
            this.text = this.text.slice(0, this.cursor) + this.text.slice(this.cursor + count); this.changed();
          } else this.insert("\n");
          continue;
        }
        if (sequences.some(value => value.startsWith(this.pending))) break;
        const escape = this.pending.match(/^\x1b(?:\[[0-9;?]*[ -/]*[@-~]|O.)/);
        if (escape) this.pending = this.pending.slice(escape[0].length);
        else if (/^\x1b\[[0-9;?]*$/.test(this.pending) && this.pending.length < 32) break;
        else this.pending = this.pending.slice(1);
        continue;
      }
      const char = String.fromCodePoint(this.pending.codePointAt(0)); this.pending = this.pending.slice(char.length);
      if (char === "\u0003") { this.onCancel(); this.clear(); }
      else if (char === "\u0004") { if (!this.dirty) this.onExit(); }
      else if (char === "\r") { if (this.text.trim()) this.onSubmit(this.text); }
      else if (char === "" || char === "\b") {
        const end = this.cursor; this.left(); this.text = this.text.slice(0, this.cursor) + this.text.slice(end); this.changed();
      } else if (char === "\u0001") { this.cursor = 0; this.onChange(this); }
      else if (char === "\u0005") { this.cursor = this.text.length; this.onChange(this); }
      else if (char === "\u0015") this.clear();
      else this.insert(char);
    }
  }
}

class LocalTerminal {
  constructor({ control, input = process.stdin, output = process.stdout, pollIntervalMs = 100, onExit = () => {} }) {
    Object.assign(this, { control, input, output, pollIntervalMs, onExit });
    this.identity = null; this.client = null; this.active = null; this.closed = false; this.failed = false;
    this.pollTimer = null; this.polling = false; this.draftTask = null; this.draftVersion = 0; this.seen = new Set(); this.promptShown = false;
    this.editor = new LineEditor({ onSubmit: text => this.submitHuman(text), onChange: () => this.draftChanged(),
      onCancel: () => this.cancel(), onExit: () => this.close(), onNotice: code => this.notice(code) });
    this.onInput = chunk => { if (!this.closed && !this.failed) this.editor.feed(chunk); };
    this.onEnd = () => this.close(); this.onResize = () => this.render();
  }
  write(value) { this.output.write(value); }
  notice(code) { this.write(`\r\n[${safeText(code)}]\r\n`); this.promptShown = false; this.render(); }
  render() {
    if (this.closed || this.active) return;
    // The compact draft display shows embedded line breaks without turning them into submissions.
    const shown = safeText(this.editor.text).replace(/\n/g, " \\n ");
    const before = safeText(this.editor.text.slice(0, this.editor.cursor)).replace(/\n/g, " \\n ");
    const columns = Math.max(20, this.output.getWindowSize?.()[0] || this.output.columns || 80), available = columns - 6;
    const points = Array.from(shown), caret = Array.from(before).length;
    let offset = caret, leftWidth = 0;
    while (offset > 0 && leftWidth + cellWidth(points[offset - 1]) < available) leftWidth += cellWidth(points[--offset]);
    let end = offset, visibleWidth = 0;
    while (end < points.length && visibleWidth + cellWidth(points[end]) <= available) visibleWidth += cellWidth(points[end++]);
    const visible = points.slice(offset, end).join("");
    this.write(`\r\x1b[2K> ${visible}`);
    // Redrawing only this line avoids moving through prior model output after terminal resize.
    const suffix = points.slice(caret, end).reduce((total, char) => total + cellWidth(char), 0);
    if (suffix > 0) this.write(`\x1b[${suffix}D`);
    this.promptShown = true;
  }
  async call(type, data = {}) {
    const result = await this.control({ type, ...(this.identity || {}), ...data });
    if (!record(result) || result.ok !== true) {
      const code = typeof result?.code === "string" && /^[A-Z0-9_]{1,80}$/.test(result.code) ? result.code : "CONTROL_REJECTED";
      throw error(code);
    }
    return result;
  }
  async start() {
    try {
      const boot = await this.call("bootstrap", { processId: process.pid });
      if (this.closed) return this;
      if (typeof boot.helperId !== "string" || !UUID.test(boot.helperId) || typeof boot.generation !== "string" || !UUID.test(boot.generation) ||
          typeof boot.coreEpoch !== "string" || !boot.coreEpoch || boot.coreEpoch.length > 128 || !record(boot.config)) throw error("INVALID_BOOTSTRAP");
      this.identity = { helperId: boot.helperId, generation: boot.generation, coreEpoch: boot.coreEpoch };
      this.client = createLocalClient(boot.config);
      this.requestTimeoutMs = boot.config.requestTimeoutMs ?? 300000;
      this.write(`PatchPort Local chat | ${safeText(boot.config.backend)} | ${safeText(boot.config.modelId)}\r\n`);
      this.write("Conversation only. Enter sends; Ctrl+J adds a line; Ctrl+C cancels; /exit closes.\r\n");
      this.input.setRawMode?.(true); this.input.resume?.(); this.write("\u001b[?2004h");
      this.input.on("data", this.onInput); this.input.on("end", this.onEnd); this.output.on?.("resize", this.onResize);
      await this.call("draft", { dirty: false });
      if (this.closed) return this;
      this.render();
      this.pollTimer = setInterval(() => this.poll(), this.pollIntervalMs);
      await this.poll();
    } catch { this.fail(); }
    return this;
  }
  draftChanged() {
    this.render(); this.draftVersion++;
    if (!this.identity || this.closed || this.failed || this.draftTask) return;
    this.draftTask = (async () => {
      let sent;
      do { sent = this.draftVersion; await this.call("draft", { dirty: this.editor.dirty }); }
      while (!this.closed && sent !== this.draftVersion);
    })().catch(() => this.fail()).finally(() => { this.draftTask = null; });
  }
  async poll() {
    if (this.polling || this.closed || this.failed) return;
    this.polling = true;
    try {
      const result = await this.call("poll", { ready: !this.active && !this.editor.dirty, dirty: this.editor.dirty, requestId: this.active?.requestId ?? null });
      if (!Array.isArray(result.commands) || result.commands.length > 16) throw error("INVALID_COMMANDS");
      for (const command of result.commands) {
        if (!record(command)) throw error("INVALID_COMMAND");
        if (command.type === "stop") { this.close(); break; }
        if (command.type === "cancel") { if (command.requestId === this.active?.requestId) this.cancel(); continue; }
        if (command.type !== "submit" || !UUID.test(command.requestId) || typeof command.prompt !== "string" || !command.prompt.trim() ||
            command.prompt.length > 32000 || typeof command.deliveryId !== "string" || !command.deliveryId) throw error("INVALID_COMMAND");
        if (this.seen.has(command.requestId)) continue;
        if (this.active || this.editor.dirty) {
          await this.call("reject", { requestId: command.requestId, deliveryId: command.deliveryId, code: this.active ? "REQUEST_BUSY" : "DRAFT_BUSY" });
          continue;
        }
        this.runRequest(command.requestId, command.prompt, command.deliveryId);
      }
    } catch { this.fail(); } finally { this.polling = false; }
  }
  submitHuman(prompt) {
    if (this.active) { this.notice("REQUEST_BUSY"); return; }
    if (prompt.trim() === "/exit") { this.close(); return; }
    this.editor.clear(); this.runRequest(randomUUID(), prompt, null);
  }
  async runRequest(requestId, prompt, deliveryId) {
    if (this.closed || this.failed || this.active || this.seen.has(requestId)) return;
    this.seen.add(requestId);
    const active = { requestId, deliveryId, controller: new AbortController(), cancelled: false };
    this.active = active;
    const version = this.editor.version;
    this.write(`\r\x1b[2K${deliveryId ? "Peer" : "You"}: ${safeText(prompt).replace(/\n/g, "\r\n")}\r\nAssistant: `);
    let result;
    try {
      const deadline = Date.now() + this.requestTimeoutMs;
      let prepared;
      while (true) {
        prepared = await this.call("prepare", { requestId, prompt, deliveryId });
        if (prepared.waiting !== true) break;
        if (!active.cancelled && Date.now() >= deadline) this.cancel("QUEUE_TIMEOUT");
        if (active.cancelled || this.closed) {
          await active.cancelTask;
          if (!this.closed) this.write("\r\n[CANCELLED before dispatch]\r\n");
          return;
        }
        await new Promise(resolve => {
          const done = () => { clearTimeout(timer); active.controller.signal.removeEventListener("abort", done); resolve(); };
          const timer = setTimeout(done, this.pollIntervalMs); active.controller.signal.addEventListener("abort", done, { once: true });
        });
        if (active.cancelled || this.closed) {
          await active.cancelTask;
          if (!this.closed) this.write("\r\n[CANCELLED before dispatch]\r\n");
          return;
        }
      }
      if (!Array.isArray(prepared.messages) || prepared.messages.at(-1)?.role !== "user" || prepared.messages.at(-1)?.content !== prompt) throw error("INVALID_CONTEXT");
      result = await this.client.generate({ requestId, messages: prepared.messages, signal: active.controller.signal,
        onText: text => { if (!this.closed && !active.cancelled) this.write(safeText(text).replace(/\n/g, "\r\n")); },
        authorize: async binding => {
          if (this.closed || this.failed || active.cancelled || this.editor.dirty || this.editor.version !== version) throw error("AUTHORIZATION_DENIED");
          let authorized;
          try { authorized = await this.call("authorize", { requestId, prompt, deliveryId, binding }); }
          catch (failure) { if (failure.code !== "AUTHORIZATION_DENIED") this.fail(); throw failure; }
          if (this.closed || this.failed || active.cancelled || this.editor.dirty || this.editor.version !== version) throw error("AUTHORIZATION_DENIED");
          return authorized.grant;
        } });
      if (!this.closed && !this.failed) {
        const finished = await this.call("finish", { requestId, deliveryId, result });
        if (!["COMPLETED", "FAILED", "TRUNCATED", "CANCELLED", "UNCERTAIN"].includes(finished.state)) throw error("INVALID_FINISH");
        this.write(`\r\n[${finished.state}${result.serverCancelUnconfirmed ? "; server cancellation unconfirmed" : ""}]\r\n`);
      }
    } catch { if (!this.closed) this.fail(); }
    finally { if (this.active === active) this.active = null; this.render(); }
    return result;
  }
  cancel(reason) {
    const active = this.active;
    if (!active || active.cancelled) return;
    active.cancelled = true; active.controller.abort(error("CANCELLED"));
    active.cancelTask = this.call("cancel", { requestId: active.requestId, ...(reason ? { reason } : {}) }).catch(() => this.fail());
  }
  fail() {
    if (this.closed) return;
    this.failed = true; this.active?.controller.abort(error("CONNECTION_LOST"));
    this.write("\r\n[Control or request state could not be confirmed. This session is closed; no automatic retry.]\r\n");
    this.close(1);
  }
  close(code = 0) {
    if (this.closed) return;
    this.closed = true; clearInterval(this.pollTimer);
    this.active?.controller.abort(error("CANCELLED"));
    this.input.off("data", this.onInput); this.input.off("end", this.onEnd); this.output.off?.("resize", this.onResize);
    try { this.input.setRawMode?.(false); } catch { /* The PTY may already have closed. */ }
    this.input.pause?.(); this.write("\u001b[?2004l\r\n"); this.onExit(code);
  }
}

async function main() {
  let terminal, streams;
  try {
    const control = controlClient(process.env.PATCHPORT_ENDPOINT, process.env.PATCHPORT_TOKEN);
    delete process.env.PATCHPORT_TOKEN; delete process.env.PATCHPORT_ENDPOINT;
    delete process.env.PATCHPORT_LOCAL_EXECUTABLE; delete process.env.PATCHPORT_LOCAL_HELPER;
    streams = terminalStreams();
    terminal = new LocalTerminal({ control, input: streams.input, output: streams.output,
      onExit: code => { streams.close(); process.exitCode = code; } });
    process.on("SIGTERM", () => terminal.close()); process.on("SIGHUP", () => terminal.close());
    await terminal.start();
  } catch { streams?.close(); process.stderr.write("PatchPort local helper could not start.\n"); process.exitCode = 1; }
}

module.exports = { LineEditor, LocalTerminal, controlClient, safeText, localInvocation };
if (require.main === module) main();
