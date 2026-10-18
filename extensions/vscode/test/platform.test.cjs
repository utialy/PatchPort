const test = require('node:test'), assert = require('node:assert/strict');
const { defaultProfiles, defaultPython, defaultShell, supportedLocal } = require('../dist/test/platform');
const { launch } = require('../dist/test/session');
const { nativeTarget } = require('../scripts/native-package.cjs');

test('native targets and shell defaults distinguish OS and CPU without enabling remote sessions', () => {
  for (const [platform, arch, shell, python] of [['win32', 'x64', 'powershell', 'python'], ['linux', 'x64', 'bash', 'python3'], ['linux', 'arm64', 'bash', 'python3'], ['darwin', 'x64', 'zsh', 'python3'], ['darwin', 'arm64', 'zsh', 'python3']]) {
    assert.equal(supportedLocal(platform, arch), true);
    assert.equal(supportedLocal(platform, arch, 'ssh-remote'), false);
    assert.equal(defaultShell(platform), shell); assert.equal(defaultPython(platform), python);
    assert.equal(defaultProfiles(platform)[0].shell, shell);
    assert.equal(nativeTarget(`${platform}-${arch}`, platform, arch), `${platform}-${arch}`);
  }
  assert.equal(supportedLocal('freebsd', 'x64'), false);
  assert.throws(() => nativeTarget('darwin-arm64', 'win32', 'x64'), /matching native host/);
  assert.throws(() => nativeTarget('../bad', 'linux', 'x64'), /Unsupported/);
});

test('PowerShell Core uses the POSIX executable while Windows PowerShell is rejected there', () => {
  const profile = { id: 'x', name: 'x', provider: 'terminal', command: 'fixture', args: [], shell: 'pwsh' };
  assert.equal(launch(profile, [], '/project', 'linux').file, 'pwsh');
  assert.equal(launch(profile, [], '/project', 'darwin').file, 'pwsh');
  assert.equal(launch(profile, [], 'C:/project', 'win32').file, 'pwsh.exe');
  assert.throws(() => launch({ ...profile, shell: 'powershell' }, [], '/project', 'darwin'), /requires Windows/);
});
