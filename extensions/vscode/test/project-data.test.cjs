const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { defaultBoard, projectKey, restoreBoard } = require("../dist/test/model");
const { StateStore } = require("../dist/test/stateStore");
const { changedProject, planProject, validateDataPlan } = require("../dist/test/projectData");
const { CoreClient, Ledger } = require("../dist/test/core");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "patchport-data-"));
  t.after(() => { assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep)); fs.rmSync(root, { recursive: true, force: true }); });
  const a = path.join(root, "a"), b = path.join(root, "b"); fs.mkdirSync(a); fs.mkdirSync(b);
  const board = defaultBoard(); Object.assign(board.groups[0], { used: 7, round: "preserved-round", goal: "Shared goal", maxDeliveries: 11, paused: true });
  board.nodes = [a, b].map((project, index) => ({ id: String(index), name: "Peer", project, provider: "claude", profileId: "claude", groupId: "team", x: 0, y: 0 }));
  board.edges = [{ id: "cross", source: "0", target: "1", enabled: true }];
  const configs = { [projectKey(a)]: path.join(a, "custom.json"), [projectKey(b)]: path.join(b, "bridge.json") };
  return { root, a, b, board, configs };
}

test("reset removes only the selected project cards and incident edges, preserving shared limits", t => {
  const f = fixture(t), before = structuredClone(f.board);
  const next = changedProject(f.board, f.configs, f.a, "reset_board");
  assert.deepEqual(next.board.nodes, [before.nodes[1]]); assert.deepEqual(next.removed, ["0"]);
  assert.deepEqual(next.board.groups, before.groups); assert.deepEqual(next.configs, f.configs);
  assert.deepEqual(f.board, before); assert.deepEqual(next.board.edges, []);
});

test("explicit disconnection is persistent while default selection removes only the selected override", t => {
  const f = fixture(t), store = new StateStore(f.root, [f.a, f.b]); store.load();
  const next = changedProject(f.board, f.configs, f.a, "disconnect"); store.save(next.board, next.configs, "0.3.0");
  const snapshot = new StateStore(f.root, [f.b, f.a]).load();
  assert.equal(snapshot.schema, 2); assert.equal(snapshot.configs[projectKey(f.a)], null);
  assert.equal(snapshot.configs[projectKey(f.b)], f.configs[projectKey(f.b)]);
  const defaults = changedProject(snapshot.board, snapshot.configs, f.a, "reset_config");
  assert.equal(Object.hasOwn(defaults.configs, projectKey(f.a)), false); assert.deepEqual(defaults.board, f.board);
  const selected = changedProject(defaults.board, defaults.configs, f.a, "select_config", path.join(f.a, "new.json"));
  assert.equal(selected.configs[projectKey(f.a)], path.join(f.a, "new.json"));
});

test("management snapshot survives mirror failure and requires explicit completion after restart", t => {
  const f = fixture(t), store = new StateStore(f.root, [f.a, f.b]); store.load(); store.save(f.board, f.configs, "0.3.0");
  const next = changedProject(f.board, f.configs, f.a, "reset_board");
  store.beginManagement(next.board, next.configs, "0.3.0", { id: "operation", project: f.a, action: "reset_board", removed: next.removed });
  assert.throws(() => store.save(f.board, f.configs, "0.3.0"), /[Ff]in(alization|ish)/);
  const restarted = new StateStore(f.root, [f.a, f.b]), saved = restarted.load();
  assert.equal(restarted.pending.id, "operation"); assert.deepEqual(saved.board.nodes.map(n => n.id), ["1"]);
  assert.throws(() => restarted.finishManagement("different"), /ID/);
  restarted.finishManagement("operation"); assert.equal(restarted.pending, undefined);
  assert.deepEqual(restoreBoard(new StateStore(f.root, [f.a, f.b]).load().board, [f.a, f.b]).nodes.map(n => n.id), ["1"]);
});

test("stale window cannot restore a cleared project after a management operation", t => {
  const f = fixture(t), first = new StateStore(f.root, [f.a]); first.load(); first.save(f.board, f.configs, "0.3.0");
  const stale = new StateStore(f.root, [f.a]); stale.load();
  const next = changedProject(f.board, f.configs, f.a, "disconnect");
  first.beginManagement(next.board, next.configs, "0.3.0", { id: "detach", project: f.a, action: "disconnect", removed: [] });
  first.finishManagement("detach");
  assert.throws(() => stale.save(f.board, f.configs, "0.3.0"), /Another window/);
  assert.equal(new StateStore(f.root, [f.a]).load().configs[projectKey(f.a)], null);
});

test("management operations on one workspace do not change another snapshot", t => {
  const f = fixture(t), single = new StateStore(f.root, [f.a]), multi = new StateStore(f.root, [f.a, f.b]);
  single.load(); multi.load(); single.save(f.board, f.configs, "0.3.0"); multi.save(f.board, f.configs, "0.3.0");
  const before = fs.readFileSync(multi.file);
  const next = changedProject(f.board, f.configs, f.a, "reset_board"); single.save(next.board, next.configs, "0.3.0");
  assert.deepEqual(fs.readFileSync(multi.file), before);
});

