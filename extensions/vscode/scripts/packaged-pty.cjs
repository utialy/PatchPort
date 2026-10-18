const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const installed = path.resolve(process.argv[2]);
const manifest = require(path.join(installed, 'native-manifest.json'));
assert.equal(manifest.target, `${process.platform}-${process.arch}`);
const pty = require(path.join(installed, 'node_modules/node-pty'));
const program = `process.stdin.setRawMode(true); process.stdin.setEncoding('utf8'); process.stdout.write('PTY_READY\\r\\n'); let text=''; process.stdin.on('data', data => { text+=data; if(text.includes('\\r')) { process.stdout.write(text.includes('unicode-\\uD55C\\uAE00')?'PTY_PASS\\r\\n':'PTY_FAIL\\r\\n'); process.exit(text.includes('unicode-\\uD55C\\uAE00')?0:2); } });`;
const fixture = path.join(process.env.PATCHPORT_HOST_EVIDENCE, 'pty-child.cjs');
fs.writeFileSync(fixture, program);
assert.ok(process.env.PATCHPORT_TEST_NODE);
let output = '', sent = false;
const child = pty.spawn(process.env.PATCHPORT_TEST_NODE, [fixture], { name: 'xterm-256color', cols: 80, rows: 24, cwd: installed, env: process.env });
const timer = setTimeout(() => { child.kill(); console.error('Packaged PTY timed out.'); process.exitCode = 1; }, 20000);
child.onData(data => {
  output += data;
  if (!sent && output.includes('PTY_READY')) { sent = true; child.resize(91, 29); child.write('unicode-\uD55C\uAE00\r'); }
});
child.onExit(({ exitCode }) => {
  clearTimeout(timer);
  try {
    assert.equal(exitCode, 0); assert.match(output, /PTY_PASS/);
    if (process.platform === 'darwin') assert.ok(fs.statSync(path.join(installed, 'node_modules/node-pty/prebuilds', manifest.target, 'spawn-helper')).mode & 0o111);
    console.log(JSON.stringify({ passed: true, target: manifest.target, electron: process.versions.electron, node: process.versions.node, resize: true, unicode: true, actual_ai_calls: 0 }));
  } catch (error) { console.error(error); console.error(output); process.exitCode = 1; }
});
