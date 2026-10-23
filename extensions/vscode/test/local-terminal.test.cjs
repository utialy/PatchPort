"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { once } = require("node:events");
const { PassThrough } = require("node:stream");
const { LineEditor, LocalTerminal, controlClient, safeText } = require("../media/local-terminal.cjs");

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const waitFor = async (check, timeout = 6000) => {
  const end = Date.now() + timeout;
  while (!check()) { if (Date.now() >= end) throw Error("Local terminal fixture timed out"); await pause(10); }
};
const frame = content => JSON.stringify({ model: "fixture-model", message: { role: "assistant", content }, done: true, done_reason: "stop" }) + "\n";

async function server(t, handler) {
  const instance = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => Promise.resolve(handler(req, res, Buffer.concat(chunks).toString("utf8"))).catch(() => res.destroy()));
  });
  instance.listen(0, "127.0.0.1"); await once(instance, "listening");
  t.after(async () => { instance.closeAllConnections(); await new Promise(resolve => instance.close(resolve)); });
  return { instance, origin: `http://127.0.0.1:${instance.address().port}` };
}

async function fixture(t, { apiHandler, controlHandler, config = {}, start = true } = {}) {
  const calls = [], posts = [], commands = [], history = [], output = [];
  const identity = { helperId: randomUUID(), generation: randomUUID(), coreEpoch: "epoch-1" };
  const token = randomUUID(), modelToken = "private-model-token";
  const api = await server(t, async (req, res, raw) => {
    posts.push({ path: req.url, headers: req.headers, body: JSON.parse(raw) });
    if (apiHandler) return apiHandler(req, res, raw);
    res.setHeader("Content-Type", "application/x-ndjson"); res.end(frame("Mock completed answer"));
  });
  const profile = { backend: "ollama", endpointOrigin: api.origin, modelId: "fixture-model", contextWindow: 8192,
    maxOutputTokens: 512, requestTimeoutMs: 3000, idleTimeoutMs: 2000, executionLocationConfirmed: true, ...config };
  const host = await server(t, async (req, res, raw) => {
    assert.equal(req.headers.authorization, `Bearer ${token}`);
    assert.equal(req.url, "/events/node-fixture");
    const message = JSON.parse(raw); calls.push(message);
    assert.equal(message.local, 1);
    if (message.type !== "bootstrap") for (const [key, value] of Object.entries(identity)) assert.equal(message[key], value);
    let reply;
    if (controlHandler) reply = await controlHandler(message, res, { calls, posts, commands, profile });
    if (res.destroyed || res.writableEnded) return;
    if (reply === undefined) {
      if (message.type === "bootstrap") reply = { ...identity, config: profile };
      else if (message.type === "poll") reply = { commands: commands.splice(0) };
      else if (message.type === "prepare") reply = { messages: [...history, { role: "user", content: message.prompt }] };
      else if (message.type === "authorize") reply = { grant: randomUUID() };
      else if (message.type === "finish") {
        reply = { state: message.result.state };
        if (message.result.state === "COMPLETED") {
          const prepared = calls.find(item => item.type === "prepare" && item.requestId === message.requestId);
          history.push({ role: "user", content: prepared.prompt }, { role: "assistant", content: message.result.content });
        }
      } else reply = {};
    }
    res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify({ ok: true, ...reply }));
  });
  const input = new PassThrough(), stdout = new PassThrough(); stdout.columns = 80;
  stdout.on("data", value => output.push(value.toString()));
  const exits = [];
  const control = controlClient(`${host.origin}/events/node-fixture`, token, 2000);
  const terminal = new LocalTerminal({ control, input, output: stdout, pollIntervalMs: 20, onExit: code => exits.push(code) });
  t.after(() => { terminal.close(); input.destroy(); stdout.destroy(); });
  if (start) await terminal.start();
  return { terminal, input, output, exits, calls, posts, commands, profile, identity, host, token, modelToken };
}