test("preview rejects changed inputs, changed inventory and expiry", t => {
  const f = fixture(t), plan = planProject(f.board, f.configs, f.a, "reset_board", "revision");
  validateDataPlan(plan, f.board, f.configs, "revision");
  assert.throws(() => validateDataPlan(plan, f.board, f.configs, "changed"), /changed/);
  const board = structuredClone(f.board); board.groups[0].used++;
  assert.throws(() => validateDataPlan(plan, board, f.configs, "revision"), /changed/);
  plan.expires = 0; assert.throws(() => validateDataPlan(plan, f.board, f.configs, "revision"), /expired/);
  assert.throws(() => changedProject(f.board, f.configs, f.a, "select_config", "relative"));
});

test("schema 1 reads migrate without resetting control data and corrupt state is preserved", t => {
  const f = fixture(t), store = new StateStore(f.root, [f.a]);
  fs.mkdirSync(path.dirname(store.file), { recursive: true });
  fs.writeFileSync(store.file, JSON.stringify({ schema: 1, board: f.board, configs: f.configs, version: "0.3.0" }));
  const previous = store.load(); store.save(previous.board, previous.configs, "0.3.0");
  assert.equal(new StateStore(f.root, [f.a]).load().board.groups[0].used, 7);
  fs.writeFileSync(store.file, "{broken"); const broken = new StateStore(f.root, [f.a]);
  assert.throws(() => broken.load()); assert.throws(() => broken.beginManagement(f.board, {}, "0.3.0", { id: "bad", project: f.a, action: "disconnect", removed: [] }));
  assert.equal(fs.readFileSync(store.file, "utf8"), "{broken");
});

test("failed snapshot publication leaves the previous snapshot intact", t => {
  const f = fixture(t), store = new StateStore(f.root, [f.a]); store.load(); store.save(f.board, f.configs, "0.3.0");
  const before = fs.readFileSync(store.file), rename = fs.renameSync;
  fs.renameSync = () => { throw new Error("Injected rename failure"); };
  try { assert.throws(() => store.beginManagement(f.board, {}, "0.3.0", { id: "bad", project: f.a, action: "disconnect", removed: [] }), /Injected/); }
  finally { fs.renameSync = rename; }
  assert.deepEqual(fs.readFileSync(store.file), before); assert.equal(store.pending, undefined);
});

test("inventory through the actual core pipe preserves separate fallback and batch databases", async t => {
  const f = fixture(t), root = path.resolve(__dirname, "..");
  fs.writeFileSync(path.join(f.a, "input.txt"), "input");
  fs.writeFileSync(path.join(f.a, "bridge.json"), JSON.stringify({ project: ".", include: ["input.txt"], writable: ["input.txt"], endpoints: { mock: { adapter: "command", command: ["never-run"] } } }));
  const core = new CoreClient(path.resolve(root, "../../.venv/Scripts/python.exe"), path.join(root, "core")); t.after(() => core.dispose());
  const ledger = new Ledger(core, path.join(f.root, "ledgers"));
  await ledger.inspect(f.a); await ledger.start(f.board.nodes[0]);
  assert.throws(() => ledger.forget(f.a), /shutdown/);
  await ledger.finish("0");
  const candidates = await ledger.candidates(f.a), database = ledger.binding(f.a).database;
  const before = fs.readFileSync(database);
  const report = await core.request("project.data.inspect", { project: f.a, databases: candidates });
  assert.equal(report.schema, 1); assert.equal(report.blockers.length, 0);
  assert.equal(report.databases.find(item => item.path.toLowerCase() === fs.realpathSync.native(database).toLowerCase()).interactive.sessions, 1);
  assert.deepEqual(fs.readFileSync(database), before); assert.equal(fs.existsSync(path.join(f.a, ".agent-bridge")), false);
  ledger.forget(f.a); assert.equal(ledger.binding(f.a), undefined); assert.deepEqual(fs.readFileSync(database), before);
});

