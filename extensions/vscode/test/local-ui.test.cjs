const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const esbuild = require("esbuild");
const { chromium } = require("playwright-core");

const root = path.resolve(__dirname, "..");
const browserCandidates = {
  win32: ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Google/Chrome/Application/chrome.exe"],
  darwin: ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "/Applications/Chromium.app/Contents/MacOS/Chromium"],
  linux: ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
};
const browserPath = process.env.PATCHPORT_UI_BROWSER || (browserCandidates[process.platform] || []).find(file => fs.existsSync(file));

test("local profile form and board preserve API boundaries in a real browser", { skip: browserPath ? false : "Chromium browser unavailable; set PATCHPORT_UI_BROWSER to run browser UI checks. Native helper/PTY checks still run.", timeout: 60000 }, async t => {
  const compiled = await esbuild.build({ entryPoints: [path.join(root, "src/webview.ts")], bundle: true, write: false, platform: "browser", target: "es2022" });
  const css = fs.readFileSync(path.join(root, "media/board.css"), "utf8");
  const server = http.createServer((request, response) => {
    response.setHeader("content-type", request.url === "/board.js" ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8");
    response.end(request.url === "/board.js" ? compiled.outputFiles[0].text : `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body><div id="app"></div><script>window.messages=[]; window.acquireVsCodeApi=()=>({postMessage:message=>messages.push(message),getState:()=>undefined,setState:()=>{}});</script><script src="/board.js"></script></body></html>`);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const browser = await chromium.launch({ executablePath: browserPath, headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const cli = { id: "codex", name: "Codex", provider: "codex", command: "codex", args: [], shell: "powershell" };
  const view = { board: { version: 1, nodes: [], edges: [], groups: [{ id: "group", name: "Review", goal: "", paused: true, used: 2, round: 0, maxDeliveries: 4, maxHops: 2 }] }, runtime: [], active: [], profiles: [cli], projects: ["C:/sample/project"], trusted: true, supported: true, defaultShell: "powershell", remote: "", version: "test", deliveries: [], notices: [], routes: [], external: [] };
  const send = data => page.evaluate(data => window.dispatchEvent(new MessageEvent("message", { data })), data);
  const update = () => send({ type: "state", value: view });
  const messages = () => page.evaluate(() => window.messages);
  const last = async type => (await messages()).filter(item => item.type === type).at(-1);
  const field = text => page.locator("#modal-body").getByLabel(text, { exact: false });
  const selectProfile = async id => {
    await page.locator("#profiles").click();
    await page.locator("#modal-body > .field select").selectOption(id);
  };
  await update();

  await t.test("discovery is read-only, preserves drafts, excludes known unsupported models, and requires explicit location confirmation", async () => {
    await selectProfile(":new-local:");
    await field("Display name").fill("Local review draft");
    assert.equal(await field("Local runtime").inputValue(), "ollama");
    assert.equal(await page.getByRole("button", { name: "Set API token", exact: false }).isDisabled(), true);
    await page.getByRole("button", { name: "Check connection", exact: true }).click();
    assert.deepEqual(await last("localProbe"), { type: "localProbe", backend: "ollama", endpointOrigin: "http://127.0.0.1:11434" });
    view.localProbe = { backend: "ollama", endpointOrigin: "http://127.0.0.1:11434", state: "ready", models: [
      { modelId: "chat:latest", kind: "chat", executionLocation: "unknown", supported: true },
      { modelId: "embedding-only", kind: "embedding", executionLocation: "unknown", supported: false },
      { modelId: "remote-model", kind: "chat", executionLocation: "remote", supported: false }
    ] };
    await update();
    assert.equal(await field("Display name").inputValue(), "Local review draft");
    assert.deepEqual(await field("Server models").locator("option").evaluateAll(options => options.map(option => option.value)), ["", "chat:latest"]);
    await field("Server models").selectOption("chat:latest");
    await field("Context window").fill("8192");
    const save = page.getByRole("button", { name: "Save local profile", exact: true });
    await save.click();
    assert.equal(await last("localProfileSave"), undefined);
    await field("I confirmed in the server settings that this model runs on my computer.").check();
    await save.click();
    const saved = await last("localProfileSave");
    assert.match(saved.profile.id, /^local-/);
    assert.equal(saved.profile.executionLocationConfirmed, true);
    assert.equal(saved.profile.contextWindow, 8192);
    assert.equal(saved.profile.requestTimeoutSec, 300);
    assert.equal(saved.profile.modelId, "chat:latest");
    assert.equal(Object.hasOwn(saved.profile, "token"), false);
    assert.equal(Object.hasOwn(saved.profile, "command"), false);
    assert.equal((await messages()).some(item => ["add", "start", "input", "localNewConversation"].includes(item.type)), false);
    await field("Exact model ID").fill("embedding-only");
    assert.equal(await save.isDisabled(), true);
    assert.match(await page.locator(".local-model-status").innerText(), /unavailable/);
    assert.match(await page.locator(".local-model-status").innerText(), /Response compatibility: unverified/);
  });

  const localProfile = { id: "local-saved", name: "Saved LM", provider: "local", command: "", args: [], shell: "direct", local: { backend: "lmstudio", endpointOrigin: "http://127.0.0.1:1234", modelId: "lm-chat", contextWindow: 8192, maxOutputTokens: 1024, requestTimeoutSec: 300, revision: "revision-new", executionLocationConfirmed: true } };
  await t.test("secret actions carry only profile identity and edited origins cannot reuse the saved token", async () => {
    view.profiles.push(localProfile); await update();
    await page.locator("#modal-close").click(); await selectProfile(localProfile.id);
    await page.getByRole("button", { name: "Set API token", exact: false }).click();
    assert.deepEqual(await last("localTokenSet"), { type: "localTokenSet", id: localProfile.id });
    assert.equal(await page.locator("#modal-body input[type=password]").count(), 0);
    await page.getByRole("button", { name: "Refresh models", exact: false }).click();
    assert.deepEqual(await last("localProbe"), { type: "localProbe", id: localProfile.id, backend: "lmstudio", endpointOrigin: "http://127.0.0.1:1234" });
    await field("Server origin").fill("http://127.0.0.1:1235");
    assert.equal(await field("I confirmed in the server settings that this model runs on my computer.").isChecked(), false);
    assert.equal(await page.getByRole("button", { name: "Set API token", exact: false }).isDisabled(), true);
    view.localProbe = { id: localProfile.id, backend: "lmstudio", endpointOrigin: "http://127.0.0.1:1234", state: "ready", models: [{ modelId: "stale-model", kind: "chat", executionLocation: "unknown", supported: true }] };
    await update();
    assert.equal(await field("Server models").isDisabled(), true);
    assert.doesNotMatch(await field("Server models").innerText(), /stale-model/);
    await page.getByRole("button", { name: "Refresh models", exact: false }).click();
    assert.deepEqual(await last("localProbe"), { type: "localProbe", backend: "lmstudio", endpointOrigin: "http://127.0.0.1:1235" });
    await field("Local runtime").selectOption("ollama");
    assert.equal(await field("Server origin").inputValue(), "http://127.0.0.1:11434");
    assert.equal(await field("Exact model ID").inputValue(), "");
    assert.equal(await field("Context window").inputValue(), "");
  });

  await t.test("cards show frozen active settings and keep new conversation separate from group rounds", async () => {
    await page.locator("#modal-close").click();
    view.board.version = 2;
    view.board.nodes = [{ id: "node-local", name: "Local review", project: view.projects[0], profileId: localProfile.id, provider: "local", groupId: "group", x: 32, y: 32 }];
    view.active = ["node-local"];
    view.runtime = [{ id: "node-local", phase: "busy", dirty: false, focused: false, lastMessage: "", reason: "", generation: "gen-1", local: { ...localProfile.local, endpointOrigin: "http://127.0.0.1:1236", modelId: "frozen-model", revision: "revision-active" } }];
    await update();
    assert.match(await page.locator(".profile-name").innerText(), /frozen-model/);
    assert.match(await page.locator(".local-capability").innerText(), /Conversation only.*1236/);
    assert.match(await page.locator(".local-capability").getAttribute("title"), /revision-active/);
    assert.equal(await page.getByRole("button", { name: "New local conversation", exact: true }).isDisabled(), true);
    view.runtime[0].phase = "ready"; view.runtime[0].dirty = true; await update();
    assert.equal(await page.getByRole("button", { name: "New local conversation", exact: true }).isDisabled(), true);
    view.runtime[0].dirty = false; await update();
    await page.getByRole("button", { name: "New local conversation", exact: true }).click();
    assert.deepEqual(await last("localNewConversation"), { type: "localNewConversation", id: "node-local" });
    await page.locator("[data-tab=groups]").click();
    await page.getByRole("button", { name: "New round", exact: true }).click();
    assert.deepEqual(await last("newRound"), { type: "newRound", id: "group" });
    view.active = []; await update();
    assert.match(await page.locator(".profile-name").innerText(), /lm-chat/);
    assert.match(await page.locator(".local-capability").getAttribute("title"), /Next session settings/);
  });

  await t.test("CLI profiles still submit their own shell fields without local settings", async () => {
    await selectProfile(cli.id);
    await field("Display name").fill("Custom Codex");
    await page.getByRole("button", { name: "Save profile", exact: true }).click();
    const saved = await last("profile");
    assert.equal(saved.profile.name, "Custom Codex");
    assert.equal(saved.profile.command, "codex");
    assert.equal(saved.profile.shell, "powershell");
    assert.equal(Object.hasOwn(saved.profile, "endpointOrigin"), false);
    assert.equal(Object.hasOwn(saved.profile, "local"), false);
  });
  assert.deepEqual(errors, []);
});
