const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');

exports.run = async () => {
  await vscode.workspace.getConfiguration('patchport').update('corePython', process.env.PATCHPORT_TEST_PYTHON, vscode.ConfigurationTarget.Global);
  const extension = vscode.extensions.getExtension('utialy.patchport');
  assert.ok(extension);
  assert.equal(fs.realpathSync(extension.extensionPath), fs.realpathSync(process.env.PATCHPORT_INSTALLED_EXTENSION));
  await extension.activate();
  assert.equal(extension.isActive, true);
  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes('patchport.openBoard'));
  await vscode.commands.executeCommand('patchport.openBoard');
  fs.writeFileSync(path.join(process.env.PATCHPORT_HOST_EVIDENCE, 'host-result.json'), JSON.stringify({
    passed: true, target: `${process.platform}-${process.arch}`, vscode: vscode.version,
    node: process.versions.node, electron: process.versions.electron,
    installed_package: true, activated: extension.isActive, board_opened: true, actual_ai_calls: 0
  }, null, 2));
};
