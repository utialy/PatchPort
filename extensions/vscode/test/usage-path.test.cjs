const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const {claudeUsage} = require('../dist/test/providerUsage');

test('usage accepts Windows casing and rejects linked ancestors', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'patchport-usage-path-'));
  t.after(() => fs.rm(root, {recursive:true,force:true}));
  const folder = path.join(root, 'actual'); await fs.mkdir(folder);
  const id = '11111111-1111-4111-8111-111111111111';
  const file = path.join(folder, id + '.jsonl');
  await fs.writeFile(file, JSON.stringify({type:'assistant',sessionId:id,timestamp:new Date().toISOString(),message:{id:'one',model:'fixture',usage:{input_tokens:2,output_tokens:3,cache_read_input_tokens:0,cache_creation_input_tokens:0}}})+'\n');
  const lookup = process.platform === 'win32' ? path.join(folder.toUpperCase(), path.basename(file)) : file;
  assert.equal((await claudeUsage(lookup,id,0)).metrics.input_tokens,2);
  const link = path.join(root, 'linked');
  await fs.symlink(folder,link,process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(claudeUsage(path.join(link,path.basename(file)),id,0),/Unsupported usage file/);
});
