const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const Module = require("node:module");
const esbuild = require("esbuild");
const { temporaryRoot, python } = require("./helpers.cjs");

const extensionRoot = path.resolve(__dirname, "..");
let compiled;
async function harness(t) {
  compiled ||= esbuild.build({ entryPoints: [path.join(extensionRoot, "src/extension.ts")], bundle: true, write: false, platform: "node", target: "node20", external: ["vscode", "node-pty"] });
  const source = (await compiled).outputFiles[0].text;
  const folder = fs.mkdtempSync(path.join(temporaryRoot, "patchport-local-controller-"));
  const project = path.join(folder, "project"); fs.mkdirSync(project);
  const workspace = new Map(), settings = new Map([["profiles", []], ["localProfiles", []]]), secrets = new Map();
  const updates = [], messages = [], prompts = [], secretCalls = [], configurationListeners = [];
  let receive, nativeToken = "mock-local-token-not-for-webview";
  const disposable = () => ({ dispose() {} });
  const fake = {
    ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 }, ViewColumn: { One: 1 },
    Uri: { joinPath: (uri, ...parts) => ({ fsPath: path.join(uri.fsPath, ...parts) }) },
    EventEmitter: class { constructor() { this.event = disposable; } fire() {} dispose() {} },
    TreeItem: class { constructor(label) { this.label = label; } }, ThemeIcon: class {},
    env: {}, commands: { registerCommand: disposable, executeCommand: async () => undefined },
    workspace: { workspaceFolders: [{ uri: { scheme: "file", fsPath: project } }], isTrusted: true, textDocuments: [],
      getConfiguration: () => ({ get: (key, fallback) => key === "corePython" ? python : settings.has(key) ? structuredClone(settings.get(key)) : fallback,
        update: async (key, value, target) => { settings.set(key, structuredClone(value)); updates.push({ key, value: structuredClone(value), target }); for (const callback of configurationListeners) callback({ affectsConfiguration: name => name === `patchport.${key}` }); } }),
      onDidChangeConfiguration: callback => { configurationListeners.push(callback); return disposable(); }, onDidGrantWorkspaceTrust: disposable },
    window: { terminals: [], tabGroups: { all: [] }, onDidOpenTerminal: disposable, onDidCloseTerminal: disposable, registerTreeDataProvider: disposable,
      showInputBox: async options => { prompts.push(options); return nativeToken; },
      showInformationMessage: async (...args) => args.filter(arg => typeof arg === "string").at(-1),
      showWarningMessage: async (...args) => args.filter(arg => typeof arg === "string").at(-1), showErrorMessage: async () => undefined,
      createWebviewPanel: () => ({ reveal() {}, dispose() {}, onDidDispose: disposable, onDidChangeViewState: disposable,
        webview: { html: "", cspSource: "mock-csp:", asWebviewUri: uri => uri.fsPath,
          postMessage: async message => { messages.push(structuredClone(message)); return true; }, onDidReceiveMessage: callback => { receive = callback; return disposable(); } } }) }
  };
  const context = () => ({ globalStorageUri: { fsPath: path.join(folder, "storage") }, extensionPath: extensionRoot,
    extension: { packageJSON: { version: "0.5.0" } }, extensionUri: { fsPath: extensionRoot }, subscriptions: [],
    workspaceState: { get: (key, fallback) => workspace.has(key) ? structuredClone(workspace.get(key)) : fallback, keys: () => [...workspace.keys()],
      update: async (key, value) => { if (value === undefined) workspace.delete(key); else workspace.set(key, structuredClone(value)); } },
    secrets: { get: async key => { secretCalls.push({ action: "get", key }); return secrets.get(key); },
      store: async (key, value) => { secretCalls.push({ action: "store", key }); secrets.set(key, value); },
      delete: async key => { secretCalls.push({ action: "delete", key }); secrets.delete(key); }, onDidChange: disposable }
  });
  const previousLoad = Module._load;
  const bundle = new Module(path.join(extensionRoot, "controller-test.fixture.cjs"), module);
  bundle.filename = path.join(extensionRoot, "controller-test.fixture.cjs"); bundle.paths = Module._nodeModulePaths(extensionRoot);
  Module._load = function(name, ...args) { return name === "vscode" ? fake : previousLoad.call(this, name, ...args); };
  try { bundle._compile(source, bundle.filename); } finally { Module._load = previousLoad; }
  const extension = bundle.exports;
  let ctx = context(), api = extension.activate(ctx), controller = ctx.subscriptions.find(item => item.router && item.host);
  api.openBoard();
  const result = { folder, project, settings, workspace, secrets, updates, messages, prompts, secretCalls, fake,
    get api() { return api; }, get controller() { return controller; },
    handle: message => controller.handle(message), receive: message => receive(message),
    setToken: value => { nativeToken = value; },
    restart: async () => { await extension.deactivate(); ctx = context(); api = extension.activate(ctx); controller = ctx.subscriptions.find(item => item.router && item.host); api.openBoard(); return controller; }
  };
  t.after(async () => {
    await extension.deactivate();
    assert.ok(path.resolve(folder).startsWith(path.resolve(temporaryRoot) + path.sep));
    fs.rmSync(folder, { recursive: true, force: true });
  });
  return result;
}

