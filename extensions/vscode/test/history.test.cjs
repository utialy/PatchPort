const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { CoreClient, Ledger } = require("../dist/test/core");
const { Router, defaultBoard } = require("../dist/test/model");
const { SessionHost } = require("../dist/test/session");
const root = path.resolve(__dirname, ".."), python = path.resolve(root, "../../.venv/Scripts/python.exe");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const wait = async fn => { for (let i = 0; i < 200; i++) { if (fn()) return; await delay(25); } throw new Error("Timed out"); };

async function fixture(t) {
  const project = await fs.mkdtemp(path.join(os.tmpdir(), "patchport-history-"));
  const core = new CoreClient(python, path.join(root, "core")), observer = new CoreClient(python, path.join(root, "core"));
  const ledger = new Ledger(core, path.join(project, "ledgers"));
  const binding = await ledger.inspect(project);
  t.after(async () => { await ledger.end(); core.dispose(); observer.dispose(); await delay(150); await fs.rm(project, { recursive: true, force: true }); });
  const scope = { project, database: binding.database };
  const node = id => ({ id, name: "PRIVATE_NAME", project, provider: "claude", profileId: id, groupId: "team", x: 0, y: 0 });
  return { project, core, observer, ledger, scope, node };
}

test("writer pipe remains pinned while idle; paired purge preserves usage and invalidates resume", async t => {
  const f = await fixture(t);
  const ids = [await f.ledger.start(f.node("a")), await f.ledger.start(f.node("b"))];
  const epoch = f.core.epoch;
  await delay(2300);
  await f.ledger.record("a", "completed", { answer: "PRIVATE_BODY" });
  assert.equal(f.core.epoch, epoch);
  await f.ledger.record("a", "usage", { sequence: 1, metrics: { input_tokens: 12 } });
  await f.ledger.delivery({ id: "delivery", source: "a", target: "b", text: "PRIVATE_DELIVERY", status: "sent", groupId: "team", round: "round", hops: 1, root: "root" });
  assert.ok((await f.observer.request("history.purge.preview", { ...f.scope, session_ids: ids })).report.blockers.includes("WRITER_ACTIVE"));
  await f.ledger.finish("a", { confirmed_exit: true }); await f.ledger.finish("b", { confirmed_exit: true });
  const half = await f.observer.request("history.purge.preview", { ...f.scope, session_ids: [ids[0]] });
  assert.deepEqual(half.report.required_sessions, [ids[1]]);
  const before = await f.ledger.report(f.project);
  const preview = await f.observer.request("history.purge.preview", { ...f.scope, session_ids: ids });
  const result = await f.observer.request("history.apply", { plan_id: preview.plan_id, apply: true });
  assert.equal(result.sessions, 2); assert.equal(result.deliveries, 1);
  assert.deepEqual(await f.observer.request("history.operation", { ...f.scope, operation: preview.plan_id }), { found: true, ...result });
  const after = await f.ledger.report(f.project);
  assert.deepEqual(after.summary, before.summary); assert.deepEqual(after.group_attempts, before.group_attempts);
  assert.equal(JSON.stringify(after).includes("PRIVATE"), false);
  await assert.rejects(f.core.request("interactive.resume", { ...f.scope, session_id: ids[0] }), /BODY_DELETED/);
});

test("lost writer pipe cannot silently reconnect or regain deletion eligibility", async t => {
  const f = await fixture(t), id = await f.ledger.start(f.node("a"));
  f.core.dispose(); await delay(150);
  await assert.rejects(f.ledger.record("a", "completed", { answer: "late" }), /lost/);
  const preview = await f.observer.request("history.purge.preview", { ...f.scope, session_ids: [id] });
  assert.equal(preview.plan_id, null); assert.ok(preview.report.blockers.includes("WRITER_LOST"));
  const report = await f.observer.request("interactive.report", f.scope);
  assert.equal(report.events.length, 0); assert.equal(report.sessions[0].state, "UNCERTAIN");
});

test("lost apply response is resolved by a receipt on a new pipe without replaying the mutation", async t => {
  const f = await fixture(t), id = await f.ledger.start(f.node("a"));
  await f.ledger.record("a", "completed", { answer: "PRIVATE" }); await f.ledger.finish("a", { confirmed_exit: true });
  const plan = await f.observer.request("history.purge.preview", { ...f.scope, session_ids: [id] });
  const exchange = f.observer.exchange.bind(f.observer);
  f.observer.exchange = async (action, args) => { const result = await exchange(action, args); if (action === "history.apply") throw new Error("Injected lost response"); return result; };
  await assert.rejects(f.observer.request("history.apply", { plan_id: plan.plan_id, apply: true }), /lost response/);
  f.observer.dispose();
  const receipt = await f.core.request("history.operation", { ...f.scope, operation: plan.plan_id });
  assert.equal(receipt.found, true); assert.equal(receipt.sessions, 1);
  await assert.rejects(f.core.request("history.apply", { plan_id: plan.plan_id, apply: true }), /PLAN_EXPIRED/);
  assert.equal((await f.core.request("history.list", f.scope)).items[0].body_deleted, true);
});

test("existing schema one requires an explicit preparation and preserves legacy bodies", async t => {
  const f = await fixture(t);
  await f.core.request("interactive.initialize", { database: f.scope.database, apply: true });
  await assert.rejects(f.ledger.start(f.node("a")), /UPGRADE_REQUIRED/);
  const plan = await f.core.request("history.prepare.preview", f.scope);
  await f.core.request("history.apply", { plan_id: plan.plan_id, apply: true });
  await f.ledger.start(f.node("a")); await f.ledger.finish("a", { confirmed_exit: true });
  assert.equal((await f.core.request("history.list", f.scope)).items[0].selectable, true);
});

test("kill return is insufficient; actual mock PTY exit closes the owner before purge", { timeout: 30000 }, async t => {
  const f = await fixture(t), board = defaultBoard(), node = { ...f.node("terminal"), provider: "terminal" }; board.nodes = [node];
  const router = new Router(board, () => {}), host = new SessionHost(router, path.join(f.project, "runtime"), root, f.ledger);
  t.after(() => host.shutdown());
  await host.start(node, { id: "terminal", name: "Fixture", provider: "terminal", command: process.execPath, args: ["-e", "console.log(\"PRIVATE_PTY\");setInterval(()=>{},1000)"], shell: "direct" });
  const running = host.sessions.get(node.id), id = f.ledger.sessions.get(node.id).id;
  const realKill = running.process.kill.bind(running.process); running.process.kill = () => {};
  host.stop(node.id);
  assert.equal(host.isActive(node.id), true);
  assert.ok((await f.observer.request("history.purge.preview", { ...f.scope, session_ids: [id] })).report.blockers.includes("WRITER_ACTIVE"));
  realKill(); await wait(() => !host.isActive(node.id) && !f.ledger.isBusy(f.project));
  const plan = await f.observer.request("history.purge.preview", { ...f.scope, session_ids: [id] });
  assert.equal(plan.report.eligible, true);
  const report = await f.ledger.report(f.project);
  assert.equal(report.events.find(e => e.kind === "session_stopped").data.confirmed_exit, true);
  await f.observer.request("history.apply", { plan_id: plan.plan_id, apply: true });
});
