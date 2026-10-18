const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
const { downloadAndUnzipVSCode, resolveCliArgsFromVSCodeExecutablePath } = require('@vscode/test-electron');
const { nativeTarget } = require('./native-package.cjs');
const root = path.resolve(__dirname, '..'), target = nativeTarget();
const python = require('../test/helpers.cjs').python;
const artifact = path.resolve(process.env.PATCHPORT_ARTIFACT_DIR || path.join(root, '../../.bridge/artifacts'), `patchport-${require('../package.json').version}-${target}.vsix`);
const evidence = path.resolve(process.env.PATCHPORT_HOST_EVIDENCE || path.join(root, '../../.bridge/native-host', target));
fs.mkdirSync(evidence, { recursive: true });

function run(command, args, env, label) {
  const result = spawnSync(command, args, { env, encoding: 'utf8', windowsHide: true, timeout: 180000, maxBuffer: 16 * 1024 * 1024 });
  fs.writeFileSync(path.join(evidence, label + '.log'), (result.stdout || '') + (result.stderr || ''));
  if (result.error || result.status !== 0) throw new Error(`${label} failed: ${result.error || result.status}\n${(result.stderr || result.stdout || '').slice(-4000)}`);
  return result.stdout.trim();
}

(async () => {
  const extract = path.join(evidence, 'verified-' + randomUUID());
  run(python, [path.join(__dirname, 'verify-vsix.py'), artifact, '--target', target, '--extract', extract], process.env, 'package-verification');
  const executable = process.env.PATCHPORT_VSCODE_EXECUTABLE || await downloadAndUnzipVSCode({ version: '1.142.0', cachePath: path.join(evidence, 'vscode') });
  const cleanEnv = { ...process.env }; delete cleanEnv.ELECTRON_RUN_AS_NODE; delete cleanEnv.VSCODE_IPC_HOOK_CLI; delete cleanEnv.VSCODE_PORTABLE;
  if (process.platform === 'linux' && require('node:os').release().toLowerCase().includes('microsoft')) cleanEnv.DONT_PROMPT_WSL_INSTALL = '1';
  const profile = process.platform === 'win32' ? path.join(evidence, 'profile-' + randomUUID()) : fs.mkdtempSync(path.join(process.platform === 'darwin' ? '/private/tmp' : '/tmp', 'pp-host-'));
  const portable = path.join(profile, 'portable'), userData = path.join(portable, 'user-data'), extensions = path.join(portable, 'extensions');
  fs.mkdirSync(profile, { recursive: true });
  fs.writeFileSync(path.join(evidence, 'profile.json'), JSON.stringify({ profile, target }, null, 2));
  fs.mkdirSync(portable);
  if (process.platform !== 'darwin') cleanEnv.VSCODE_PORTABLE = portable;
  const [resolvedCli, ...resolvedArgs] = resolveCliArgsFromVSCodeExecutablePath(executable);
  const cli = process.platform === 'win32' ? executable : resolvedCli;
  let cliArgs = resolvedArgs;
  if (process.platform === 'win32') {
    // Updated Windows installs can keep app resources under a commit directory.
    const wrapper = fs.readFileSync(resolvedCli, 'utf8');
    const match = wrapper.match(/"%~dp0([^"\r\n]*resources[\\/]app[\\/]out[\\/]cli\.js)"/i);
    if (!match) throw new Error('Unrecognized VS Code CLI wrapper.');
    const script = path.resolve(path.dirname(resolvedCli), match[1]);
    if (path.relative(path.dirname(executable), script).startsWith('..') || !fs.statSync(script).isFile()) throw new Error('Unsafe VS Code CLI entry point.');
    cliArgs = [script];
  }
  const profileArgs = ['--user-data-dir', userData, '--extensions-dir', extensions];
  const sandboxArgs = process.platform === 'linux' ? ['--no-sandbox'] : [];
  run(cli, [...cliArgs, ...profileArgs, ...sandboxArgs, '--install-extension', artifact, '--force'], { ...cleanEnv, ELECTRON_RUN_AS_NODE: '1' }, 'isolated-install');
  const installed = run(cli, [...cliArgs, ...profileArgs, ...sandboxArgs, '--locate-extension', 'utialy.patchport'], { ...cleanEnv, ELECTRON_RUN_AS_NODE: '1' }, 'locate-extension').split(/\r?\n/).at(-1);
  if (path.relative(extensions, installed).startsWith('..')) throw new Error('Extension was not installed into the isolated directory.');
  const ptyOutput = run(executable, [path.join(__dirname, 'packaged-pty.cjs'), installed], { ...cleanEnv, ELECTRON_RUN_AS_NODE: '1', PATCHPORT_TEST_NODE: process.execPath, PATCHPORT_HOST_EVIDENCE: evidence }, 'electron-pty');
  const workspace = path.join(profile, 'fixture project'); fs.mkdirSync(workspace);
  run(executable, [workspace, ...profileArgs, ...sandboxArgs, '--new-window', '--disable-gpu', '--disable-extensions', '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust',
    '--extensionDevelopmentPath=' + installed, '--extensionTestsPath=' + path.join(__dirname, 'host-test.cjs')],
    { ...cleanEnv, PATCHPORT_TEST_PYTHON: python, PATCHPORT_INSTALLED_EXTENSION: installed, PATCHPORT_HOST_EVIDENCE: evidence }, 'extension-host');
  const report = JSON.parse(fs.readFileSync(path.join(evidence, 'host-result.json'), 'utf8'));
  if (!report.passed || report.target !== target) throw new Error('Missing actual host result.');
  console.log(ptyOutput); console.log(JSON.stringify(report));
})().catch(error => { console.error(error); process.exitCode = 1; });