const profile = overrides => ({ id: "local-test", name: "Local review", backend: "lmstudio", endpointOrigin: "http://127.0.0.1:1234", modelId: "mock-model", contextWindow: 8192, maxOutputTokens: 1024, requestTimeoutSec: 300, executionLocationConfirmed: true, ...overrides });
const localSettings = f => f.settings.get("localProfiles");

test("controller stores local profiles globally with a new revision and rejects CLI collisions and unsafe input", async t => {
  const f = await harness(t);
  await f.handle({ type: "localProfileSave", profile: profile() });
  assert.equal(localSettings(f).length, 1);
  const first = localSettings(f)[0]; assert.ok(first.revision);
  assert.equal(f.updates.at(-1).key, "localProfiles"); assert.equal(f.updates.at(-1).target, f.fake.ConfigurationTarget.Global);
  assert.deepEqual(f.settings.get("profiles"), []);
  assert.equal(f.api.getState().profiles.find(item => item.id === first.id).provider, "local");
  assert.equal(f.messages.at(-1)?.type === "savedProfile" || f.messages.some(item => item.type === "savedProfile" && item.id === first.id), true);
  await f.handle({ type: "localProfileSave", profile: profile({ modelId: "next-model", revision: "untrusted-user-revision" }) });
  assert.notEqual(localSettings(f)[0].revision, first.revision);
  assert.notEqual(localSettings(f)[0].revision, "untrusted-user-revision");
  const saved = structuredClone(localSettings(f));
  for (const change of [{ endpointOrigin: "http://192.0.2.1:1234" }, { endpointOrigin: "http://token@127.0.0.1:1234" }, { endpointOrigin: "http://127.0.0.1:1234/v1" }, { token: "forbidden" }, { command: "never-run" }, { id: "codex" }]) {
    await assert.rejects(f.handle({ type: "localProfileSave", profile: profile(change) }));
    assert.deepEqual(localSettings(f), saved);
  }
  await assert.rejects(f.handle({ type: "profile", profile: { id: first.id, name: "Colliding CLI", provider: "codex", command: "codex", args: [], shell: "direct" } }));
  assert.deepEqual(f.settings.get("profiles"), []);
  f.fake.workspace.isTrusted = false;
  await assert.rejects(f.handle({ type: "localProfileSave", profile: profile() }));
  assert.deepEqual(localSettings(f), saved);
});

test("controller binds native secret input to profile and origin without exposing token contents", async t => {
  const f = await harness(t), value = "mock-local-token-not-for-webview";
  await f.handle({ type: "localProfileSave", profile: profile() });
  await f.handle({ type: "localTokenSet", id: "local-test" });
  assert.equal(f.prompts.at(-1).password, true);
  assert.equal(f.secrets.size, 1);
  const firstKey = [...f.secrets.keys()][0]; assert.equal(f.secrets.get(firstKey), value);
  assert.equal(firstKey.includes("127.0.0.1"), false);
  assert.doesNotMatch(JSON.stringify(f.api.getState()), /mock-local-token-not-for-webview/);
  assert.doesNotMatch(JSON.stringify(f.messages), /mock-local-token-not-for-webview/);
  assert.doesNotMatch(JSON.stringify(f.updates), /mock-local-token-not-for-webview/);
  await f.handle({ type: "localProfileSave", profile: profile({ endpointOrigin: "http://127.0.0.1:1235" }) });
  assert.equal(f.secrets.has(firstKey), false);
  await f.handle({ type: "localTokenSet", id: "local-test" });
  const secondKey = [...f.secrets.keys()][0]; assert.notEqual(secondKey, firstKey);
  await f.handle({ type: "localProfileSave", profile: profile({ id: "local-other", endpointOrigin: "http://127.0.0.1:1235" }) });
  await f.handle({ type: "localTokenSet", id: "local-other" });
  assert.equal(f.secrets.size, 2);
  await f.handle({ type: "localTokenDelete", id: "local-test" });
  assert.equal(f.secrets.has(secondKey), false); assert.equal(f.secrets.size, 1);
  await f.handle({ type: "localProfileDelete", id: "local-other" });
  assert.equal(f.secrets.size, 0);
  assert.equal(localSettings(f).some(item => item.id === "local-other"), false);
  f.setToken(undefined);
  await f.handle({ type: "localTokenSet", id: "local-test" });
  assert.equal(f.secrets.size, 0);
});

