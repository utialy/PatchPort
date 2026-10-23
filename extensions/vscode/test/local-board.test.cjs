const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { randomUUID, createHash } = require("node:crypto");
const { Router, defaultBoard, restoreBoard, validateProfile, validateLocalProfile } = require("../dist/test/model");
const { StateStore, selectBoardSnapshot } = require("../dist/test/stateStore");
const { resumeProfile, verifyLocalHelpers } = require("../dist/test/workflow");

function profile(overrides = {}) {
  return { id: "local-1", name: "Local chat", backend: "ollama", endpointOrigin: "http://127.0.0.1:11434", modelId: "model:latest", contextWindow: 8192, maxOutputTokens: 1024, requestTimeoutSec: 300, revision: randomUUID(), executionLocationConfirmed: true, ...overrides };
}
function fixture() {
  const board = defaultBoard(); board.version = 2;
  board.nodes = ["a", "b", "c"].map((id, i) => ({ id, name: id, profileId: id, provider: i === 0 ? "codex" : "local", project: "/project", groupId: "team", x: 0, y: 0 }));
  const writes = [], router = new Router(board, (id, text, delivery) => { writes.push({ id, text, delivery }); });
  for (const { id } of board.nodes) { router.started(id); router.ready(id); }
  return { board, router, writes };
}
function peer(f, target = "b") {
  f.router.connect("a", target); f.router.complete("a", "Review this answer", randomUUID());
  const delivery = f.router.deliveries.find(d => d.target === target && d.status === "preparing");
  const generation = f.router.runtime.get(target).generation, requestId = randomUUID();
  f.router.beginLocalRequest(target, generation, requestId, "peer", delivery.id);
  return { target, generation, requestId, delivery };
}
function temp(t) { const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), "patchport-local-board-")); t.after(() => fs.rmSync(root, { recursive: true, force: true })); return root; }

test("local profiles are separate from shell profiles and cannot store credentials or commands", () => {
  const flat = profile(), validated = validateLocalProfile(flat);
  assert.equal(validated.provider, "local"); assert.equal(validated.command, ""); assert.deepEqual(validated.args, []); assert.equal(validated.shell, "direct");
  assert.equal(validated.local.modelId, flat.modelId);
  assert.throws(() => validateProfile(validated));
  for (const extra of [{ command: "run" }, { token: "secret" }, { args: [] }, { shell: "bash" }, { local: {} }]) assert.throws(() => validateLocalProfile({ ...flat, ...extra }));
  for (const endpointOrigin of ["http://192.168.1.2:1234", "http://localhost:1234/v1", "http://localhost:1234?key=value", "http://user@localhost", "http://2130706433", "http://127.0.0.1:65536"]) assert.throws(() => validateLocalProfile({ ...flat, endpointOrigin }));
  assert.equal(validateLocalProfile({ ...flat, endpointOrigin: "http://[::1]:1234/" }).local.endpointOrigin, "http://[::1]:1234");
  assert.throws(() => validateLocalProfile({ ...flat, maxOutputTokens: flat.contextWindow }));
  assert.throws(() => validateLocalProfile({ ...flat, requestTimeoutSec: 301 }));
  assert.throws(() => validateLocalProfile({ ...flat, name: "n".repeat(81) }));
  for (const revision of ["r".repeat(65), "revision with spaces", "-revision"]) assert.throws(() => validateLocalProfile({ ...flat, revision }));
  assert.throws(() => resumeProfile(validated, randomUUID()), /Local/);
});

test("local board snapshots upgrade atomically and do not downgrade after removing local cards", t => {
  const root = temp(t), { board } = fixture(), store = new StateStore(root, ["/project"]);
  board.version = 1; board.groups[0].used = 7; store.load(); store.save(board, { "/project": null }, "0.5.0");
  const saved = store.snapshot(); assert.equal(saved.schema, 3); assert.equal(saved.board.version, 2);
  const restored = restoreBoard(new StateStore(root, ["/project"]).load().board, ["/project"]);
  assert.equal(restored.nodes.length, 3); assert.equal(restored.nodes[1].provider, "local"); assert.equal(restored.groups[0].paused, true); assert.equal(restored.groups[0].used, 7); assert.equal(restored.groups[0].round, board.groups[0].round);
  board.nodes = board.nodes.filter(n => n.provider !== "local"); store.save(board, {}, "0.5.0"); assert.equal(store.snapshot().schema, 3);
  const record = store.beginManagement(board, {}, "0.5.0", { id: "cleanup", project: "/project", action: "reset", removed: ["b", "c"] });
  assert.equal(record.schema, 3); store.finishManagement("cleanup"); assert.equal(new StateStore(root, ["/project"]).load().management.stage, "DONE");
});