test("raw editor preserves split UTF-8, multiline bracketed paste, cursor edits and explicit Enter", () => {
  const submissions = [], changes = [];
  const editor = new LineEditor({ onSubmit: text => submissions.push(text), onChange: value => changes.push(value.dirty) });
  const pasted = Buffer.from("\u001b[200~\uD55C\uAE00 \uD83D\uDE42\nsecond line\u001b[201~");
  for (const byte of pasted) editor.feed(Buffer.from([byte]));
  assert.equal(editor.text, "\uD55C\uAE00 \uD83D\uDE42\nsecond line"); assert.equal(submissions.length, 0); assert.equal(editor.dirty, true);
  editor.feed("\u001b[D!\u001b[F");
  assert.equal(editor.text, "\uD55C\uAE00 \uD83D\uDE42\nsecond li!e");
  editor.feed("\r"); assert.deepEqual(submissions, ["\uD55C\uAE00 \uD83D\uDE42\nsecond li!e"]);
  assert.ok(changes.includes(true));
  editor.clear(); editor.feed("a\nb\u001b\rc\r"); assert.equal(submissions[1], "a\nb\nc");
});

test("raw editor bounds input, treats pasted controls as data and deletes Unicode code points", () => {
  let cancelled = 0, exited = 0;
  const editor = new LineEditor({ maxCharacters: 8, onCancel: () => cancelled++, onExit: () => exited++ });
  editor.feed("\uD83D\uDE42"); assert.equal(editor.text, "");
  editor.feed("\u001b[200~/exit\r\u0003\u0004\u001b[201~");
  assert.equal(editor.text, "/exit\n"); assert.equal(cancelled, 0); assert.equal(exited, 0);
  editor.feed("too long"); assert.ok(editor.text.length <= 8);
  editor.feed("\u0003"); assert.equal(cancelled, 1); assert.equal(editor.text, "");
  editor.feed("\u0004"); assert.equal(exited, 1);
});

test("untrusted output never emits ANSI, OSC, C1 or bidi controls", () => {
  assert.equal(safeText("\u001b[2J\u001b]52;c;secret\u0007\u009B31m\u202Esecret\rvalue"), "[2J]52;c;secret31msecret\nvalue");
});

for (const phase of ["bootstrap", "draft"]) test(`closing while ${phase} is pending cannot recreate input listeners or a polling timer`, async t => {
  const input = new PassThrough(), output = new PassThrough(), calls = [], exits = [];
  let release, entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const boot = { helperId: randomUUID(), generation: randomUUID(), coreEpoch: "epoch-1",
    config: { backend: "ollama", endpointOrigin: "http://127.0.0.1:11434", modelId: "fixture-model",
      contextWindow: 8192, maxOutputTokens: 128, executionLocationConfirmed: true } };
  const terminal = new LocalTerminal({ input, output, onExit: code => exits.push(code), control: async payload => {
    calls.push(payload.type);
    if (payload.type === phase) { entered(); await new Promise(resolve => { release = resolve; }); }
    return { ok: true, ...(payload.type === "bootstrap" ? boot : {}) };
  } });
  t.after(() => { release?.(); terminal.close(); input.destroy(); output.destroy(); });
  const started = terminal.start(); await waiting;
  terminal.close(); release(); await started;
  assert.deepEqual(calls, phase === "bootstrap" ? ["bootstrap"] : ["bootstrap", "draft"]);
  assert.equal(terminal.closed, true); assert.equal(terminal.pollTimer, null);
  assert.equal(input.listenerCount("data"), 0); assert.deepEqual(exits, [0]);
});

