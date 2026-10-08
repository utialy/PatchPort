const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { Router, defaultBoard } = require("../dist/test/model");
const { SessionHost } = require("../dist/test/session");
const root = path.resolve(__dirname, "..");
const evidence = path.resolve(root, "../../.bridge/vscode-connection-20261004");
const waitFor = async (check, timeout = 15000) => { const end = Date.now() + timeout; while (!check()) { if (Date.now() > end) throw new Error("Timed out waiting for PTY event"); await new Promise(resolve => setTimeout(resolve, 50)); } };

test("real PTYs honor profile functions and deliver Claude to Codex via installed adapters", { timeout: 45000 }, async () => {
  await fs.mkdir(evidence, { recursive: true });
  const workspace = path.join(evidence, "fixture project \ud55c\uae00"); await fs.mkdir(workspace, { recursive: true });
  const profilePath = path.join(workspace, "custom profile.ps1");
  const psLiteral = text => "'" + text.replace(/'/g, "''") + "'";
  await fs.writeFile(profilePath, `function fixture-ai { & ${psLiteral(process.execPath)} @args }\n`, "utf8");
  const board = defaultBoard(); board.nodes = ["a", "b"].map((id, i) => ({ id, name: id, provider: i ? "codex" : "claude", profileId: id, project: workspace, groupId: "team", x: 0, y: 0 }));
  board.groups[0].maxHops = 2;
  let host; const router = new Router(board, (id, text) => host.send(id, text)); host = new SessionHost(router, path.join(evidence, "fixture-runtime"), root);
  const output = { a: "", b: "" }; host.on("output", (id, data) => output[id] += data);
  try {
    host.resize("a", 73, 19);
    for (const n of board.nodes) await host.start(n, { id: n.id, name: n.id, provider: n.provider, command: process.platform === "win32" ? "fixture-ai" : process.execPath, args: [path.join(__dirname, "fake-ai.cjs"), "--provider", n.provider], shell: process.platform === "win32" ? "powershell" : "direct", profilePath: process.platform === "win32" ? profilePath : undefined });
    await waitFor(() => router.runtime.get("a")?.phase === "ready" && output.b.includes("PATCHPORT_FIXTURE"));
    assert.equal(host.sessions.get("a").screen.cols, 73);
    router.ready("b", true); router.connect("a", "b"); router.connect("b", "a");
    host.input("a", "Hello from the PTY\r");
    await waitFor(() => router.deliveries.length === 2 && router.deliveries.every(d => d.status === "completed"), 25000);
    assert.equal(board.groups[0].used, 2); assert.match(output.b, /Peer message received/); assert.match(output.a, /CLAUDE_REPLY_2/);
    const snapshot = await host.snapshot("a"); assert.ok(snapshot.sequence > 0); assert.equal(snapshot.cols, 73); assert.match(snapshot.data, /CLAUDE_REPLY_2/);
    const s = host.sessions.get("a");
    const forbidden = await new Promise(resolve => { const req = http.request(s ? `http://127.0.0.1:${host.port}/events/a` : "", { method: "POST", headers: { Authorization: "Bearer invalid" } }, response => { resolve(response.statusCode); response.resume(); }); req.end("{}"); });
    assert.equal(forbidden, 403);
    await fs.writeFile(path.join(evidence, "pty-fixture.json"), JSON.stringify({ mock: true, realAI: 0, platform: process.platform, result: "passed", deliveries: router.deliveries, output }, null, 2));
  } catch (error) { await fs.writeFile(path.join(evidence, "pty-failure.json"), JSON.stringify({ error: String(error), state: [...router.runtime.values()], deliveries: router.deliveries, output }, null, 2)); throw error; }
  finally { host.dispose(); }
});
