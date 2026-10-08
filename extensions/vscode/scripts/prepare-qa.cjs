const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const evidence = path.resolve(root, "../../.bridge/vscode-qa");
const project = path.join(evidence, "ui-project");
const user = path.join(evidence, "vscode-portable/user-data");
fs.mkdirSync(project, { recursive: true });
fs.mkdirSync(path.join(user, "User"), { recursive: true });
fs.writeFileSync(path.join(project, "README.md"), "# PatchPort validation workspace\n\nThis isolated folder contains no application source or credentials.\n");
const settings = {
  "workbench.startupEditor": "none", "workbench.colorTheme": "Default Dark Modern",
  "telemetry.telemetryLevel": "off", "update.mode": "none", "extensions.autoUpdate": false,
  "window.title": "PatchPort validation",
  "window.dialogStyle": "custom",
  "patchport.profiles": [
    ...["claude", "codex"].map(provider => ({ id: `fixture-${provider}`, name: `Fixture ${provider}`, provider, command: process.execPath, args: [path.join(root, "test/fake-ai.cjs"), "--provider", provider], shell: "direct" }))
  ]
};
fs.writeFileSync(path.join(user, "User/settings.json"), JSON.stringify(settings, null, 2));
console.log(JSON.stringify({ evidence, project, user }, null, 2));
