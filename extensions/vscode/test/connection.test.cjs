const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { Router, defaultBoard } = require("../dist/test/model");
const { SessionHost } = require("../dist/test/session");

async function fixture(run) {
  const project = path.resolve(__dirname, "../../../.bridge/vscode-connection-20261004/mock");
  const board = defaultBoard(); board.nodes = ["a", "b"].map(id => ({ id, name: `Claude ${id}`, provider: "claude", profileId: id, project, groupId: "team", x: 0, y: 0 }));
  const writes = []; const router = new Router(board, (id, text) => writes.push({ id, text }));
  const host = new SessionHost(router, project, path.resolve(__dirname, ".."));
  await host.initialize();
  for (const node of board.nodes) {
    router.started(node.id); router.ready(node.id, true);
    host.sessions.set(node.id, { token: "private-test-token", profile: { provider: "claude" }, node, turn: 0, disposed: false, process: { kill() {} }, screen: { dispose() {} } });
  }
  const hook = (id, name) => new Promise((resolve, reject) => {
    const req = http.request(`http://127.0.0.1:${host.port}/events/${id}`, { method: "POST", headers: { Authorization: "Bearer private-test-token" } }, res => { let body = ""; res.on("data", c => body += c); res.on("end", () => resolve(JSON.parse(body))); });
    req.on("error", reject); req.end(JSON.stringify({ session_id: `session-${id}`, cwd: project, hook_event_name: name, prompt: "Talk to the connected peer." }));
  });
  try { await run({ board, router, host, writes, hook }); } finally { host.dispose(); }
}
test("Claude receives current peer context even when connected after startup", () => fixture(async ({ router, hook }) => {
  const start = await hook("a", "SessionStart"); assert.match(start.hookSpecificOutput.additionalContext, /"peers":\[\]/);
  router.connect("a", "b"); router.connect("b", "a");
  const submitted = await hook("a", "UserPromptSubmit");
  const context = submitted.hookSpecificOutput.additionalContext;
  assert.equal(submitted.hookSpecificOutput.hookEventName, "UserPromptSubmit");
  assert.match(context, /Claude b/); assert.match(context, /independent CLI processes/); assert.match(context, /native-session discovery/); assert.match(context, /final assistant reply/);
  assert.doesNotMatch(context, /private-test-token/); assert.doesNotMatch(context, /mock-runtime/);
}));
test("actual prompt acceptance clears stale busy/Enter input blocking", () => fixture(async ({ router, writes, hook }) => {
  router.connect("a", "b"); router.input("a", "draft"); router.input("a", "\r"); router.input("a", "\r"); assert.equal(router.runtime.get("a").dirty, true);
  await hook("a", "UserPromptSubmit"); assert.equal(router.runtime.get("a").dirty, false);
  router.complete("a", "Please review this.", "turn-1"); assert.equal(writes.length, 1); assert.equal(writes[0].id, "b");
}));
test("prompt acknowledgement preserves a draft typed after submission", () => fixture(async ({ router, writes, hook }) => {
  router.connect("a", "b"); router.input("a", "first"); router.input("a", "\r"); router.input("a", "new unsent draft");
  await hook("a", "UserPromptSubmit"); assert.equal(router.runtime.get("a").dirty, true);
  router.complete("a", "previous result", "turn-1"); assert.equal(writes.length, 0);
}));
test("automatic prompt acknowledgements preserve the hop budget", () => fixture(async ({ board, router, writes, hook }) => {
  board.groups[0].maxHops = 1; router.connect("a", "b"); router.connect("b", "a");
  router.complete("a", "review", "a-1"); await hook("b", "UserPromptSubmit"); assert.equal(router.runtime.get("b").cause.hops, 1);
  router.complete("b", "done", "b-1"); assert.equal(writes.length, 1);
}));
test("connection state distinguishes a saved arrow from an available route", () => fixture(async ({ board, router, hook }) => {
  const edge = router.connect("a", "b"); assert.equal(router.routeStatus(edge).state, "ready");
  board.groups[0].paused = true; assert.match(router.routeStatus(edge).label, /Group paused/);
  const context = (await hook("a", "UserPromptSubmit")).hookSpecificOutput.additionalContext; assert.match(context, /"paused":true/);
  board.groups[0].paused = false; router.stopped("b"); assert.equal(router.routeStatus(edge).state, "offline");
}));
