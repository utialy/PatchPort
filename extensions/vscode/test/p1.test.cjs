const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { CoreClient, Ledger } = require("../dist/test/core");
const { dirtyConflicts, runtimeReport, resumeProfile, affectedPaths } = require("../dist/test/workflow");
const { restoreBoard, defaultBoard } = require("../dist/test/model");
const { StateStore } = require("../dist/test/stateStore");
const root = path.resolve(__dirname, "..");
const python = path.resolve(root, "../../.venv/Scripts/python.exe");

test("same core connection retains one-use plans; reconnection cannot replay them", async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "patchport-p1-")); t.after(() => fs.rm(dir, { recursive: true, force: true }));
  await fs.writeFile(path.join(dir, "input.txt"), "Original");
  const config = path.join(dir, "bridge.json");
  await fs.writeFile(config, JSON.stringify({ project: ".", include: ["input.txt"], writable: ["input.txt"], endpoints: { mock: { adapter: "command", command: [python, "-c", "print('MOCK')"] } } }));
  const core = new CoreClient(python, path.join(root, "core")); t.after(() => core.dispose());
  const inventory = await core.request("context.inventory", { config });
  const [capabilities, preview] = await Promise.all([core.request("capabilities"), core.request("batch.preview", { config, batch_id: "first", targets: ["mock"], prompt: "Read input", plan: { schema: 1, files: inventory.files.map(i => ({ path: i.path, mode: "full", reason: "Explicit" })) } })]);
  assert.equal(capabilities.workflow_schema, 1);
  assert.equal((await core.request("operation.apply", { plan_id: preview.plan_id, apply: true })).tasks.length, 1);
  await assert.rejects(core.request("operation.apply", { plan_id: preview.plan_id, apply: true }), /PLAN_EXPIRED/);
  const next = await core.request("batch.preview", { config, batch_id: "second", targets: ["mock"], prompt: "Read input", plan: { schema: 1, files: inventory.files.map(i => ({ path: i.path, mode: "full", reason: "Explicit" })) } });
  core.dispose();
  const reopened = new CoreClient(python, path.join(root, "core")); t.after(() => reopened.dispose());
  await assert.rejects(reopened.request("operation.apply", { plan_id: next.plan_id, apply: true }), /PLAN_EXPIRED/);
  assert.equal((await reopened.request("tasks.list", { config })).tasks.length, 1);
});

test("dirty file protection covers selected files, directories, proposal copies and summary sources", () => {
  const project = path.resolve(os.tmpdir(), "original"), proposal = path.resolve(os.tmpdir(), "copy");
  assert.deepEqual(dirtyConflicts([project, proposal], ["src"], [path.join(project, "src/a.py"), path.join(proposal, "src/a.py"), path.join(project, "src-other/a.py")]), [path.join(project, "src/a.py"), path.join(proposal, "src/a.py")]);
  const paths = affectedPaths("batch.preview", {}, { context: { selected: [{ path: "src/a.py" }], summaries: [{ record: { body_path: "summary.md", sources: [{ path: "history.md" }] } }] } });
  assert.ok(paths.includes("history.md"));
  assert.deepEqual(affectedPaths("roles.recover.preview", {}, { journal: { changes: [{ path: "src/a.py" }] } }), ["src/a.py"]);
});

test("creating the first batch queue preserves an already active project ledger across hosts", async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "patchport-ledger-bind-")); t.after(() => fs.rm(dir, { recursive: true, force: true }));
  await fs.writeFile(path.join(dir, "input.txt"), "Original");
  const config = path.join(dir, "bridge.json");
  await fs.writeFile(config, JSON.stringify({ project: ".", include: ["input.txt"], writable: ["input.txt"], endpoints: { mock: { adapter: "command", command: [python, "-c", "print('MOCK')"] } } }));
  const core = new CoreClient(python, path.join(root, "core")); t.after(() => core.dispose());
  const ledger = new Ledger(core, path.join(dir, "ledgers"));
  const first = await ledger.inspect(dir);
  await ledger.start({ id: "node", name: "Retained peer", project: dir, provider: "claude" });
  await ledger.record("node", "completed", { answer: "Preserved answer" }, "1");
  const inventory = await core.request("context.inventory", { config });
  const preview = await core.request("batch.preview", { config, batch_id: "first", targets: ["mock"], prompt: "Read input", plan: { schema: 1, files: inventory.files.map(i => ({ path: i.path, mode: "full", reason: "Explicit" })) } });
  await core.request("operation.apply", { plan_id: preview.plan_id, apply: true });
  assert.equal((await ledger.inspect(dir)).database, first.database);
  if (process.platform === "win32") assert.equal((await ledger.inspect(dir.toUpperCase())).database, first.database);
  const restored = new Ledger(core, path.join(dir, "ledgers"));
  assert.equal((await restored.inspect(dir)).database, first.database);
  assert.equal((await restored.report(dir)).events[0].data.answer, "Preserved answer");
  await ledger.end();
});