test("helper startup and draft/paste editing make no inference request", async t => {
  const f = await fixture(t);
  assert.ok(f.calls.some(call => call.type === "poll" && call.ready));
  f.input.write("\u001b[200~\uD55C\uAE00\nsecond\u001b[201~");
  await waitFor(() => f.calls.some(call => call.type === "draft" && call.dirty));
  assert.equal(f.posts.length, 0);
  f.input.write("\r"); await waitFor(() => f.calls.some(call => call.type === "finish"));
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].body.messages[0].content, "\uD55C\uAE00\nsecond");
  assert.ok(f.calls.findIndex(call => call.type === "prepare") < f.calls.findIndex(call => call.type === "authorize"));
  const authorize = f.calls.find(call => call.type === "authorize");
  assert.equal(authorize.prompt, "\uD55C\uAE00\nsecond"); assert.equal(authorize.deliveryId, null);
  assert.deepEqual(JSON.parse(authorize.binding.requestBody), f.posts[0].body);
});

test("completed turns use parent context and a typed peer /exit is model data", async t => {
  const f = await fixture(t);
  f.input.write("first\r"); await waitFor(() => !f.terminal.active && f.posts.length === 1);
  const command = { type: "submit", requestId: randomUUID(), deliveryId: "delivery-1", prompt: "/exit" };
  f.commands.push(command);
  await waitFor(() => f.calls.filter(call => call.type === "finish").length === 2);
  assert.equal(f.terminal.closed, false);
  assert.deepEqual(f.posts[1].body.messages, [{ role: "user", content: "first" }, { role: "assistant", content: "Mock completed answer" }, { role: "user", content: "/exit" }]);
  f.commands.push(command); await pause(60); assert.equal(f.posts.length, 2);
  f.input.write("/exit\r"); assert.equal(f.terminal.closed, true);
});

test("typed peer refuses a dirty draft and never merges it with human text", async t => {
  const f = await fixture(t);
  f.input.write("private draft");
  f.commands.push({ type: "submit", requestId: randomUUID(), deliveryId: "delivery-1", prompt: "Peer text" });
  await waitFor(() => f.calls.some(call => call.type === "reject"));
  assert.equal(f.calls.find(call => call.type === "reject").code, "DRAFT_BUSY");
  assert.equal(f.posts.length, 0); assert.equal(f.terminal.editor.text, "private draft");
});

test("typing while grant is pending consumes no model POST and retains the new draft", async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = await fixture(t, { controlHandler: async message => { if (message.type === "authorize") { await gate; return { grant: randomUUID() }; } } });
  f.input.write("question\r"); await waitFor(() => f.calls.some(call => call.type === "authorize"));
  f.input.write("new draft"); release();
  await waitFor(() => f.calls.some(call => call.type === "finish"));
  assert.equal(f.posts.length, 0); assert.equal(f.terminal.editor.text, "new draft");
  assert.equal(f.calls.find(call => call.type === "finish").result.state, "FAILED");
});

test("queue wait remains cancellable, continues polling and starts no generation", async t => {
  const f = await fixture(t, { controlHandler: message => message.type === "prepare" ? { waiting: true } : undefined });
  f.input.write("queued\r"); await waitFor(() => f.calls.filter(call => call.type === "prepare").length >= 2);
  f.input.write("\u0003"); await waitFor(() => !f.terminal.active);
  assert.equal(f.posts.length, 0); assert.equal(f.calls.filter(call => call.type === "cancel").length, 1);
  assert.ok(f.calls.some(call => call.type === "poll" && call.requestId));
  assert.equal(f.calls.some(call => call.type === "finish"), false);
});

test("queue timeout cancels one waiting request without obtaining a grant", async t => {
  const f = await fixture(t, { config: { requestTimeoutMs: 60 }, controlHandler: message => message.type === "prepare" ? { waiting: true } : undefined });
  f.input.write("queued\r"); await waitFor(() => f.calls.some(call => call.type === "cancel"));
  assert.equal(f.calls.find(call => call.type === "cancel").reason, "QUEUE_TIMEOUT");
  assert.equal(f.calls.some(call => call.type === "authorize"), false); assert.equal(f.posts.length, 0);
});

