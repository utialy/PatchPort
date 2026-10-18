const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const targets = ['win32-x64', 'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64'];

function nativeTarget(requested, platform = process.platform, arch = process.arch) {
  const current = `${platform}-${arch}`, target = requested || current;
  if (!targets.includes(target)) throw new Error(`Unsupported native target: ${target}`);
  if (target !== current) throw new Error(`Build ${target} on a matching native host; current host is ${current}.`);
  return target;
}

function checked(root, relative) {
  const file = path.resolve(root, relative);
  if (path.relative(root, file).startsWith('..') || !path.relative(root, file)) throw new Error('Unsafe native asset path.');
  for (let item = file; item !== root; item = path.dirname(item)) {
    if (fs.existsSync(item) && fs.lstatSync(item).isSymbolicLink()) throw new Error('Linked native assets are not supported.');
  }
  return file;
}

function prepareNative(root, target) {
  nativeTarget(target);
  const base = 'node_modules/node-pty', nodePty = path.join(root, base);
  if (require(path.join(nodePty, 'package.json')).version !== '1.1.0') throw new Error('Review native packaging before updating node-pty.');
  const windows = target.startsWith('win32-');
  const required = windows
    ? ['pty.node', 'conpty.node', 'conpty_console_list.node', 'winpty-agent.exe', 'winpty.dll', 'conpty/conpty.dll', 'conpty/OpenConsole.exe']
    : target.startsWith('darwin-') ? ['pty.node', 'spawn-helper'] : ['pty.node'];
  const prefix = `${base}/prebuilds/${target}`;
  for (const relative of required) {
    const destination = checked(root, `${prefix}/${relative}`);
    if (!fs.existsSync(destination)) {
      const source = checked(root, `${base}/build/Release/${relative}`);
      if (!fs.existsSync(source) || !fs.statSync(source).isFile()) throw new Error(`Missing native asset ${relative}; run npm ci on ${target}.`);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.copyFileSync(source, destination);
    }
    if (!fs.statSync(destination).isFile()) throw new Error('Native asset is not a regular file.');
    if (relative === 'spawn-helper') fs.chmodSync(destination, 0o755);
  }
  const files = required.map(relative => {
    const name = `${prefix}/${relative}`, file = checked(root, name);
    return { path: name, sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') };
  });
  fs.writeFileSync(path.join(root, 'native-manifest.json'), JSON.stringify({ schema: 1, target, node_pty: '1.1.0', files }, null, 2) + '\n');
  const generated = path.join(root, '.bridge');
  fs.mkdirSync(generated, { recursive: true });
  const ignoreFile = path.join(generated, `package-${target}.ignore`);
  const ignored = fs.readdirSync(path.join(nodePty, 'prebuilds')).filter(name => name !== target).map(name => {
    if (!/^[a-z0-9-]+$/.test(name)) throw new Error('Unexpected native directory name.');
    return `${base}/prebuilds/${name}/**`;
  });
  // The pinned package's debug symbols are intentionally excluded from the VSIX.
  fs.writeFileSync(ignoreFile, fs.readFileSync(path.join(root, '.vscodeignore'), 'utf8') + '\n.bridge/**\n' + ignored.join('\n') + '\n');
  return { ignoreFile, files };
}

module.exports = { targets, nativeTarget, prepareNative };
