const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function bundleExecutable(candidate, readName = file => {
  const result = spawnSync('/usr/bin/plutil', ['-extract', 'CFBundleExecutable', 'raw', '-o', '-', file], { encoding: 'utf8' });
  if (result.error || result.status !== 0) throw new Error('Cannot read the VS Code app executable metadata.');
  return result.stdout;
}) {
  const contents = path.dirname(path.dirname(candidate));
  const name = readName(path.join(contents, 'Info.plist')).trim();
  if (!name || name === '.' || name === '..' || /[/\\\0\r\n]/.test(name)) throw new Error('Invalid app executable name.');
  const executable = path.join(contents, 'MacOS', name);
  if (!fs.statSync(executable).isFile()) throw new Error('The app executable is not a regular file.');
  return executable;
}

module.exports = { bundleExecutable };