test("Ctrl+C aborts the model socket before a slow cancel ACK and marks unconfirmed server cancellation", async t => {
  let socketClosed = false, release;
  const gate = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  const f = await fixture(t, { apiHandler: (_req, res) => {
    res.on("close", () => { socketClosed = true; });
    res.setHeader("Content-Type", "application/x-ndjson");
    res.write(JSON.stringify({ model: "fixture-model", message: { content: "partial" }, done: false }) + "\n");
  }, controlHandler: async message => { if (message.type === "cancel") await gate; } });
  f.input.write("slow\r"); await waitFor(() => f.output.join("").includes("partial"));
  f.input.write("\u0003"); await waitFor(() => socketClosed);
  await waitFor(() => f.calls.some(call => call.type === "finish"));
  const result = f.calls.find(call => call.type === "finish").result;
  assert.equal(result.state, "CANCELLED"); assert.equal(result.serverCancelUnconfirmed, true); assert.equal(f.posts.length, 1);
  release();
});

test("lost grant response exits the helper without inference or automatic retry", async t => {
  const f = await fixture(t, { controlHandler: (message, res) => { if (message.type === "authorize") res.destroy(); } });
  f.input.write("question\r"); await waitFor(() => f.terminal.closed);
  assert.equal(f.posts.length, 0); assert.equal(f.calls.filter(call => call.type === "authorize").length, 1);
  assert.deepEqual(f.exits, [1]); assert.equal(f.calls.some(call => call.type === "finish"), false);
});

test("lost heartbeat aborts an active model stream and closes without a false completed record", async t => {
  let failPoll = false, socketClosed = false;
  const f = await fixture(t, { apiHandler: (_req, res) => {
    res.on("close", () => { socketClosed = true; });
    res.setHeader("Content-Type", "application/x-ndjson");
    res.write(JSON.stringify({ model: "fixture-model", message: { content: "partial" }, done: false }) + "\n");
  }, controlHandler: (message, res) => { if (message.type === "poll" && failPoll) res.destroy(); } });
  f.input.write("question\r"); await waitFor(() => f.posts.length === 1); failPoll = true;
  await waitFor(() => f.terminal.closed && socketClosed);
  assert.equal(f.calls.some(call => call.type === "finish"), false); assert.equal(f.posts.length, 1);
});

test("finish CAS state controls the displayed status, and model control sequences are inert", async t => {
  const f = await fixture(t, { apiHandler: (_req, res) => { res.setHeader("Content-Type", "application/x-ndjson"); res.end(frame("\u001b]52;c;attack\u0007\u001b[2Janswer")); },
    controlHandler: message => message.type === "finish" ? { state: "CANCELLED" } : undefined });
  f.input.write("question\r"); await waitFor(() => f.output.join("").includes("[CANCELLED]"));
  const output = f.output.join("");
  assert.ok(!output.includes("\u001b]52;")); assert.ok(!output.includes("\u001b[2J")); assert.ok(!output.includes("[COMPLETED]"));
  assert.match(output, /answer/);
});

test("LM Studio token stays in bootstrap memory and model headers, never in request metadata or display", async t => {
  const secret = "memory-only-model-secret";
  const f = await fixture(t, { config: { backend: "lmstudio", token: secret }, apiHandler: (_req, res) => {
    res.setHeader("Content-Type", "text/event-stream");
    res.end("data: " + JSON.stringify({ model: "fixture-model", choices: [{ index: 0, delta: { content: "LM mock answer" }, finish_reason: "stop" }] }) + "\n\ndata: [DONE]\n\n");
  } });
  f.input.write("LM question\r"); await waitFor(() => f.calls.some(call => call.type === "finish"));
  assert.equal(f.posts[0].path, "/v1/chat/completions"); assert.equal(f.posts[0].headers.authorization, `Bearer ${secret}`);
  assert.equal(f.calls.find(call => call.type === "finish").result.state, "COMPLETED");
  assert.ok(!JSON.stringify(f.calls).includes(secret)); assert.ok(!f.output.join("").includes(secret));
});

