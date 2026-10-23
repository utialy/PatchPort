"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { randomUUID } = require("node:crypto");
const { once } = require("node:events");
const { CoreClient, Ledger } = require("../dist/test/core");
const { Router, defaultBoard, validateLocalProfile } = require("../dist/test/model");
const { SessionHost } = require("../dist/test/session");
const { python, temporaryRoot } = require("./helpers.cjs");

const extensionRoot = path.resolve(__dirname, "..");
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const waitFor = async (check, timeout = 15000, diagnostic = () => "") => {
  const end = Date.now() + timeout;
  while (!await check()) { if (Date.now() >= end) throw Error("Local session fixture timed out: " + diagnostic()); await pause(25); }
};

function reply(response, backend, content) {
  if (backend === "ollama") {
    response.writeHead(200, { "Content-Type": "application/x-ndjson" });
    response.end(JSON.stringify({ model: "fixture-model", message: { role: "assistant", content }, done: true,
      done_reason: "stop", prompt_eval_count: 2, eval_count: 3 }) + "\n");
  } else {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.end("data: " + JSON.stringify({ model: "fixture-model", choices: [{ index: 0, delta: { content }, finish_reason: "stop" }] }) + "\n\n" +
      "data: " + JSON.stringify({ model: "fixture-model", choices: [], usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 } }) + "\n\ndata: [DONE]\n\n");
  }
}

async function closeCore(core) {
  const child = core.child;
  if (!child || child.exitCode !== null) { core.dispose(); return; }
  const stopped = once(child, "exit"); core.dispose();
  let timer;
  try { await Promise.race([stopped, new Promise((_, reject) => { timer = setTimeout(() => reject(Error("Isolated core did not exit")), 5000); })]); }
  finally { clearTimeout(timer); }
}

