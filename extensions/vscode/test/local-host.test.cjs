"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { LocalHost } = require("../dist/test/localHost");
const { Router, defaultBoard, validateLocalProfile } = require("../dist/test/model");
const api = require("../media/local-api.cjs");

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const tick = () => new Promise(resolve => setImmediate(resolve));

async function fixture(t, hooks = {}) {
  const original = api.createLocalClient;
  t.mock.method(api, "createLocalClient", config => ({ ...original(config), listModels: async () => ({ models: [{ modelId: "fixture-model", supported: true }] }) }));
  const profile = validateLocalProfile({ id: "local-test", name: "Local fixture", backend: "ollama", endpointOrigin: "http://127.0.0.1:11434", modelId: "fixture-model", contextWindow: 8192, maxOutputTokens: 256, requestTimeoutSec: 10, revision: randomUUID(), executionLocationConfirmed: true });
  const board = defaultBoard(); board.version = 2;
  const node = { id: "local", name: profile.name, profileId: profile.id, provider: "local", project: "/project", groupId: "team", x: 0, y: 0 };
  board.nodes.push(node, { ...node, id: "peer", provider: "codex", profileId: "peer" });
  const router = new Router(board, () => {}), terminated = [], calls = { prepare: [], finish: [], dispatch: [], delivery: [] }, owners = new Map();
  let session = 1;
  const ledger = {
    core: { epoch: 1 },
    localIdentity: id => ({ id: `${id}-${session}`, epoch: 1 }),
    localContext: async () => { if (hooks.context) await hooks.context(); return { messages: [] }; },
    localPrepare: async (id, requestId) => { calls.prepare.push({ id, requestId, session }); owners.set(requestId, session); if (hooks.prepare) await hooks.prepare(); return { binding_sha256: "a".repeat(64) }; },
    localDispatch: async (id, requestId) => { calls.dispatch.push({ id, requestId, session }); return { grant: "b".repeat(64) }; },
    localFinish: async (id, requestId, hash, outcome) => { calls.finish.push({ id, requestId, session }); if (owners.get(requestId) !== session) throw Error("Request belongs to the previous session"); if (hooks.finish) await hooks.finish(); return { state: outcome.state }; },
    delivery: async value => { calls.delivery.push(value); if (hooks.delivery) await hooks.delivery(); }
  };
  const host = new LocalHost(router, ledger, path.resolve(__dirname, ".."), id => { terminated.push(id); router.stopped(id); }, hooks.persist);
  t.after(() => host.dispose());
  let identity;
  const attach = async () => {
    router.started(node.id); await host.attach(node, profile);
    const boot = await host.handle(node.id, { local: 1, type: "bootstrap" });
    identity = { helperId: boot.helperId, generation: boot.generation, coreEpoch: boot.coreEpoch };
    await host.handle(node.id, { local: 1, ...identity, type: "poll", ready: true, dirty: false, requestId: null });
  };
  await attach();
  const replace = async () => { host.exited(node.id); router.stopped(node.id); session++; await attach(); };
  const control = payload => host.handle(node.id, { local: 1, ...identity, ...payload });
  const prepare = async () => {
    const requestId = randomUUID(), prompt = "Controlled question";
    const prepared = await control({ type: "prepare", requestId, prompt, deliveryId: null });
    const binding = original({ ...profile.local, requestTimeoutMs: profile.local.requestTimeoutSec * 1000 }).prepareRequest({ requestId, messages: prepared.messages });
    return { requestId, prompt, deliveryId: null, binding };
  };
  return { node, profile, board, router, host, ledger, calls, terminated, control, prepare, replace };
}

test("a delayed old authorization and queued cancellation cannot terminate a replacement session", async t => {
  const entered = deferred(), release = deferred();
  const f = await fixture(t, { persist: async () => { entered.resolve(); await release.promise; } });
  t.after(() => release.resolve());
  const request = await f.prepare(), old = f.host.states.get(f.node.id);
  const authorization = f.control({ type: "authorize", ...request });
  const denied = assert.rejects(authorization, /identity changed/);
  await entered.promise;
  await f.control({ type: "cancel", requestId: request.requestId });
  await f.replace(); const replacement = f.router.runtime.get(f.node.id).generation;
  release.resolve(); await denied; await old.chain; await tick();
  assert.equal(f.calls.dispatch.length, 1); assert.equal(f.calls.finish.length, 0);
  assert.deepEqual(f.terminated, []); assert.equal(f.router.runtime.get(f.node.id).generation, replacement);
  assert.equal(f.router.runtime.get(f.node.id).phase, "ready"); assert.equal(f.host.idle(f.node.id), true);
  assert.equal(f.host.slots.size, 0); assert.equal(f.host.waiting.size, 0);
});

test("a late delivery-write failure is scoped to the original local session", async t => {
  const entered = deferred(), release = deferred();
  const f = await fixture(t, { delivery: async () => { entered.resolve(); await release.promise; } });
  t.after(() => release.resolve());
  f.router.started("peer"); f.router.ready("peer"); f.router.connect("peer", f.node.id); f.router.complete("peer", "Peer question", randomUUID());
  const delivery = f.router.deliveries[0], queued = f.host.queue(f.node.id, delivery);
  const failed = assert.rejects(queued, /Controlled write failure/);
  await entered.promise; await f.replace(); release.reject(Error("Controlled write failure")); await failed; await tick();
  assert.deepEqual(f.terminated, []); assert.equal(f.router.runtime.get(f.node.id).phase, "ready"); assert.equal(f.host.idle(f.node.id), true);
  assert.equal(f.host.states.get(f.node.id).commands.length, 0);
});

for (const stage of ["context", "prepare"]) {
  test(`session replacement while awaiting ${stage} cannot continue the old local request`, async t => {
    const entered = deferred(), release = deferred();
    const f = await fixture(t, { [stage]: async () => { entered.resolve(); await release.promise; } });
    t.after(() => release.resolve());
    const pending = f.control({ type: "prepare", requestId: randomUUID(), prompt: "Old question", deliveryId: null });
    const failed = assert.rejects(pending, /identity changed/);
    await entered.promise; await f.replace(); release.resolve(); await failed;
    assert.equal(f.calls.prepare.length, stage === "context" ? 0 : 1); assert.equal(f.calls.dispatch.length, 0);
    assert.equal(f.router.runtime.get(f.node.id).phase, "ready"); assert.equal(f.router.pendingLocal(f.node.id), undefined);
    assert.equal(f.host.slots.size, 0); assert.equal(f.host.waiting.size, 0); assert.deepEqual(f.terminated, []);
  });
}

test("an old terminal CAS response cannot publish into a replacement local session", async t => {
  const entered = deferred(), release = deferred();
  const f = await fixture(t, { finish: async () => { entered.resolve(); await release.promise; } });
  t.after(() => release.resolve());
  const request = await f.prepare(); await f.control({ type: "authorize", ...request });
  const pending = f.control({ type: "finish", requestId: request.requestId, deliveryId: null,
    result: { requestId: request.requestId, state: "COMPLETED", content: "Old completed reply", reportedModel: "fixture-model", usage: {} } });
  const failed = assert.rejects(pending, /identity changed/);
  await entered.promise; await f.replace(); release.resolve(); await failed;
  assert.equal(f.router.runtime.get(f.node.id).lastMessage, ""); assert.equal(f.router.runtime.get(f.node.id).phase, "ready");
  assert.equal(f.host.states.get(f.node.id).completed.size, 0); assert.deepEqual(f.terminated, []);
});
