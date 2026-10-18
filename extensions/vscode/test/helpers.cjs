const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const root = path.resolve(__dirname, '..');
module.exports = {
  python: process.env.PATCHPORT_TEST_PYTHON || path.resolve(root, '../../.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  temporaryRoot: fs.realpathSync(os.tmpdir())
};