test("runtime manifest validates installed core and detects a damaged runtime", async t => {
  assert.equal((await runtimeReport(root)).integrity, "VERIFIED");
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "patchport-runtime-")); t.after(() => fs.rm(dir, { recursive: true, force: true }));
  await fs.cp(path.join(root, "core"), path.join(dir, "core"), { recursive: true });
  await fs.copyFile(path.join(root, "runtime-manifest.json"), path.join(dir, "runtime-manifest.json"));
  await fs.appendFile(path.join(dir, "core/agent_bridge/p1.py"), "\n# Corrupt fixture\n");
  await assert.rejects(runtimeReport(dir), /mismatch/);
});

test("explicit resume targets recorded UUID and preserves launch permissions", () => {
  const sid = "caa20871-3e56-4e86-a99e-443d43f36fa9";
  const profile = { id: "custom", name: "Peer", provider: "codex", command: "codex-custom", args: ["--sandbox", "read-only"], shell: "powershell" };
  assert.deepEqual(resumeProfile(profile, sid).args, ["--sandbox", "read-only", "resume", sid]);
  assert.deepEqual(resumeProfile({ ...profile, provider: "claude", args: [] }, sid).args, ["--resume", sid]);
  assert.throws(() => resumeProfile(profile, "--last"));
  assert.throws(() => resumeProfile({ ...profile, args: ["resume", sid] }, sid));
});

test("long session restore retains group round and consumed limit while pausing routes", () => {
  const board = defaultBoard(); board.groups[0].round = "saved-round"; board.groups[0].used = 8; board.groups[0].paused = false;
  const restored = restoreBoard(JSON.parse(JSON.stringify(board)), []);
  assert.equal(restored.groups[0].round, "saved-round"); assert.equal(restored.groups[0].used, 8); assert.equal(restored.groups[0].paused, true);
});

test("Windows host casing changes preserve restored cards and their project binding", { skip: process.platform !== "win32" }, () => {
  const board = defaultBoard(); const project = "D:\\Project\\Example";
  board.nodes.push({ id: "saved", name: "Peer", provider: "claude", profileId: "claude", project, groupId: board.groups[0].id, x: 0, y: 0 });
  const restored = restoreBoard(board, [project.toLowerCase()]);
  assert.equal(restored.nodes.length, 1); assert.equal(restored.nodes[0].project, project.toLowerCase());
});

test("durable board snapshot survives a new host and refuses stale or damaged writes", async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "patchport-board-state-")); t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const first = new StateStore(dir, [dir]); first.load();
  const board = defaultBoard(); board.groups[0].used = 7; board.groups[0].round = "saved-round";
  first.save(board, { [dir]: path.join(dir, "custom.json") }, "0.3.0");
  const restored = new StateStore(dir, [process.platform === "win32" ? dir.toUpperCase() : dir]);
  const snapshot = restored.load(); assert.equal(snapshot.board.groups[0].used, 7); assert.equal(snapshot.version, "0.3.0");
  snapshot.board.groups[0].used = 9; restored.save(snapshot.board, snapshot.configs, snapshot.version);
  assert.throws(() => first.save(board, {}, "0.3.0"), /Another window/);
  await fs.writeFile(restored.file, "{broken");
  const damaged = new StateStore(dir, [dir]); assert.throws(() => damaged.load()); assert.throws(() => damaged.save(board, {}, "0.3.0"), /corrupt/);
  assert.equal(await fs.readFile(restored.file, "utf8"), "{broken");
});