test("legacy boards preserve counters and unsupported local formats cannot fall back to mirrors", t => {
  const root = temp(t), { board } = fixture(), store = new StateStore(root, ["/project"]);
  const legacy = defaultBoard(); legacy.groups[0].used = 3;
  store.load(); store.save(legacy, {}, "0.5.0"); assert.equal(store.snapshot().schema, 2); assert.equal(restoreBoard(store.snapshot().board, ["/project"]).groups[0].used, 3);
  assert.throws(() => restoreBoard({ ...board, version: 1 }, ["/project"]));
  assert.throws(() => selectBoardSnapshot(undefined, undefined, legacy, 3));
  assert.throws(() => selectBoardSnapshot(undefined, undefined, board, undefined));
  assert.equal(selectBoardSnapshot(undefined, board, legacy, 3), board);
  assert.throws(() => selectBoardSnapshot(store.snapshot(), board, legacy, 3));
  assert.equal(selectBoardSnapshot(store.snapshot(), undefined, legacy, 2).version, 1);
  const data = JSON.parse(fs.readFileSync(store.file)); data.schema = 2; data.board = board; fs.writeFileSync(store.file, JSON.stringify(data));
  const broken = new StateStore(root, ["/project"]); assert.throws(() => broken.load()); assert.throws(() => broken.save(legacy, {}, "0.5.0"));
});

test("local peer preparation preserves origin and budget until dispatch authorization", () => {
  const f = fixture(), p = peer(f), pending = f.router.pendingLocal("b");
  assert.equal(f.board.groups[0].used, 0); assert.equal(p.delivery.status, "preparing"); assert.equal(f.writes[0].delivery.id, p.delivery.id);
  assert.equal(pending.root, p.delivery.root); assert.equal(pending.hops, p.delivery.hops); assert.equal(pending.origin, "peer");
  f.router.ready("b", true); f.router.setPhase("b", "ready"); f.router.acceptedPrompt("b"); f.router.complete("b", "Untrusted completion", randomUUID());
  assert.equal(f.router.runtime.get("b").phase, "busy"); assert.equal(f.router.pendingLocal("b").requestId, p.requestId); assert.equal(f.router.hasPendingLocal("team"), true);
  assert.throws(() => f.router.started("b"));
  f.router.assertLocalDispatch("b", p.generation, p.requestId); assert.equal(f.board.groups[0].used, 0);
  assert.equal(f.router.localDispatchAuthorized("b", p.generation, p.requestId), true); assert.equal(f.board.groups[0].used, 1); assert.equal(p.delivery.status, "sent");
  assert.equal(f.router.localDispatchAuthorized("b", p.generation, p.requestId), false); assert.equal(f.board.groups[0].used, 1);
});

test("pausing before a local grant cancels without consuming a delivery", () => {
  const f = fixture(), p = peer(f); f.board.groups[0].paused = true;
  assert.throws(() => f.router.assertLocalDispatch("b", p.generation, p.requestId));
  assert.equal(f.router.finishLocalRequest("b", p.generation, p.requestId, "CANCELLED"), true);
  assert.equal(f.board.groups[0].used, 0); assert.equal(p.delivery.status, "cancelled"); assert.equal(f.router.hasPendingLocal(), false);
});

test("concurrent local grant reservations cannot overshoot the shared budget", () => {
  const f = fixture(); f.board.groups[0].maxDeliveries = 1; f.router.connect("a", "b"); f.router.connect("a", "c"); f.router.complete("a", "fanout", randomUUID());
  const turns = ["b", "c"].map(id => { const generation = f.router.runtime.get(id).generation, requestId = randomUUID(), delivery = f.router.deliveries.find(d => d.target === id); f.router.beginLocalRequest(id, generation, requestId, "peer", delivery.id); return { id, generation, requestId }; });
  f.router.assertLocalDispatch(turns[0].id, turns[0].generation, turns[0].requestId);
  assert.throws(() => f.router.assertLocalDispatch(turns[1].id, turns[1].generation, turns[1].requestId));
  f.router.localDispatchAuthorized(turns[0].id, turns[0].generation, turns[0].requestId);
  f.router.finishLocalRequest(turns[1].id, turns[1].generation, turns[1].requestId, "CANCELLED"); assert.equal(f.board.groups[0].used, 1);
});

