import type { Profile } from './model';

export const nativeTargets = ['win32-x64', 'linux-x64', 'linux-arm64', 'darwin-x64', 'darwin-arm64'] as const;

export function supportedLocal(platform = process.platform, arch = process.arch, remote = ''): boolean {
  return !remote && nativeTargets.some(target => target === `${platform}-${arch}`);
}

export function defaultShell(platform = process.platform): Profile['shell'] {
  return platform === 'win32' ? 'powershell' : platform === 'darwin' ? 'zsh' : 'bash';
}

export function defaultPython(platform = process.platform): string {
  return platform === 'win32' ? 'python' : 'python3';
}

export function defaultProfiles(platform = process.platform): Profile[] {
  const shell = defaultShell(platform);
  return [
    { id: 'claude', name: 'Claude Code', provider: 'claude', command: 'claude', args: [], shell },
    { id: 'codex', name: 'Codex', provider: 'codex', command: 'codex', args: [], shell },
    { id: 'shell', name: 'Shell', provider: 'terminal', command: platform === 'win32' ? 'powershell.exe' : shell, args: [], shell: 'direct' }
  ];
}
