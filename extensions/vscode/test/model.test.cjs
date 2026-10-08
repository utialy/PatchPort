const { test } = require("node:test");
const assert = require("node:assert/strict");
const { Router, defaultBoard, restoreBoard, validateProfile, cleanMessage, inputIntent, explicitTerminalText } = require("../dist/test/model");

function setup() {
  const board = defaultBoard();
  board.nodes = ["a", "b", "c"].map((id, i) => ({ id, name: id, profileId: id, provider: i === 1 ? "codex" : "claude", project: "/project", groupId: "team", x: 0, y: 0 }));
  const writes = []; const router = new Router(board, (id, text) => writes.push({ id, text }));
  for (const n of board.nodes) { router.started(n.id); router.ready(n.id); }
  return { board, router, writes };
}
test("a completed response crosses a directed edge exactly once per event", () => {
  const { router, writes } = setup(); router.connect("a", "b"); router.complete("a", "Build the feature", "turn-1"); router.complete("a", "Build the feature", "turn-1");
  assert.equal(writes.length, 1); assert.match(writes[0].text, /Build the feature/); assert.equal(writes[0].id, "b"); assert.equal(router.runtime.get("b").phase, "busy");
  router.complete("b", "Reviewed", "turn-b"); assert.equal(router.deliveries[0].status, "completed");
});
test("cycles stop at the group hop bound", () => {
  const { router, board, writes } = setup(); board.groups[0].maxHops = 2; router.connect("a", "b"); router.connect("b", "a");
  router.complete("a", "first", "a1"); router.complete("b", "second", "b1"); router.complete("a", "third", "a2"); assert.equal(writes.length, 2); assert.equal(board.groups[0].used, 2);
});
test("fan-out consumes the shared delivery budget without overshoot", () => {
  const { router, board, writes } = setup(); board.groups[0].maxDeliveries = 1; router.connect("a", "b"); router.connect("a", "c"); router.complete("a", "hello", "1");
  assert.equal(writes.length, 1); assert.equal(board.groups[0].used, 1); assert.equal(router.deliveries[1].status, "cancelled");
});
test("manual input, focus and permission waits keep messages queued", () => {
  const { router, writes } = setup(); router.connect("a", "b"); router.input("b", "unfinished"); router.complete("a", "peer", "1"); assert.equal(writes.length, 0);
  router.runtime.get("b").focused = true; router.ready("b", true); assert.equal(writes.length, 0);
  router.runtime.get("b").focused = false; router.setPhase("b", "attention"); router.drain(); assert.equal(writes.length, 0);
  router.ready("b", true); assert.equal(writes.length, 1);
});
test("paused groups do not replay responses produced during pause", () => {
  const { router, board, writes } = setup(); router.connect("a", "b"); board.groups[0].paused = true; router.complete("a", "private", "1"); board.groups[0].paused = false; router.drain(); assert.equal(writes.length, 0);
});
test("restart and disconnect cancel stale queued messages", () => {
  const { router, writes } = setup(); const edge = router.connect("a", "b"); router.setPhase("b", "busy"); router.complete("a", "old", "1"); router.started("b"); router.ready("b"); assert.equal(writes.length, 0); assert.equal(router.deliveries[0].status, "cancelled");
  router.setPhase("b", "busy"); router.complete("a", "next", "2"); router.removeEdge(edge.id); router.ready("b"); assert.equal(writes.length, 0);
});
test("uncertain writes are not retried", () => {
  const { board } = setup(); let attempts = 0; const r = new Router(board, () => { attempts++; throw new Error("interrupted"); }); for (const id of ["a", "b"]) { r.started(id); r.ready(id); }
  r.connect("a", "b"); r.complete("a", "message", "1"); r.ready("b", true); r.drain(); assert.equal(attempts, 1); assert.equal(r.deliveries[0].status, "uncertain"); assert.equal(board.groups[0].used, 1);
});
test("shells, self links and other projects cannot be automatic targets", () => {
  const { router, board } = setup(); assert.throws(() => router.connect("a", "a")); board.nodes[1].provider = "terminal"; assert.throws(() => router.connect("a", "b")); board.nodes[2].project = "/other"; assert.throws(() => router.connect("a", "c"));
});
test("control sequences cannot escape bracketed paste", () => {
  assert.equal(cleanMessage("\u001b[31mhello\u001b[0m\u001b[201~\r\u0003world\u001b]52;c;secret\u0007"), "helloworld");
});
test("restore pauses groups and excludes unknown project nodes", () => {
  const { board } = setup(); board.groups[0].used = 7; board.nodes[2].project = "/other"; const restored = restoreBoard(board, ["/project"]); assert.equal(restored.nodes.length, 2); assert.equal(restored.groups[0].used, 7); assert.equal(restored.groups[0].paused, true);
});
test("launch profiles preserve custom command and reject shell-code newlines", () => {
  const p = { id: "custom", name: "Custom Claude", provider: "claude", command: "claude-2", args: ["--model", "opus"], shell: "powershell", profilePath: "C:/profile.ps1" };
  assert.equal(validateProfile(p).command, "claude-2"); assert.throws(() => validateProfile({ ...p, args: "hello" })); assert.throws(() => validateProfile({ ...p, command: "claude\nwhoami" }));
});
test("focus reports and Win32 key-up events do not become human edits", () => {
  const { router } = setup(); router.input("a", "\u001b[I"); router.input("a", "\u001b[O"); router.input("a", "\u001b[65;30;97;0;0;1_"); assert.equal(router.runtime.get("a").dirty, false);
  assert.equal(inputIntent("\u001b[13;28;13;1;0;1_"), "submit"); router.input("a", "\u001b[65;30;97;1;0;1_"); assert.equal(router.runtime.get("a").dirty, true);
  router.input("a", "\u001b[13;28;13;1;0;1_"); assert.equal(router.runtime.get("a").phase, "busy"); assert.equal(router.runtime.get("a").dirty, false);
});
test("existing-terminal paste cannot smuggle a newline or terminal control sequence", () => {
  assert.equal(explicitTerminalText("Write-Output EXPLICIT_PASTE"), "Write-Output EXPLICIT_PASTE");
  for (const value of ["", "echo one\necho two", "echo one\r", "\u001b[201~hello", "\u0003", "a".repeat(8001)]) assert.throws(() => explicitTerminalText(value));
});