test("local completion resolves one delivery and preserves root and hop for its reply", () => {
  const f = fixture(), p = peer(f); f.router.connect("b", "a");
  const unrelated = { ...p.delivery, id: randomUUID(), status: "sent" }; f.router.deliveries.push(unrelated);
  f.router.assertLocalDispatch("b", p.generation, p.requestId); f.router.localDispatchAuthorized("b", p.generation, p.requestId);
  assert.equal(f.router.finishLocalRequest("b", p.generation, p.requestId, "COMPLETED", "Reviewed"), true);
  assert.equal(p.delivery.status, "completed"); assert.equal(unrelated.status, "sent");
  const outgoing = f.router.deliveries.find(d => d.source === "b"); assert.equal(outgoing.root, p.delivery.root); assert.equal(outgoing.hops, p.delivery.hops + 1);
  assert.equal(f.router.runtime.get("b").phase, "busy"); assert.equal(outgoing.status, "queued");
  assert.equal(f.writes.filter(w => w.id === "a").length, 0); f.router.ready("b");
  assert.equal(f.router.finishLocalRequest("b", p.generation, p.requestId, "COMPLETED", "Duplicate"), false);
  assert.equal(f.writes.filter(w => w.id === "a").length, 1);
});

test("the next local peer delivery waits for helper idle after completion acknowledgement", () => {
  const f = fixture(), p = peer(f); f.router.connect("b", "a");
  f.router.complete("a", "Next reply", randomUUID());
  const next = f.router.deliveries.find(d => d.id !== p.delivery.id && d.target === "b"); assert.equal(next.status, "queued");
  f.router.assertLocalDispatch("b", p.generation, p.requestId); f.router.localDispatchAuthorized("b", p.generation, p.requestId);
  f.router.finishLocalRequest("b", p.generation, p.requestId, "COMPLETED", "First complete");
  assert.equal(f.writes.length, 1); assert.equal(next.status, "queued");
  f.router.ready("b"); assert.equal(f.writes.length, 3); assert.equal(next.status, "preparing");
  assert.equal(f.writes.filter(w => w.id === "a").length, 1);
});

test("human local turns do not consume automatic-delivery budget and cancellation never revives", () => {
  const f = fixture(), generation = f.router.runtime.get("b").generation, requestId = randomUUID();
  f.router.beginLocalRequest("b", generation, requestId, "human"); f.router.assertLocalDispatch("b", generation, requestId); f.router.localDispatchAuthorized("b", generation, requestId);
  assert.equal(f.board.groups[0].used, 0); f.router.finishLocalRequest("b", generation, requestId, "CANCELLED");
  assert.equal(f.router.finishLocalRequest("b", generation, requestId, "COMPLETED", "Late answer"), false); assert.equal(f.router.runtime.get("b").lastMessage, "");
  f.router.started("b"); assert.equal(f.router.finishLocalRequest("b", generation, requestId, "COMPLETED", "Old generation"), false);
});

test("stopping during an uncertain grant consumes once and a late callback cannot dispatch again", () => {
  const f = fixture(), p = peer(f); f.router.assertLocalDispatch("b", p.generation, p.requestId); f.router.stopped("b");
  assert.equal(f.board.groups[0].used, 1); assert.equal(p.delivery.status, "uncertain");
  assert.equal(f.router.localDispatchAuthorized("b", p.generation, p.requestId, { uncertain: true }), false);
  assert.equal(f.router.finishLocalRequest("b", p.generation, p.requestId, "COMPLETED", "Late answer"), false); assert.equal(f.board.groups[0].used, 1);
});

test("non-completed authorized turns preserve uncertain delivery consumption", () => {
  for (const status of ["FAILED", "TRUNCATED", "CANCELLED", "UNCERTAIN"]) {
    const f = fixture(), p = peer(f); f.router.assertLocalDispatch("b", p.generation, p.requestId); f.router.localDispatchAuthorized("b", p.generation, p.requestId);
    assert.equal(f.router.finishLocalRequest("b", p.generation, p.requestId, status), true);
    assert.equal(f.board.groups[0].used, 1); assert.equal(p.delivery.status, "uncertain"); assert.equal(f.router.runtime.get("b").phase, "attention");
  }
});

test("local helper verification requires both fixed assets and matching hashes", async t => {
  const root = temp(t); fs.mkdirSync(path.join(root, "media"));
  const files = ["media/local-api.cjs", "media/local-terminal.cjs"].map(name => { const data = "// controlled helper"; fs.writeFileSync(path.join(root, name), data); return { path: name, sha256: createHash("sha256").update(data).digest("hex") }; });
  const manifest = path.join(root, "local-helper-manifest.json"); fs.writeFileSync(manifest, JSON.stringify({ schema: 1, files }));
  assert.equal((await verifyLocalHelpers(root)).integrity, "VERIFIED");
  fs.writeFileSync(path.join(root, files[0].path), "// modified"); await assert.rejects(verifyLocalHelpers(root), /mismatch/);
  fs.writeFileSync(manifest, JSON.stringify({ schema: 1, files: [files[1], files[1]] })); await assert.rejects(verifyLocalHelpers(root), /inventory/);
});