async function fixture(t, { runtimeHandler, backends = ["ollama", "lmstudio"], finishAckDelayMs = 0 } = {}) {
  const project = await fs.mkdtemp(path.join(temporaryRoot, "patchport-local-session-"));
  const core = new CoreClient(python, path.join(extensionRoot, "core"));
  const observer = new CoreClient(python, path.join(extensionRoot, "core"));
  core.retain("local-session-fixture"); observer.retain("local-session-observer");
  const ledger = new Ledger(core, path.join(project, "ledgers"));
  const binding = await ledger.inspect(project), scope = { database: binding.database, project };
  const legacy = await core.request("history.prepare.preview", scope);
  await core.request("history.apply", { plan_id: legacy.plan_id, apply: true });
  const schema = await core.request("local.schema.preview", scope);
  assert.equal(schema.report.eligible, true);
  await core.request("local.schema.apply", { plan_id: schema.plan_id, apply: true });
  const requests = [], posts = [], responses = [], output = { a: "", b: "" };
  const secret = "isolated-lmstudio-secret";
  let active = 0, maxActive = 0;
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      const call = { method: request.method, path: request.url, headers: request.headers, body: Buffer.concat(chunks).toString("utf8") };
      requests.push(call);
      if (call.method === "GET") {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify(call.path === "/api/tags" ? { models: [{ model: "fixture-model", capabilities: ["completion"] }] } : { data: [{ id: "fixture-model", type: "llm" }] }));
        return;
      }
      call.payload = JSON.parse(call.body); call.backend = call.path === "/api/chat" ? "ollama" : "lmstudio";
      posts.push(call); responses.push(response); active++; maxActive = Math.max(maxActive, active);
      let settled = false;
      const ended = () => { if (!settled) { settled = true; active--; } };
      response.on("close", ended); response.on("finish", ended);
      Promise.resolve(runtimeHandler ? runtimeHandler(call, response, posts.length) : reply(response, call.backend, `Mock answer ${posts.length}: \uD55C\uAE00`)).catch(() => response.destroy());
    });
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const origin = `http://127.0.0.1:${server.address().port}`;
  const profiles = backends.map((backend, index) => ["codex", "claude"].includes(backend) ? {
    id: `mock-${index}`, name: `Mock ${backend}`, provider: backend, command: process.execPath,
    args: [path.join(__dirname, "fake-ai.cjs"), "--provider", backend], shell: "direct"
  } : validateLocalProfile({ id: `local-${index}`, name: `Local ${backend}`,
    backend, endpointOrigin: origin, modelId: "fixture-model", contextWindow: 16384, maxOutputTokens: 256,
    requestTimeoutSec: 10, revision: randomUUID(), executionLocationConfirmed: true }));
  const board = defaultBoard(); board.version = 2; board.groups[0].maxHops = 2; board.groups[0].maxDeliveries = 2;
  board.nodes = profiles.map((profile, index) => ({ id: String.fromCharCode(97 + index), name: profile.name, provider: profile.provider,
    profileId: profile.id, project, groupId: "team", x: 0, y: 0 }));
  let host, router;
  const deliverySnapshots = new Map(), ledgerErrors = [];
  router = new Router(board, async (id, text, delivery) => {
    if (delivery) return host.local.queue(id, delivery);
    const ordinary = router.deliveries.find(item => item.target === id && item.status === "sent");
    assert.ok(ordinary, "CLI routing requires a recorded delivery");
    await ledger.delivery({ ...ordinary, status: "queued" });
    await ledger.record(id, "input_attempt", { delivery_id: ordinary.id, origin: "peer" });
    await ledger.delivery(ordinary); host.send(id, text);
  }, () => {
    if (!router) return;
    for (const delivery of router.deliveries) {
      if (board.nodes.find(node => node.id === delivery.target)?.provider === "local" || delivery.status === "sent") continue;
      const key = JSON.stringify(delivery);
      if (deliverySnapshots.get(delivery.id) === key) continue;
      deliverySnapshots.set(delivery.id, key);
      void ledger.delivery({ ...delivery }).catch(error => ledgerErrors.push(error));
    }
  });
  let persists = 0;
  host = new SessionHost(router, path.join(project, "runtime"), extensionRoot, ledger,
    async profile => profile.local?.backend === "lmstudio" ? secret : undefined, async () => { persists++; });
  host.on("output", (id, value) => { output[id] = (output[id] || "") + value; });
  const controls = [], controlReplies = [];
  const handle = host.local.handle.bind(host.local);
  host.local.handle = async (id, payload) => {
    controls.push({ id, ...payload });
    const response = await handle(id, payload);
    controlReplies.push({ id, type: payload.type, requestId: payload.requestId, response });
    if (payload.type === "finish" && finishAckDelayMs) await pause(finishAckDelayMs);
    return response;
  };
  t.after(async () => {
    try { await host.shutdown(); await ledger.end(); }
    finally {
      // A failing lifecycle check must not leave the mock server holding the test worker open.
      server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
      try { await closeCore(core); }
      finally {
        try { await closeCore(observer); }
        finally {
          const checked = path.resolve(project), root = path.resolve(temporaryRoot) + path.sep;
          assert.ok(checked.startsWith(root)); await fs.rm(checked, { recursive: true, force: true });
        }
      }
    }
  });
  const diagnostic = () => JSON.stringify({ runtime: [...router.runtime.values()], deliveries: router.deliveries, output });
  const start = async () => {
    for (let index = 0; index < profiles.length; index++) await host.start(board.nodes[index], profiles[index]);
    for (const node of board.nodes.filter(item => item.provider === "codex")) {
      await waitFor(() => output[node.id]?.includes("PATCHPORT_FIXTURE codex"), 15000, diagnostic);
      router.ready(node.id, true);
    }
    await waitFor(() => board.nodes.every(node => router.runtime.get(node.id)?.phase === "ready"), 15000, diagnostic);
  };
  return { project, core, observer, ledger, scope, host, board, router, profiles, output, controls, controlReplies,
    requests, posts, responses, secret, start, diagnostic, ledgerErrors, maxActive: () => maxActive, persists: () => persists };
}

test("real local PTYs route Ollama to LM Studio and back with exact request usage and two-hop accounting", { timeout: 40000 }, async t => {
  const f = await fixture(t); await f.start();
  assert.equal(f.posts.length, 0); assert.equal(f.requests.filter(call => call.method === "GET").length, 2);
  f.router.connect("a", "b"); f.router.connect("b", "a");
  f.host.resize("a", 73, 19);
  f.host.input("a", "\u001b[200~\uC9C8\uBB38 \uD83D\uDE42\nsecond line\u001b[201~");
  await waitFor(() => f.router.runtime.get("a").dirty); assert.equal(f.posts.length, 0);
  f.host.input("a", "\r");
  await waitFor(() => f.router.deliveries.length === 2 && f.router.deliveries.every(delivery => delivery.status === "completed"), 15000, f.diagnostic);
  assert.equal(f.posts.length, 3); assert.equal(f.board.groups[0].used, 2); assert.equal(f.persists(), 3);
  assert.deepEqual(f.posts.map(call => call.backend), ["ollama", "lmstudio", "ollama"]);
  assert.equal(f.posts[0].payload.messages.at(-1).content, "\uC9C8\uBB38 \uD83D\uDE42\nsecond line");
  assert.equal(f.posts[1].payload.messages.at(-1).content, "Mock answer 1: \uD55C\uAE00");
  assert.equal(f.posts[2].payload.messages.at(-1).content, "Mock answer 2: \uD55C\uAE00");
  assert.equal(f.posts[2].payload.messages.filter(message => message.role === "assistant").length, 1);
  assert.equal(f.maxActive(), 1);
  const contextA = await f.ledger.localContext("a"), contextB = await f.ledger.localContext("b");
  assert.equal(contextA.completed_requests, 2); assert.equal(contextB.completed_requests, 1);
  const report = await f.ledger.report(f.project);
  assert.equal(report.summary.metrics.input_tokens.known_sum, 6); assert.equal(report.summary.metrics.output_tokens.known_sum, 9);
  assert.equal(Object.values(report.group_attempts).reduce((total, value) => total + value, 0), 2);
  assert.equal(f.posts[1].headers.authorization, `Bearer ${f.secret}`);
  assert.ok(!JSON.stringify(f.controls).includes(f.secret)); assert.ok(!JSON.stringify(report).includes(f.secret));
  assert.ok(!Object.values(f.output).join("").includes(f.secret));
  const snapshot = await f.host.snapshot("a"); assert.equal(snapshot.cols, 73); assert.equal(snapshot.rows, 19); assert.match(snapshot.data, /Mock answer 3/);
  const sessionIds = [...f.ledger.sessions.values()].map(session => session.id);
  for (const node of f.board.nodes) f.host.input(node.id, "/exit\r");
  await Promise.all(f.board.nodes.map(node => f.host.waitStopped(node.id)));
  const purge = await f.observer.request("history.purge.preview", { ...f.scope, session_ids: sessionIds });
  assert.equal(purge.report.eligible, true);
});