test("controller probes only the requested model list and never sends credentials to an edited origin", async t => {
  const f = await harness(t), seen = [];
  const server = http.createServer((request, response) => {
    seen.push({ method: request.method, path: request.url, authorization: request.headers.authorization });
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify(request.url === "/api/tags" ? { models: [{ name: "mock-model" }] } : { data: [{ id: "mock-model", type: "llm" }] }));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const origin = `http://127.0.0.1:${server.address().port}`;
  await f.handle({ type: "localProbe", backend: "ollama", endpointOrigin: origin });
  assert.deepEqual(seen, [{ method: "GET", path: "/api/tags", authorization: undefined }]);
  assert.equal(f.api.getState().localProbe.state, "ready");
  assert.equal(f.api.getState().localProbe.models[0].modelId, "mock-model");
  await f.handle({ type: "localProfileSave", profile: profile({ endpointOrigin: origin }) });
  await f.handle({ type: "localTokenSet", id: "local-test" });
  await f.handle({ type: "localProbe", id: "local-test", backend: "lmstudio", endpointOrigin: origin });
  assert.equal(seen.at(-1).authorization, "Bearer mock-local-token-not-for-webview");
  await f.handle({ type: "localProbe", backend: "lmstudio", endpointOrigin: origin });
  assert.equal(seen.at(-1).authorization, undefined);
  const before = seen.length;
  await f.handle({ type: "localProbe", id: "local-test", backend: "ollama", endpointOrigin: origin });
  assert.equal(seen.length, before + 1); assert.equal(seen.at(-1).authorization, undefined);
  await assert.rejects(f.handle({ type: "localProbe", backend: "ollama", endpointOrigin: "http://192.0.2.1:1234" }));
  assert.equal(seen.length, before + 1);
  assert.equal(seen.every(item => item.method === "GET"), true);
  assert.equal(f.controller.host.sessions.size, 0);
  assert.doesNotMatch(JSON.stringify(f.messages), /mock-local-token-not-for-webview/);
});

test("native token entry rejects invalid or stale input without publishing it to the webview", async t => {
  const f = await harness(t);
  await f.handle({ type: "localProfileSave", profile: profile() });
  f.setToken("private token\ninvalid");
  await assert.rejects(f.handle({ type: "localTokenSet", id: "local-test" }));
  assert.equal(f.secrets.size, 0);
  f.fake.window.showInputBox = async () => {
    await f.handle({ type: "localProfileSave", profile: profile({ endpointOrigin: "http://127.0.0.1:1235" }) });
    return "stale-private-token";
  };
  await assert.rejects(f.handle({ type: "localTokenSet", id: "local-test" }));
  assert.equal(f.secrets.size, 0);
  assert.doesNotMatch(JSON.stringify(f.messages), /stale-private-token|private token/);
  f.fake.workspace.isTrusted = false;
  await assert.rejects(f.handle({ type: "localTokenSet", id: "local-test" }));
  await assert.rejects(f.handle({ type: "localTokenDelete", id: "local-test" }));
  await assert.rejects(f.handle({ type: "localProbe", backend: "ollama", endpointOrigin: "http://127.0.0.1:11434" }));
});

test("ambiguous configured local IDs cannot replace existing CLI profiles or start by last-value selection", async t => {
  const f = await harness(t);
  f.settings.set("localProfiles", [profile({ revision: "one" }), profile({ revision: "two", modelId: "different-model" }), profile({ id: "codex", revision: "three" })]);
  const profiles = f.api.getState().profiles;
  assert.equal(profiles.some(item => item.id === "local-test"), false);
  assert.equal(profiles.find(item => item.id === "codex").provider, "codex");
  await assert.rejects(f.controller.add("local-test", f.project, f.controller.router.board.groups[0].id));
  assert.equal(f.controller.host.sessions.size, 0);
});

test("malformed profile collection settings do not prevent the board from opening or discard valid peers", async t => {
  const f = await harness(t), configuredLocal = profile({ revision: "valid-revision" });
  for (const invalid of [null, {}, "invalid-array"]) {
    f.settings.set("profiles", invalid); f.settings.set("localProfiles", [configuredLocal]);
    let state = f.api.getState();
    assert.equal(state.profiles.find(item => item.id === "codex").provider, "codex");
    assert.equal(state.profiles.find(item => item.id === configuredLocal.id).provider, "local");
    assert.deepEqual(f.settings.get("profiles"), invalid);
    f.settings.set("localProfiles", invalid);
    state = f.api.getState();
    assert.ok(state.profiles.some(item => item.id === "codex"));
    assert.equal(state.profiles.some(item => item.provider === "local"), false);
    assert.deepEqual(f.settings.get("localProfiles"), invalid);
  }
  assert.deepEqual(f.updates, []);
  await f.restart();
  assert.ok(f.api.getState().profiles.some(item => item.id === "codex"));
});

test("active local settings stay frozen after editing and new conversation preserves group limits", async t => {
  const f = await harness(t);
  await f.handle({ type: "localProfileSave", profile: profile() });
  const controller = f.controller, initial = f.api.getState().profiles.find(item => item.id === "local-test");
  const group = controller.router.board.groups[0]; group.used = 3; group.round = "retained-round"; group.paused = true;
  const node = { id: "node-local", name: "Local review", provider: "local", profileId: initial.id, project: f.project, groupId: group.id, x: 0, y: 0 };
  controller.router.board.nodes.push(node); controller.router.board.version = 2;
  const runtime = { id: node.id, phase: "ready", dirty: false, focused: false, lastMessage: "", reason: "", generation: "old-generation", local: structuredClone(initial.local) };
  controller.router.runtime.set(node.id, runtime);
  controller.host.sessions.set(node.id, { profile: initial });
  t.after(() => controller.host.sessions.clear());
  await f.handle({ type: "localProfileSave", profile: profile({ modelId: "next-model" }) });
  assert.equal(f.api.getState().runtime.find(item => item.id === node.id).local.modelId, "mock-model");
  assert.equal(f.api.getState().profiles.find(item => item.id === initial.id).local.modelId, "next-model");
  const transitions = [];
  let helperIdle = false;
  controller.host.local.idle = () => helperIdle;
  controller.host.stop = id => { transitions.push(["stop", id]); controller.host.sessions.delete(id); };
  controller.host.waitStopped = async id => { transitions.push(["waitStopped", id]); };
  controller.startNode = async (selected, next) => { transitions.push(["start", selected.id, structuredClone(next)]); };
  runtime.localRequest = { requestId: "pending-request", generation: runtime.generation, origin: "human", root: "root", hops: 0, authorized: false, dispatchPending: false };
  await assert.rejects(f.handle({ type: "localNewConversation", id: node.id }));
  runtime.localRequest = undefined; runtime.dirty = true;
  await assert.rejects(f.handle({ type: "localNewConversation", id: node.id }));
  assert.deepEqual(transitions, []);
  runtime.dirty = false;
  await assert.rejects(f.handle({ type: "localNewConversation", id: node.id }));
  helperIdle = true;
  await f.handle({ type: "localNewConversation", id: node.id });
  assert.deepEqual(transitions.map(item => item[0]), ["stop", "waitStopped", "start"]);
  assert.equal(transitions[2][2].local.modelId, "next-model");
  assert.equal(group.used, 3); assert.equal(group.round, "retained-round"); assert.equal(group.paused, true);
});

test("controller reload preserves schema 3 local cards, paused groups and protected legacy mirrors", async t => {
  const f = await harness(t);
  await f.handle({ type: "localProfileSave", profile: profile() });
  const group = f.controller.router.board.groups[0]; group.used = 5; group.round = "saved-round"; group.paused = false;
  f.controller.router.board.nodes.push({ id: "saved-local", name: "Local card", provider: "local", profileId: "local-test", project: f.project, groupId: group.id, x: 20, y: 40 });
  f.controller.router.board.version = 2;
  f.controller.saveState(); await f.controller.mirrorChain;
  const file = f.controller.stateStore.file;
  assert.equal(JSON.parse(fs.readFileSync(file)).schema, 3);
  assert.equal(f.workspace.get("board.schema"), 3); assert.equal(f.workspace.get("board.v2").version, 2);
  assert.equal(f.workspace.get("board.v1").version, 2);
  await f.restart();
  const restored = f.api.getState();
  assert.equal(restored.board.nodes[0].provider, "local"); assert.equal(restored.board.nodes[0].id, "saved-local");
  assert.equal(restored.board.groups[0].used, 5); assert.equal(restored.board.groups[0].round, "saved-round");
  assert.equal(restored.board.groups[0].paused, true); assert.deepEqual(restored.active, []);
  assert.equal(f.controller.host.sessions.size, 0);
  await f.handle({ type: "localProfileDelete", id: "local-test" });
  assert.equal(f.api.getState().board.nodes[0].id, "saved-local");
  await assert.rejects(f.handle({ type: "start", id: "saved-local" }));
});
