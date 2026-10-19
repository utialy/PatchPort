const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const { temporaryRoot } = require('./helpers.cjs');
const { bundleExecutable } = require('../scripts/vscode-path.cjs');

test('app metadata resolves a renamed macOS executable without assuming the Electron filename', () => {
  const root = fs.mkdtempSync(path.join(temporaryRoot, 'patchport-app-'));
  try {
    const directory = path.join(root, 'Example.app/Contents/MacOS'); fs.mkdirSync(directory, { recursive: true });
    const expected = path.join(directory, 'Renamed Code'); fs.writeFileSync(expected, 'fixture');
    assert.equal(bundleExecutable(path.join(directory, 'Electron'), file => { assert.equal(path.basename(file), 'Info.plist'); return 'Renamed Code\n'; }), expected);
    assert.throws(() => bundleExecutable(path.join(directory, 'Electron'), () => '../outside'), /Invalid app executable/);
  } finally {
    assert.equal(path.dirname(root), temporaryRoot); fs.rmSync(root, { recursive: true, force: true });
  }
});