test("two real local cards sharing one endpoint serialize human generations while polling remains live", { timeout: 40000 }, async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  const f = await fixture(t, { backends: ["ollama", "ollama"], runtimeHandler: async (call, response, count) => {
    if (count === 1) await gate;
    reply(response, call.backend, `Serial answer ${count}`);
  } });
  await f.start(); f.host.input("a", "first\r"); await waitFor(() => f.posts.length === 1, 15000, f.diagnostic);
  f.host.input("b", "second\r");
  await waitFor(() => f.controlReplies.some(item => item.id === "b" && item.type === "prepare" && item.response.waiting), 15000, f.diagnostic);
  const waitingId = f.router.pendingLocal("b").requestId;
  await pause(200); assert.equal(f.posts.length, 1); assert.equal(f.router.pendingLocal("b").authorized, false);
  assert.ok(f.controls.some(item => item.id === "b" && item.type === "poll" && item.requestId === waitingId));
  release();
  await waitFor(() => ["a", "b"].every(id => !f.router.pendingLocal(id)) && f.posts.length === 2, 15000, f.diagnostic);
  assert.equal(f.maxActive(), 1); assert.equal(f.board.groups[0].used, 0);
  assert.equal((await f.ledger.localContext("a")).completed_requests, 1);
  assert.equal((await f.ledger.localContext("b")).completed_requests, 1);
});

test("peer cancellation in a real PTY ignores late model completion, preserves the charged hop and requires explicit resume", { timeout: 40000 }, async t => {
  let cancelledSocket = false;
  const f = await fixture(t, { runtimeHandler: (call, response, count) => {
    if (count === 1) return reply(response, call.backend, "First complete answer");
    response.on("close", () => { cancelledSocket = true; });
    response.setHeader("Content-Type", "text/event-stream");
    response.write("data: " + JSON.stringify({ model: "fixture-model", choices: [{ index: 0, delta: { content: "unfinished response" }, finish_reason: null }] }) + "\n\n");
  } });
  await f.start(); f.router.connect("a", "b"); f.router.connect("b", "a");
  f.host.input("a", "start chain\r"); await waitFor(() => f.posts.length === 2 && f.output.b.includes("unfinished response"), 15000, f.diagnostic);
  const requestId = f.router.pendingLocal("b").requestId;
  f.host.input("b", "\u0003");
  await waitFor(() => cancelledSocket && !f.router.pendingLocal("b"), 15000, f.diagnostic);
  const result = await f.observer.request("local.result", { ...f.scope, request_id: requestId });
  assert.equal(result.state, "CANCELLED"); assert.equal((await f.ledger.localContext("b")).completed_requests, 0);
  assert.equal(f.board.groups[0].paused, true); assert.equal(f.board.groups[0].used, 1);
  assert.equal(f.router.deliveries.length, 1); assert.equal(f.router.deliveries[0].status, "uncertain");
  f.responses[1].end("data: " + JSON.stringify({ model: "fixture-model", choices: [{ index: 0, delta: { content: "late answer" }, finish_reason: "stop" }] }) + "\n\ndata: [DONE]\n\n");
  await pause(250); assert.equal(f.posts.length, 2); assert.equal(f.router.deliveries.length, 1);
  assert.equal(f.router.runtime.get("b").phase, "attention");
  assert.equal((await f.observer.request("local.result", { ...f.scope, request_id: requestId })).state, "CANCELLED");
  f.host.local.confirmReady("b"); assert.equal(f.router.runtime.get("b").phase, "ready");
  assert.equal(f.board.groups[0].paused, true);
});

