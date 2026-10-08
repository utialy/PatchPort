const { chromium } = require("playwright-core");
const fs = require("node:fs/promises");
const path = require("node:path");
const evidence = path.resolve(__dirname, "../../../.bridge/vscode-qa");
const mode = process.argv[2] || "inspect";
const assert = require("node:assert/strict");
async function command(page, title) {
  await page.keyboard.press("Control+Shift+P");
  const input = page.locator(".quick-input-widget input").first(); await input.waitFor({ state: "visible" });
  await input.fill(">" + title); await page.waitForTimeout(180); await input.press("Enter");
}
async function boardFrame(page) {
  const before = new Set(page.frames());
  await command(page, "Developer: Reload Webviews");
  for (let i = 0; i < 70; i++) { const frame = page.frames().find(f => !before.has(f) && f.url().includes("/fake.html")); if (frame && await frame.locator("#app").count()) { await frame.locator("#counts").filter({ hasText: /\uac1c \ud130\ubbf8\ub110/ }).waitFor(); return frame; } await page.waitForTimeout(100); }
  throw new Error("PatchPort webview did not become available");
}
async function connect() {
  const browser = await chromium.connectOverCDP("http://127.0.0.1:9347");
  const page = browser.contexts().flatMap(c => c.pages()).find(p => p.url().includes("workbench"));
  if (!page) throw new Error("Isolated VS Code workbench not found");
  await page.waitForTimeout(400);
  return { browser, page };
}
(async () => {
  const { browser, page } = await connect();
  try {
    if (mode === "shutdown") {
      assert.equal(await page.title(), "PatchPort validation");
      await (await browser.newBrowserCDPSession()).send("Browser.close");
      console.log("Closed the isolated QA VS Code instance.");
    } else if (mode === "cleanup-native") {
      await command(page, "Terminal: Kill the Active Terminal Instance"); await page.waitForTimeout(400);
      const frame = await boardFrame(page); await frame.locator("#external").click(); assert.equal(await frame.locator(".external-row").count(), 0); await frame.locator("#modal-close").click();
      console.log("Dedicated existing-terminal fixture stopped.");
    } else if (mode === "inspect") {
      console.log(JSON.stringify({ title: await page.title(), url: page.url(), frames: await Promise.all(page.frames().map(async f => ({ url: f.url(), text: (await f.locator("body").innerText().catch(() => "")).slice(-6500) }))), controls: await page.locator("[aria-label]").evaluateAll(items => items.map(i => i.getAttribute("aria-label")).filter(s => /Hide|Toggle|Close|Side Bar/.test(s)).slice(0, 40)) }, null, 2));
    } else if (mode === "trust") {
      await page.keyboard.press("Control+Shift+P"); await page.keyboard.type("Workspaces: Manage Workspace Trust"); await page.keyboard.press("Enter"); await page.waitForTimeout(500);
      console.log((await page.locator("body").innerText()).slice(-6500));
    } else if (mode === "trust-confirm") {
      await page.getByRole("button", { name: "Trust", exact: true }).click();
      await page.waitForTimeout(500);
      await page.keyboard.press("Control+Shift+P"); await page.keyboard.type("PatchPort: Open AI Terminal Board"); await page.keyboard.press("Enter");
      await page.setViewportSize({ width: 1600, height: 1000 });
      await page.keyboard.press("Control+b");
      await page.waitForTimeout(600);
      console.log((await page.locator("body").innerText()).slice(-2500));
    } else if (mode === "open") {
      const trust = page.getByRole("button", { name: "Yes, I trust the authors", exact: true });
      if (await trust.isVisible()) await trust.click();
      await command(page, "PatchPort: Open AI Terminal Board");
      await page.waitForTimeout(1600);
      console.log(JSON.stringify({ frames: page.frames().map(f => f.url()), text: (await page.locator("body").innerText()).slice(-4000) }));
    } else if (mode === "reload-webview") {
      await page.keyboard.press("Control+Shift+P"); await page.keyboard.type("Developer: Reload Webviews"); await page.keyboard.press("Enter"); await page.waitForTimeout(1200);
      console.log(JSON.stringify(await Promise.all(page.frames().map(async f => ({ url: f.url(), text: (await f.locator("body").innerText().catch(() => "")).slice(0, 5000) }))), null, 2));
    } else if (mode === "fixture") {
      const hideChat = page.getByLabel("Hide Secondary Side Bar (Ctrl+Alt+B)", { exact: true }); if (await hideChat.isVisible()) await hideChat.click();
      const explorer = page.getByRole("heading", { name: "Explorer", exact: true });
      if (await explorer.isVisible()) await page.getByLabel("Toggle Primary Side Bar (Ctrl+B)", { exact: true }).click();
      const closeNotice = page.getByLabel("Close (Escape)", { exact: true }); if (await closeNotice.first().isVisible()) await closeNotice.first().click();
      const frame = await boardFrame(page);
      while (await frame.locator(".remove-button:visible").count()) {
        const remove = frame.locator(".remove-button:visible").first();
        const id = await remove.evaluate(e => e.closest("article").dataset.id);
        await remove.click(); await frame.locator(`article[data-id=\"${id}\"]`).waitFor({ state: "detached" });
      }
      assert.equal(await frame.locator(".terminal-card").count(), 0, "Stop any previous QA sessions before starting this test");
      for (const provider of ["claude", "codex"]) {
        await frame.locator("#add").click(); await frame.locator("#modal-body select").nth(0).selectOption(`fixture-${provider}`); await frame.getByRole("button", { name: "Start terminal", exact: true }).click();
        await frame.waitForTimeout(1800);
      }
      const cards = frame.locator(".terminal-card"); assert.equal(await cards.count(), 2);
      await cards.nth(1).getByRole("button", { name: "Ready to receive", exact: true }).click();
      await frame.locator("[data-tab=\"groups\"]").click(); const fixtureGroup = frame.locator(".group-row").filter({ hasText: "Project team" }); await fixtureGroup.getByRole("button", { name: "Settings", exact: true }).click();
      await frame.locator("#modal-body input[type=\"number\"]").nth(0).fill("2"); await frame.locator("#modal-body input[type=\"number\"]").nth(1).fill("2"); await frame.getByRole("button", { name: "Save settings", exact: true }).click();
      await fixtureGroup.getByRole("button", { name: "New round", exact: true }).click(); await page.getByRole("button", { name: "New round", exact: true }).click();
      await cards.nth(0).getByRole("button", { name: "Output port", exact: true }).click(); await cards.nth(1).getByRole("button", { name: "Input port", exact: true }).click();
      await cards.nth(1).getByRole("button", { name: "Output port", exact: true }).click(); await cards.nth(0).getByRole("button", { name: "Input port", exact: true }).click();
      await cards.nth(0).locator(".xterm-helper-textarea").focus(); await page.keyboard.type("UI bridge verification"); await page.keyboard.press("Enter"); await frame.locator("h1").click();
      await frame.locator("[data-tab=\"activity\"]").click();
      await frame.locator(".delivery.completed").nth(1).waitFor({ timeout: 20000 });
      assert.equal(await frame.locator(".delivery").count(), 2);
      const text = await frame.locator("body").innerText();
      await frame.locator("#zoom-out").click();
      await page.screenshot({ path: path.join(evidence, "board-fixture.png") });
      await fs.writeFile(path.join(evidence, "ui-fixture.json"), JSON.stringify({ installedVSIX: true, realAI: 0, result: "passed", cards: 2, completedDeliveries: 2, text }, null, 2));
      console.log("Installed VSIX UI: two PTYs, two directed routes, bounded round trip passed.");
    } else if (mode === "screen-restore") {
      const frame = await boardFrame(page); const cards = frame.locator(".terminal-card");
      await cards.nth(0).getByRole("button", { name: "Expand terminal", exact: true }).click(); await frame.waitForTimeout(350);
      assert.match(await frame.locator(".terminal-expanded").innerText(), /CLAUDE_REPLY_2/);
      await page.screenshot({ path: path.join(evidence, "terminal-expanded.png") });
      await frame.getByRole("button", { name: "Back to board", exact: true }).click();
      const before = await cards.nth(0).getAttribute("data-id");
      const restored = await boardFrame(page); await restored.waitForTimeout(500);
      assert.equal(await restored.locator(".terminal-card").first().getAttribute("data-id"), before);
      assert.match(await restored.locator(".terminal-card").first().innerText(), /CLAUDE_REPLY_2/);
      await page.screenshot({ path: path.join(evidence, "board-final.png") });
      await fs.writeFile(path.join(evidence, "screen-restore.json"), JSON.stringify({ result: "passed", sameSession: true, snapshotRestored: true, enlargedAndReturned: true, additionalAI: 0 }, null, 2));
      console.log("Terminal enlargement and screen snapshot restoration passed without restarting sessions.");
    } else if (mode === "profile-form") {
      const frame = await boardFrame(page); await frame.locator("#profiles").click();
      const form = frame.locator("#modal-body form");
      await form.locator("input").nth(0).fill("QA Profile Through UI"); await form.locator("select").nth(0).selectOption("claude");
      await form.locator("input").nth(1).fill("fixture-ai");
      await form.locator("input").nth(2).fill(JSON.stringify([path.resolve(__dirname, "../test/fake-ai.cjs"), "--provider", "claude"]));
      await form.locator("select").nth(1).selectOption("powershell");
      await form.locator("input").nth(4).fill(path.join(evidence, "fixture project \ud55c\uae00/custom profile.ps1"));
      await frame.getByRole("button", { name: "Save profile", exact: true }).click();
      await frame.getByRole("button", { name: "Start terminal", exact: true }).waitFor();
      assert.match(await frame.locator("#modal-body select").first().locator("option:checked").innerText(), /QA Profile Through UI/);
      await frame.getByRole("button", { name: "Start terminal", exact: true }).click();
      const card = frame.locator(".terminal-card").last(); await card.locator(".phase.ready").waitFor({ timeout: 10000 });
      await card.locator(".xterm-helper-textarea").focus(); await page.keyboard.insertText("PROFILE_UI_CONFIRMED"); await page.keyboard.press("Enter"); await frame.locator("h1").click();
      await card.getByText(/CLAUDE_REPLY_1: PROFILE_UI_CONFIRMED/).first().waitFor({ timeout: 8000 });
      await fs.writeFile(path.join(evidence, "profile-ui.json"), JSON.stringify({ result: "passed", customShellFunction: "fixture-ai", profilePathWithSpaceAndKorean: true, savedThroughUI: true, autoSelectedAfterSave: true, actualAI: 0 }, null, 2));
      console.log("Profile form saved, selected, and launched a custom PowerShell function.");
    } else if (mode === "external") {
      let frame = await boardFrame(page); await frame.locator("#external").click();
      const initial = await frame.locator(".external-row").count(); assert.ok(initial <= 1, "Only the dedicated QA terminal should receive the paste."); await frame.locator("#modal-close").click();
      if (!initial) { await command(page, "Terminal: Create New Terminal"); await page.waitForTimeout(1000); }
      await command(page, "PatchPort: Open AI Terminal Board");
      frame = await boardFrame(page); await frame.locator("#external").click(); assert.equal(await frame.locator(".external-row").count(), 1);
      await frame.locator(".external-row input").fill("Write-Output PATCHPORT_EXISTING_PASTE"); await frame.locator(".external-row").getByRole("button", { name: "Paste", exact: true }).click();
      await page.getByRole("button", { name: "Paste without Enter", exact: true }).click(); await page.waitForTimeout(500); await frame.locator("#modal-close").click();
      await page.screenshot({ path: path.join(evidence, "existing-terminal.png") });
      await fs.writeFile(path.join(evidence, "existing-terminal.json"), JSON.stringify({ result: "explicit paste action completed", terminalCount: 1, automaticEnter: false, automaticRouting: false, actualAI: 0 }, null, 2));
      console.log("Existing terminal listed, selected, and explicitly pasted without Enter.");
    } else if (mode === "stop") {
      const frame = await boardFrame(page); const running = await frame.locator(".stop-button:visible").count(); await frame.locator("#stop-all").click();
      if (running) await page.getByRole("button", { name: "Stop", exact: true }).click();
      await page.waitForTimeout(1600); console.log("Managed QA terminals stopped.");
    } else if (mode === "reload-window") {
      await command(page, "Developer: Reload Window"); await page.waitForTimeout(1800);
      await command(page, "PatchPort: Open AI Terminal Board"); await page.waitForTimeout(1000);
      const frame = await boardFrame(page); const text = await frame.locator("body").innerText();
      assert.match(text, /0\uac1c \uc2e4\ud589 \uc911/); assert.match(text, /\uc5f0\uacb0 \uc7ac\uac1c/);
      await fs.writeFile(path.join(evidence, "ui-reload.json"), JSON.stringify({ result: "passed", automaticallyStarted: 0, text }, null, 2));
      console.log("Reload: saved board restored, routing paused, no AI process restarted.");
    } else if (mode === "live-prepare") {
      const frame = await boardFrame(page);
      while (await frame.locator(".remove-button:visible").count()) {
        const remove = frame.locator(".remove-button:visible").first(); const id = await remove.evaluate(e => e.closest("article").dataset.id);
        await remove.click(); await frame.locator(`article[data-id=\"${id}\"]`).waitFor({ state: "detached" });
      }
      assert.equal(await frame.locator(".terminal-card").count(), 0);
      await frame.locator("#new-group").click(); await frame.locator("#modal-body input").fill("\uc2e4\uc81c AI \uc5f0\uacb0 \uc2dc\ud5d8"); await frame.getByRole("button", { name: "Create group", exact: true }).click(); await frame.waitForTimeout(200);
      const groupOption = await frame.locator("#modal").isVisible(); assert.equal(groupOption, false);
      for (const profile of ["claude-2", "codex"]) {
        await frame.locator("#add").click(); await frame.locator("#modal-body select").nth(0).selectOption(profile); await frame.locator("#modal-body select").nth(2).selectOption({ label: "\uc2e4\uc81c AI \uc5f0\uacb0 \uc2dc\ud5d8" }); await frame.getByRole("button", { name: "Start terminal", exact: true }).click(); await frame.waitForTimeout(1600);
      }
      await frame.locator("[data-tab=\"groups\"]").click(); const group = frame.locator(".group-row").filter({ hasText: "\uc2e4\uc81c AI \uc5f0\uacb0 \uc2dc\ud5d8" }); await group.getByRole("button", { name: "Settings", exact: true }).click();
      await frame.locator("#modal-body input[type=\"number\"]").nth(0).fill("1"); await frame.locator("#modal-body input[type=\"number\"]").nth(1).fill("1"); await frame.getByRole("button", { name: "Save settings", exact: true }).click();
      await page.waitForTimeout(1200); await page.screenshot({ path: path.join(evidence, "live-startup.png") }); console.log((await frame.locator("body").innerText()).slice(-10000));
    } else if (mode === "live-inspect") {
      const frame = await boardFrame(page); await frame.locator("[data-tab=\"activity\"]").click(); console.log((await frame.locator("body").innerText()).slice(-14000)); await page.screenshot({ path: path.join(evidence, "live-current.png") });
    } else if (mode === "input-probe") {
      const frame = await boardFrame(page); const card = frame.locator(".terminal-card").first();
      await card.locator(".xterm-helper-textarea").focus(); await page.keyboard.insertText("INPUT_PROBE"); await page.waitForTimeout(800);
      console.log((await card.innerText()).slice(-5000)); await page.screenshot({ path: path.join(evidence, "input-probe.png") });
    } else if (mode === "live-view") {
      const frame = await boardFrame(page); const cards = frame.locator(".terminal-card");
      for (let i = 0; i < 2; i++) {
        await cards.nth(i).getByRole("button", { name: "Expand terminal", exact: true }).click(); await frame.waitForTimeout(700);
        await page.screenshot({ path: path.join(evidence, `live-expanded-${i}.png`) });
        console.log((await frame.locator(".terminal-expanded").innerText()).slice(-4500));
        await frame.getByRole("button", { name: "Back to board", exact: true }).click(); await frame.waitForTimeout(700);
      }
      await frame.locator("[data-tab=\"activity\"]").click(); await page.screenshot({ path: path.join(evidence, "board-live-final-clean.png") });
    } else if (mode === "live-confirm-target") {
      const frame = await boardFrame(page); const target = frame.locator(".terminal-card").nth(1);
      await target.locator(".xterm-helper-textarea").focus(); await page.keyboard.press("Enter"); await frame.locator("h1").click(); await frame.locator("[data-tab=\"activity\"]").click();
      await frame.locator(".delivery.completed").first().waitFor({ timeout: 45000 });
      const text = await frame.locator("body").innerText(); assert.match(text, /PATCHPORT_CODEX_ACK/);
      const budgetPath = path.join(evidence, "live-budget.json"); const budget = JSON.parse(await fs.readFile(budgetPath, "utf8"));
      budget.status = "two real responses confirmed; target required explicit Enter"; budget.observedResponses = 2;
      budget.firstInputAttempt = "No provider request was submitted: the input remained empty; evidence live-ui-result.json and input-probe.png.";
      await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2)); await fs.writeFile(path.join(evidence, "live-manual-enter.json"), JSON.stringify({ result: "provider events passed; automatic Enter needs correction", text }, null, 2));
      await page.screenshot({ path: path.join(evidence, "board-live-manual-enter.png") }); console.log(text.slice(-10000));
    } else if (mode === "live-send") {
      const budgetPath = path.join(evidence, "live-budget.json");
      const previous = await fs.readFile(budgetPath, "utf8").then(JSON.parse).catch(error => { if (error.code === "ENOENT") return null; throw error; });
      assert.equal(previous, null, "A live request already exists; inspect the recorded run instead of retrying.");
      const frame = await boardFrame(page); const cards = frame.locator(".terminal-card"); assert.equal(await cards.count(), 2);
      for (let i = 0; i < 2; i++) await cards.nth(i).getByRole("button", { name: "Ready to receive", exact: true }).click();
      await cards.nth(0).getByRole("button", { name: "Output port", exact: true }).click(); await cards.nth(1).getByRole("button", { name: "Input port", exact: true }).click();
      const prompt = "Please send a one-sentence greeting to the other AI and ask it to reply with exactly PATCHPORT_CODEX_ACK. Do not use tools, read files, change files, or delegate. End after that sentence.";
      const budget = { maxTurns: 4, reservedTurns: 2, submittedByUser: 1, automaticTargetLimit: 1, startedAt: new Date().toISOString(), source: "claude-2", target: "codex", prompt, status: "submitted; target response reserved" };
      await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2), { flag: "wx" });
      await cards.nth(0).locator(".xterm-helper-textarea").focus(); await page.keyboard.type(prompt); await page.keyboard.press("Enter"); await frame.locator("h1").click(); await frame.locator("[data-tab=\"activity\"]").click();
      try { await frame.locator(".delivery.completed").first().waitFor({ timeout: 45000 }); budget.status = "completed"; }
      catch { budget.status = "awaiting inspection; no retry"; }
      const text = await frame.locator("body").innerText();
      await fs.writeFile(path.join(evidence, "live-ui-result.json"), JSON.stringify({ ...budget, text }, null, 2));
      await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2));
      await page.screenshot({ path: path.join(evidence, "board-live.png") });
      console.log(JSON.stringify({ status: budget.status, text: text.slice(-14000) }, null, 2));
    } else if (mode === "live-send-text") {
      const budgetPath = path.join(evidence, "live-budget.json"); const budget = JSON.parse(await fs.readFile(budgetPath, "utf8"));
      assert.equal(budget.status, "awaiting inspection; no retry"); assert.ok(budget.reservedTurns + 2 <= budget.maxTurns);
      const frame = await boardFrame(page); const cards = frame.locator(".terminal-card");
      await cards.nth(0).getByRole("button", { name: "Interrupt", exact: true }).click(); await frame.waitForTimeout(500);
      for (let i = 0; i < 2; i++) await cards.nth(i).getByRole("button", { name: "Ready to receive", exact: true }).click();
      budget.reservedTurns += 2; budget.submittedByUser += 1; budget.status = "explicit second UI attempt, text insertion"; budget.secondAttemptAt = new Date().toISOString();
      await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2));
      await cards.nth(0).locator(".xterm-helper-textarea").focus(); await page.keyboard.insertText(budget.prompt); await page.waitForTimeout(300); await page.keyboard.press("Enter"); await frame.locator("h1").click(); await frame.locator("[data-tab=\"activity\"]").click();
      try { await frame.locator(".delivery.completed").first().waitFor({ timeout: 45000 }); budget.status = "completed"; } catch { budget.status = "awaiting inspection; budget reserved"; }
      const text = await frame.locator("body").innerText(); await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2));
      await fs.writeFile(path.join(evidence, "live-ui-result-2.json"), JSON.stringify({ ...budget, text }, null, 2)); await page.screenshot({ path: path.join(evidence, "board-live.png") });
      console.log(JSON.stringify({ status: budget.status, text: text.slice(-14000) }, null, 2));
    } else if (mode === "live-final") {
      const budgetPath = path.join(evidence, "live-budget.json"); const budget = JSON.parse(await fs.readFile(budgetPath, "utf8"));
      assert.equal(budget.observedResponses, 2); assert.equal(budget.maxTurns, 4); assert.equal(budget.finalAttemptAt, undefined);
      const frame = await boardFrame(page); const cards = frame.locator(".terminal-card"); assert.equal(await cards.count(), 2);
      for (let i = 0; i < 2; i++) { await cards.nth(i).getByRole("button", { name: "Start", exact: true }).click(); await frame.waitForTimeout(1800); }
      await frame.locator("[data-tab=\"groups\"]").click(); const group = frame.locator(".group-row").filter({ hasText: "\uc2e4\uc81c AI \uc5f0\uacb0 \uc2dc\ud5d8" });
      await group.getByRole("button", { name: "New round", exact: true }).click(); await page.getByRole("button", { name: "New round", exact: true }).click();
      for (let i = 0; i < 2; i++) await cards.nth(i).getByRole("button", { name: "Ready to receive", exact: true }).click();
      budget.finalAttemptAt = new Date().toISOString(); budget.status = "final automatic delivery check"; budget.accountedProviderTurns = 4;
      budget.reservationReconciliation = "The first keyboard-only UI attempt sent no provider prompt; the second attempt produced exactly two observed responses. Reserve two final provider turns, total ceiling four.";
      await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2));
      await cards.nth(0).locator(".xterm-helper-textarea").focus(); await page.keyboard.insertText(budget.prompt); await frame.waitForTimeout(300); await page.keyboard.press("Enter"); await frame.locator("h1").click(); await frame.locator("[data-tab=\"activity\"]").click();
      try { await frame.locator(".delivery.completed").first().waitFor({ timeout: 45000 }); budget.status = "automatic delivery completed"; budget.observedResponses = 4; }
      catch { budget.status = "final automatic delivery awaiting inspection; no further model turns"; }
      const text = await frame.locator("body").innerText(); await fs.writeFile(budgetPath, JSON.stringify(budget, null, 2));
      await fs.writeFile(path.join(evidence, "live-final.json"), JSON.stringify({ ...budget, text }, null, 2)); await page.screenshot({ path: path.join(evidence, "board-live-final.png") });
      console.log(JSON.stringify({ status: budget.status, text: text.slice(-14000) }, null, 2));
    } else if (mode === "screenshot") {
      await page.screenshot({ path: path.join(evidence, "board.png") });
      console.log("Saved board.png");
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
