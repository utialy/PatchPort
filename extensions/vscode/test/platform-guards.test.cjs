const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs/promises'), path = require('node:path');
const { CoreClient } = require('../dist/test/core');
const { python, temporaryRoot } = require('./helpers.cjs');

test('POSIX inspection retains the existing batch-body and connection-removal guards without creating state', { skip: process.platform === 'win32' }, async t => {
  const project = await fs.mkdtemp(path.join(temporaryRoot, 'patchport-platform-'));
  const config = path.join(project, 'bridge.json');
  await fs.writeFile(path.join(project, 'input.txt'), 'ORIGINAL');
  await fs.writeFile(config, JSON.stringify({ project: '.', include: ['input.txt'], writable: ['input.txt'], endpoints: { mock: { adapter: 'command', command: [python, '-c', 'pass'] } } }));
  const core = new CoreClient(python, path.resolve(__dirname, '../core'));
  t.after(async () => { core.dispose(); await fs.rm(project, { recursive: true, force: true }); });
  const before = await fs.readdir(project);
  const records = await core.request('records.prepare.preview', { project, config });
  assert.ok(records.report.blockers.includes('RECORDS_PLATFORM_UNSUPPORTED'));
  assert.equal(records.plan_id, null);
  const connection = await core.request('connection.remove.preview', { project, config, paths: ['bridge.json'] });
  assert.ok(connection.report.blockers.includes('CONNECTION_PLATFORM_UNSUPPORTED'));
  assert.equal(connection.plan_id, null);
  assert.deepEqual(await fs.readdir(project), before);
});