test("core epoch loss stops a real local helper and preserves uncertain request evidence without replay", { timeout: 40000 }, async t => {
  let socketClosed = false;
  const f = await fixture(t, { backends: ["ollama"], runtimeHandler: (_call, response) => {
    response.on("close", () => { socketClosed = true; });
    response.setHeader("Content-Type", "application/x-ndjson");
    response.write(JSON.stringify({ model: "fixture-model", message: { content: "partial before core loss" }, done: false }) + "\n");
  } });
  await f.start(); f.host.input("a", "core loss\r");
  await waitFor(() => f.posts.length === 1 && f.router.pendingLocal("a")?.authorized, 15000, f.diagnostic);
  const requestId = f.router.pendingLocal("a").requestId;
  await closeCore(f.core);
  await waitFor(() => !f.host.isActive("a") && socketClosed, 15000, f.diagnostic);
  assert.equal(f.posts.length, 1);
  const result = await f.observer.request("local.result", { ...f.scope, request_id: requestId });
  assert.equal(result.state, "UNCERTAIN"); assert.equal(f.router.runtime.get("a").phase, "stopped");
  assert.ok(!f.output.a.includes("[COMPLETED]"));
});

for (const provider of ["claude", "codex"]) {
  test(`real ${provider} mock CLI routes a completed answer to local and receives one completed reply`, { timeout: 45000 }, async t => {
    const f = await fixture(t, { backends: [provider, "ollama"] }); await f.start();
    assert.equal(f.posts.length, 0);
    f.router.connect("a", "b"); f.router.connect("b", "a");
    f.host.input("a", "Mixed provider question\r");
    await waitFor(() => f.router.deliveries.length === 2 && f.router.deliveries.every(delivery => delivery.status === "completed"), 25000, f.diagnostic);
    assert.equal(f.posts.length, 1); assert.equal(f.board.groups[0].used, 2);
    assert.deepEqual(f.router.deliveries.map(delivery => delivery.hops), [1, 2]);
    assert.equal(f.router.deliveries[0].root, f.router.deliveries[1].root);
    assert.equal(f.posts[0].payload.messages.at(-1).content, `${provider.toUpperCase()}_REPLY_1: Mixed provider question`);
    assert.match(f.output.a, new RegExp(`${provider.toUpperCase()}_REPLY_2: Peer message received`));
    const context = await f.ledger.localContext("b"); assert.equal(context.completed_requests, 1);
    assert.equal(context.messages[1].content, "Mock answer 1: \uD55C\uAE00");
    const report = await f.ledger.report(f.project);
    assert.equal(report.summary.metrics.input_tokens.known_sum, 2); assert.equal(report.summary.metrics.output_tokens.known_sum, 3);
    assert.equal(report.summary.metrics.input_tokens.unknown_tasks, 1);
    assert.equal(Object.values(report.group_attempts).reduce((total, value) => total + value, 0), 2);
    assert.equal(f.ledgerErrors.length, 0);
    await pause(150); assert.equal(f.posts.length, 1); assert.equal(f.router.deliveries.length, 2);
  });
}

test("two queued peer replies reach one local PTY in order after the previous finish ACK", { timeout: 45000 }, async t => {
  let release;
  const gate = new Promise(resolve => { release = resolve; }); t.after(() => release());
  const f = await fixture(t, { backends: ["claude", "ollama", "claude"], finishAckDelayMs: 350, runtimeHandler: async (call, response, count) => {
    if (count === 1) await gate;
    reply(response, call.backend, `Queued answer ${count}`);
  } });
  await f.start(); f.router.connect("a", "b"); f.router.connect("c", "b");
  f.host.input("a", "First peer question\r");
  await waitFor(() => f.posts.length === 1, 15000, f.diagnostic);
  f.host.input("c", "Second peer question\r");
  await waitFor(() => f.router.deliveries.length === 2 && f.router.deliveries[1].status === "queued", 15000, f.diagnostic);
  assert.equal(f.board.groups[0].used, 1); release();
  await waitFor(() => f.router.deliveries.every(delivery => delivery.status === "completed"), 15000, f.diagnostic);
  assert.equal(f.posts.length, 2); assert.equal(f.board.groups[0].used, 2); assert.equal(f.maxActive(), 1);
  assert.deepEqual(f.posts.map(call => call.payload.messages.at(-1).content), [
    "CLAUDE_REPLY_1: First peer question", "CLAUDE_REPLY_1: Second peer question"
  ]);
  assert.equal((await f.ledger.localContext("b")).completed_requests, 2);
  assert.equal(f.controls.some(item => item.type === "reject"), false);
  assert.equal(f.ledgerErrors.length, 0);
});