test("PTY input EOF closes the session and a typed stop cancels active inference", async t => {
  const first = await fixture(t); first.input.end(); await waitFor(() => first.terminal.closed);
  assert.equal(first.posts.length, 0);
  let socketClosed = false;
  const second = await fixture(t, { apiHandler: (_req, res) => {
    res.on("close", () => { socketClosed = true; });
    res.setHeader("Content-Type", "application/x-ndjson");
    res.write(JSON.stringify({ model: "fixture-model", message: { content: "partial" }, done: false }) + "\n");
  } });
  second.input.write("question\r"); await waitFor(() => second.posts.length === 1);
  second.commands.push({ type: "stop" }); await waitFor(() => second.terminal.closed && socketClosed);
  assert.equal(second.posts.length, 1);
});

test("control client rejects remote/path tricks, HTTP errors, oversized and malformed responses", async t => {
  for (const endpoint of ["https://127.0.0.1:1234/events/a", "http://localhost:1234/events/a", "http://127.0.0.1:1234/events/a?x=1", "http://127.1:1234/events/a"]) {
    assert.throws(() => controlClient(endpoint, randomUUID()), { code: "INVALID_CONTROL_CONFIG" });
  }
  for (const mode of ["status", "oversize", "json"]) {
    const host = await server(t, (_req, res) => {
      if (mode === "status") { res.statusCode = 307; res.setHeader("Location", "http://localhost/"); res.end("{}"); }
      else res.end(mode === "oversize" ? "x".repeat(192 * 1024 + 1) : "{\"ok\":");
    });
    await assert.rejects(controlClient(`${host.origin}/events/a`, randomUUID())({ type: "bootstrap" }));
  }
});

test("real node-pty helper handles Unicode paste, resizing, typed peer data and exit", { timeout: 20000 }, async t => {
  const pty = require("node-pty");
  const { Terminal } = require("@xterm/headless");
  const f = await fixture(t, { start: false });
  const screen = new Terminal({ cols: 64, rows: 16, allowProposedApi: true });
  const chunks = [], writes = [];
  const child = pty.spawn(process.execPath, [path.resolve(__dirname, "../media/local-terminal.cjs")], {
    name: "xterm-256color", cols: 64, rows: 16, cwd: path.resolve(__dirname, ".."),
    env: { ...process.env, PATCHPORT_ENDPOINT: `${f.host.origin}/events/node-fixture`, PATCHPORT_TOKEN: f.token, ELECTRON_RUN_AS_NODE: "1" }
  });
  const closed = new Promise(resolve => child.onExit(resolve));
  child.onData(text => { chunks.push(text); writes.push(new Promise(resolve => screen.write(text, resolve))); });
  t.after(() => { try { child.kill(); } catch { /* Already exited. */ } screen.dispose(); });
  await waitFor(() => f.calls.some(call => call.type === "poll" && call.ready));
  assert.equal(f.posts.length, 0);
  child.write("\u001b[200~\uD55C\uAE00 \uD83D\uDE42\nsecond line\u001b[201~");
  await waitFor(() => f.calls.some(call => call.type === "draft" && call.dirty));
  assert.equal(f.posts.length, 0); child.resize(91, 22); screen.resize(91, 22); child.write("\r");
  await waitFor(() => f.calls.some(call => call.type === "finish"));
  assert.equal(f.posts[0].body.messages[0].content, "\uD55C\uAE00 \uD83D\uDE42\nsecond line");
  f.commands.push({ type: "submit", requestId: randomUUID(), deliveryId: "delivery-pty", prompt: "/exit\nPeer-only data" });
  await waitFor(() => f.calls.filter(call => call.type === "finish").length === 2);
  assert.equal(f.posts[1].body.messages.at(-1).content, "/exit\nPeer-only data");
  child.write("/exit\r"); const exit = await closed; assert.equal(exit.exitCode, 0);
  await Promise.all(writes);
  const visible = Array.from({ length: screen.buffer.active.length }, (_, index) => screen.buffer.active.getLine(index)?.translateToString(true)).join("\n");
  assert.match(visible, /Mock completed answer/); assert.match(chunks.join(""), /\uD55C\uAE00 \uD83D\uDE42/);
  assert.ok(!chunks.join("").includes(f.token));
});
