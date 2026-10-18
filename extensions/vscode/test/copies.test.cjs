const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");
const { CoreClient } = require("../dist/test/core");
const { cleanupRoots, dirtyConflicts } = require("../dist/test/workflow");
const root = path.resolve(__dirname, "..");
const python = path.resolve(root, "../../.venv/Scripts/python.exe");

const prepare = String.raw`
import os, sys, time
from pathlib import Path
from agent_bridge.storage import Store, load_config
from agent_bridge import workspace
project = Path(sys.argv[1]).resolve()
config = load_config(project / 'bridge.json')
store = Store(config['state'])
old = time.time() - 60 * 86400
for name in ('a', 'b', 'c'):
    task = store.submit(name, ['mock'], 'Preserved request', config['endpoints'])[0]
    home = workspace.create(config, task)
    (home / 'answer.txt').write_text('Preserved answer', encoding='utf-8')
    store.claim(task)
    store.finish(task, 'DONE', {'workspace': str(home)})
    with store.connect() as db:
        db.execute('UPDATE tasks SET started=?,finished=? WHERE id=?', (old-1, old, task))
    for item in [*home.rglob('*'), home]: os.utime(item, (old, old))
`;

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "patchport-copies-"));
  t.after(() => { assert.ok(path.resolve(dir).startsWith(path.resolve(os.tmpdir()) + path.sep)); fs.rmSync(dir, { recursive: true, force: true }); });
  fs.writeFileSync(path.join(dir, "input.txt"), "Original");
  const config = path.join(dir, "bridge.json");
  fs.writeFileSync(config, JSON.stringify({ project: ".", include: ["input.txt"], writable: ["input.txt"], endpoints: { mock: { adapter: "command", command: ["never-run"] } } }));
  const result = cp.spawnSync(python, ["-B", "-X", "utf8", "-c", prepare, dir], { windowsHide: true, encoding: "utf8", env: { ...process.env, PYTHONPATH: path.join(root, "core") } });
  assert.equal(result.status, 0, result.stderr);
  const core = new CoreClient(python, path.join(root, "core")); t.after(() => core.dispose());
  return { dir, config, core, home: task => path.join(dir, ".agent-bridge/workspaces", task) };
}

test("dirty protection covers every selected copy and excludes unrelated copies", () => {
  const a = path.resolve(os.tmpdir(), "copies/a"), b = path.resolve(os.tmpdir(), "copies/b");
  const roots = cleanupRoots("copies.prune.preview", { items: [{ artifact: { path: a } }, { artifact: { path: b } }] });
  assert.deepEqual(roots, [a, b]);
  assert.deepEqual(dirtyConflicts(roots, ["."], [path.join(a, ".hidden/log"), path.join(b, "new-unsaved.txt"), a + "-other/file"]), [path.join(a, ".hidden/log"), path.join(b, "new-unsaved.txt")]);
  assert.deepEqual(cleanupRoots("roles.prune.preview", { workspace: b }), [b]);
  assert.deepEqual(cleanupRoots("batch.preview", { workspace: a }), []);
});

test("actual copy API pages without writes and deletes exactly the explicit selection", async t => {
  const { dir, config, core, home } = fixture(t);
  const database = path.join(dir, ".agent-bridge/queue.sqlite3"), before = fs.readFileSync(database);
  assert.equal((await core.request("capabilities")).copy_cleanup_schema, 1);
  const page = await core.request("copies.list", { config, limit: 2, days: 30 });
  assert.equal(page.items.length, 2); assert.equal(page.more, true);
  const next = await core.request("copies.list", { config, limit: 2, days: 30, cursor: page.next_cursor });
  assert.deepEqual(next.items.map(item => item.task_id), ["c--mock"]);
  const preview = await core.request("copies.prune.preview", { config, ids: ["a--mock"], days: 30 });
  assert.deepEqual(fs.readFileSync(database), before);
  assert.equal(fs.existsSync(path.join(dir, ".agent-bridge/runner.lock")), false);
  const applied = await core.request("operation.apply", { plan_id: preview.plan_id, apply: true });
  assert.equal(applied.tasks[0].deleted, true); assert.equal(fs.existsSync(home("a--mock")), false);
  assert.equal(fs.existsSync(home("b--mock")), true); assert.equal(fs.readFileSync(path.join(dir, "input.txt"), "utf8"), "Original");
  const result = await core.request("result", { config, task_id: "a--mock" });
  assert.equal(result.answer.text, "Preserved answer"); assert.equal(result.artifact_cleanup.state, "DELETED");
  assert.equal((await core.request("project.inspect", { project: dir })).control.control.calls_started, 3);
  await assert.rejects(core.request("operation.apply", { plan_id: preview.plan_id, apply: true }), /PLAN_EXPIRED/);
});

test("partial deletion exposes structured results through the actual core client", { skip: process.platform !== "win32" }, async t => {
  const { config, core, home } = fixture(t);
  const protectedFile = path.join(home("b--mock"), "project/input.txt");
  fs.chmodSync(protectedFile, 0o444);
  t.after(() => { if (fs.existsSync(protectedFile)) fs.chmodSync(protectedFile, 0o666); });
  const plan = await core.request("copies.prune.preview", { config, ids: ["a--mock", "b--mock"], days: 30 });
  assert.ok(plan.plan_id);
  await assert.rejects(core.request("operation.apply", { plan_id: plan.plan_id, apply: true }), error => {
    assert.match(error.message, /OPERATION_INCOMPLETE/);
    assert.deepEqual(error.result.tasks.map(item => item.reason), ["DELETED", "DELETE_FAILED"]);
    return true;
  });
  assert.equal(fs.existsSync(home("a--mock")), false); assert.equal(fs.existsSync(protectedFile), true);
  assert.equal((await core.request("result", { config, task_id: "b--mock" })).answer.text, "Preserved answer");
});