test("controller restart after a mirror failure preserves disconnection and explicitly completes the mirror", async t => {
  const f = fixture(t), root = path.resolve(__dirname, ".."), Module = require("node:module");
  for (const project of [f.a, f.b]) {
    fs.writeFileSync(path.join(project, "input.txt"), "Input");
    const config = JSON.stringify({ project: ".", include: ["input.txt"], writable: ["input.txt"], endpoints: { mock: { adapter: "command", command: ["never-run"] } } });
    fs.writeFileSync(path.join(project, "bridge.json"), config); fs.writeFileSync(path.join(project, "custom.json"), config);
  }
  const values = new Map([["board.v1", f.board], ...Object.entries(f.configs).map(([project, config]) => [`core.config.${project}`, config])]);
  let failMirror = true;
  let receive, confirmations = 0;
  const disposable = () => ({ dispose() {} });
  const fake = {
    ViewColumn: { One: 1 }, Uri: { joinPath: (uri, ...parts) => ({ fsPath: path.join(uri.fsPath, ...parts) }) },
    EventEmitter: class { constructor() { this.event = () => disposable(); } fire() {} dispose() {} },
    env: {}, commands: { registerCommand: disposable },
    workspace: { workspaceFolders: [f.a, f.b].map(fsPath => ({ uri: { scheme: "file", fsPath } })), isTrusted: true, textDocuments: [],
      getConfiguration: () => ({ get: (key, fallback) => key === "corePython" ? path.resolve(root, "../../.venv/Scripts/python.exe") : fallback }),
      onDidChangeConfiguration: disposable, onDidGrantWorkspaceTrust: disposable },
    window: { terminals: [], tabGroups: { all: [] }, onDidOpenTerminal: disposable, onDidCloseTerminal: disposable, registerTreeDataProvider: disposable,
      showOpenDialog: async () => undefined, showWarningMessage: async () => { confirmations++; return "\uc801\uc6a9"; },
      createWebviewPanel: () => ({ reveal() {}, dispose() {}, onDidDispose: disposable, onDidChangeViewState: disposable,
        webview: { html: "", asWebviewUri: uri => uri.fsPath, postMessage: async () => true, onDidReceiveMessage: callback => { receive = callback; return disposable(); } } }) }
  };
  const context = () => ({ globalStorageUri: { fsPath: path.join(f.root, "storage") }, extensionPath: root,
    extension: { packageJSON: { version: "0.3.0" } }, extensionUri: { fsPath: root }, subscriptions: [],
    workspaceState: { get: key => values.get(key), keys: () => [...values.keys()], update: async (key, value) => {
      if (failMirror && key === "core.settings.v2") throw new Error("Injected mirror failure");
      if (value === undefined) values.delete(key); else values.set(key, structuredClone(value));
    } }
  });
  const previous = Module._load;
  Module._load = function(name, ...args) { return name === "vscode" ? fake : previous.call(this, name, ...args); };
  let extension;
  try { delete require.cache[require.resolve("../dist/extension.js")]; extension = require("../dist/extension.js"); }
  finally { Module._load = previous; }
  const api = extension.activate(context());
  await api.previewProjectData(f.a, "disconnect");
  await assert.rejects(api.applyProjectData(), /Injected mirror failure/);
  assert.equal(api.getState().projectData.pending.stage, "MIRROR_PENDING");
  extension.deactivate(); await new Promise(resolve => setTimeout(resolve, 30));
  const restarted = extension.activate(context()); t.after(() => extension.deactivate());
  assert.deepEqual(restarted.getState().detachedProjects, [f.a]);
  await assert.rejects(restarted.previewProjectData(f.a, "reset_config"), /[Ff]in(alization|ish)/);
  failMirror = false; await restarted.resumeProjectData();
  assert.equal(restarted.getState().projectData.pending, undefined);
  assert.equal(values.get("core.settings.v2")[projectKey(f.a)], null);
  assert.equal(values.has(`core.config.${projectKey(f.a)}`), false);
  assert.equal(values.get(`core.config.${projectKey(f.b)}`), f.configs[projectKey(f.b)]);
  await restarted.refreshProject(f.a); assert.equal(restarted.getState().core.state, "detached");
  assert.equal(fs.existsSync(path.join(f.a, ".agent-bridge")), false);
  await restarted.previewProjectData(f.b, "disconnect"); restarted.openBoard();
  receive({ type: "coreConfig", project: f.a });
  await new Promise(resolve => setTimeout(resolve, 80));
  assert.equal(confirmations, 0); assert.equal(restarted.getState().projectData.plan, undefined);
  assert.deepEqual(restarted.getState().detachedProjects, [f.a]);
  const inventory = await restarted.workflow(f.b, "context.inventory");
  await restarted.workflow(f.b, "batch.preview", { batch_id: "confirmed-before-refresh", targets: ["mock"], prompt: "Queue only", plan: { schema: 1, files: inventory.files.map(item => ({ path: item.path, mode: "full", reason: "Explicit fixture" })) } });
  const applying = restarted.applyWorkflow(), folders = fake.workspace.workspaceFolders;
  fake.workspace.workspaceFolders = [];
  const confirmed = await applying;
  fake.workspace.workspaceFolders = folders;
  assert.equal(confirmed.submitted, true); assert.equal(restarted.getState().workflow.result.submitted, true);
  extension.deactivate(); await new Promise(resolve => setTimeout(resolve, 30));
  const stateFile = new StateStore(path.join(f.root, "storage"), [f.a, f.b]).file;
  fs.writeFileSync(stateFile, "{broken");
  const broken = extension.activate(context());
  await assert.rejects(broken.workflow(f.b, "context.inventory"), /board/);
  assert.equal(fs.readFileSync(stateFile, "utf8"), "{broken");
});
