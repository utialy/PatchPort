const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vscode = require("vscode");

exports.run = async () => {
  await vscode.workspace.getConfiguration("patchport").update("corePython", process.env.PATCHPORT_TEST_PYTHON, vscode.ConfigurationTarget.Global);
  const extension = vscode.extensions.getExtension("utialy.patchport");
  assert.ok(extension);
  assert.equal(fs.realpathSync(extension.extensionPath), fs.realpathSync(process.env.PATCHPORT_INSTALLED_EXTENSION));
  const api = await extension.activate();
  assert.equal(extension.isActive, true);
  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes("patchport.openBoard"));
  await vscode.commands.executeCommand("patchport.openBoard");
  const config = vscode.workspace.getConfiguration("patchport");
  const profilesBefore = config.inspect("localProfiles")?.globalValue;
  const profile = { id: "packaged-local-fixture", name: "Packaged local fixture", backend: "ollama",
    endpointOrigin: "http://127.0.0.1:11434", modelId: "fixture-only", contextWindow: 8192,
    maxOutputTokens: 128, requestTimeoutSec: 10, revision: "packaged-fixture", executionLocationConfirmed: true };
  try {
    await config.update("localProfiles", [profile], vscode.ConfigurationTarget.Global);
    const state = api.getState();
    const selected = state.profiles.find(item => item.id === profile.id);
    assert.equal(selected?.provider, "local");
    assert.equal(selected.local.backend, profile.backend);
    assert.equal(selected.local.modelId, profile.modelId);
    assert.equal(state.active.length, 0, "Profile discovery must not start a model session.");
  } finally { await config.update("localProfiles", profilesBefore, vscode.ConfigurationTarget.Global); }
  fs.writeFileSync(path.join(process.env.PATCHPORT_HOST_EVIDENCE, "host-result.json"), JSON.stringify({
    passed: true, target: `${process.platform}-${process.arch}`, vscode: vscode.version,
    node: process.versions.node, electron: process.versions.electron,
    installed_package: true, activated: extension.isActive, board_opened: true,
    local_profile_recognized: true, automatic_model_sessions: 0, actual_ai_calls: 0
  }, null, 2));
};
