"""Owned supervisor. Parent pipe EOF cancels the complete owned process group/job."""
import ctypes
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import contextlib

_JOB = None

PROFILE_SHELLS = ('powershell', 'pwsh', 'bash', 'zsh')


def validate_launch(launch):
    """Validate explicit shell settings without loading user profiles."""
    if launch is None:
        return
    if (not isinstance(launch, dict) or not launch
            or launch.keys() - {'shell', 'executable', 'profile'}
            or launch.get('shell') not in PROFILE_SHELLS):
        raise ValueError('launch requires shell: powershell, pwsh, bash or zsh')
    for key in ('executable', 'profile'):
        if key in launch and (not isinstance(launch[key], str) or not launch[key].strip()
                              or any(c in launch[key] for c in ('\x00', '\r', '\n'))):
            raise ValueError('launch ' + key + ' must be a nonempty path or name')
    if 'profile' in launch and not Path(launch['profile']).is_absolute():
        raise ValueError('launch profile must be an absolute path')


def profile_argv(argv, launch, cwd=None):
    """Build an explicit shell invocation; provider arguments remain literals."""
    validate_launch(launch)
    shell = launch['shell']
    executable = shutil.which(launch.get('executable', shell))
    if not executable:
        raise ValueError('Profile shell executable not found: ' + launch.get('executable', shell))
    if os.name == 'nt' and Path(executable).suffix.lower() != '.exe':
        raise ValueError('Select a native shell executable')
    executable = str(Path(executable).resolve())
    working_directory = str(Path(cwd or Path.cwd()).resolve())
    profile = launch.get('profile')
    if profile and not Path(profile).is_file():
        raise ValueError('Selected shell profile is missing')
    if not all(isinstance(a, str) and '\x00' not in a for a in argv):
        raise ValueError('Command arguments must be strings without NUL')
    if shell in ('powershell', 'pwsh'):
        def literal(value):
            return "'" + value.replace("'", "''") + "'"
        script = "$ErrorActionPreference = 'Stop'; "
        script += '[Console]::InputEncoding = [Console]::OutputEncoding = $OutputEncoding = [System.Text.UTF8Encoding]::new($false); '
        script += 'try { '
        if profile:
            script += '. ' + literal(profile) + '; '
        script += 'Set-Location -LiteralPath ' + literal(working_directory) + '; '
        script += '$global:LASTEXITCODE = 0; $bridgeArgs = @(' + ','.join(literal(a) for a in argv[1:]) + '); '
        script += '& ' + literal(argv[0]) + ' @bridgeArgs; '
        script += 'if (-not $?) { exit 1 }; exit $global:LASTEXITCODE } catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }'
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        return [executable, '-NoLogo', '-NonInteractive', *(['-NoProfile'] if profile else []),
                '-EncodedCommand', encoded]
    # Evaluate only generated, quoted argv after startup so aliases also expand.
    invocation = shlex.join(argv)
    script = 'shopt -s expand_aliases; ' if shell == 'bash' else ''
    if profile:
        script += '. ' + shlex.quote(profile) + ' || exit $?; '
    script += 'cd -- ' + shlex.quote(working_directory) + ' || exit $?; '
    script += 'eval ' + shlex.quote(invocation)
    flags = (['--noprofile', '--norc', '-c'] if shell == 'bash' else ['-f', '-c']) if profile else ['-lic']
    return [executable, *flags, script]


@contextlib.contextmanager
def drain_on_termination():
    """Record SIGTERM in the main thread; database work stays in the runner loop."""
    requested = [False]
    previous = None
    enabled = os.name == 'posix' and threading.current_thread() is threading.main_thread()
    if enabled:
        previous = signal.getsignal(signal.SIGTERM)
        def request(signum, frame):
            requested[0] = True
        signal.signal(signal.SIGTERM, request)
    try:
        yield lambda: requested[0]
    finally:
        if enabled:
            signal.signal(signal.SIGTERM, previous)


def systemd_user(arguments, *, check=True):
    """Invoke only the system systemctl executable and the current user's manager."""
    if sys.platform != 'linux' or os.getuid() == 0:
        raise RuntimeError('A non-root Linux systemd user session is required')
    executable = next((p for p in (Path('/usr/bin/systemctl'), Path('/bin/systemctl')) if p.is_file()), None)
    if executable is None:
        raise RuntimeError('systemctl is unavailable; use the manual runner commands')
    result = subprocess.run([str(executable), '--user', '--no-pager', *arguments],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=35,
                            env=dict(os.environ, SYSTEMD_PAGER='', SYSTEMD_COLORS='0'))
    if len(result.stdout) > 256 * 1024:
        raise RuntimeError('Service manager response too large; inspect with systemctl --user')
    if check and result.returncode:
        raise RuntimeError('systemctl --user failed; inspect service status before another action')
    return result.returncode, result.stdout.decode('utf-8', errors='replace')


def launchctl_user(arguments, *, check=True, discard_output=False):
    """Use the native launchctl in the current non-root GUI user domain only."""
    if sys.platform != 'darwin' or os.getuid() == 0:
        raise RuntimeError('A non-root macOS GUI user session is required')
    domain = 'gui/' + str(os.getuid())
    if (len(arguments) < 2 or arguments[0] not in ('print', 'print-disabled', 'bootstrap', 'bootout', 'kickstart')
            or not (arguments[1] == domain or arguments[1].startswith(domain + '/'))):
        raise ValueError('Only explicit operations in the current GUI domain are allowed')
    result = subprocess.run(['/bin/launchctl', *arguments], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL if discard_output else subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=35)
    output = result.stdout or b''
    if len(output) > 256 * 1024 or len(result.stderr) > 256 * 1024:
        raise RuntimeError('launchctl response too large; inspect in the native CLI')
    if check and result.returncode:
        raise RuntimeError('launchctl failed; inspect state before another action')
    return result.returncode, output.decode('utf-8', errors='strict'), result.stderr.decode('utf-8', errors='replace')


def verify_service_runtime(python, code_identity):
    """Require the isolated installed package to match the registering runtime."""
    with external_environment() as env:
        result = subprocess.run([str(python), '-I', '-B', '-c',
            'import json; from agent_bridge.health import code_identity; import agent_bridge.service_manager; '
            'print(json.dumps(code_identity()))'], env=env, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or json.loads(result.stdout) != code_identity:
        raise ValueError('Install the current wheel in the selected Python before registering a service')


@contextlib.contextmanager
def external_environment():
    """Keep frozen build paths out of explicitly selected external programs."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('PYTHON', '_PYI')) or key == '_MEIPASS2':
            env.pop(key)
    env['PYTHONUTF8'] = '1'
    kernel = None
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel.SetDllDirectoryW.restype = ctypes.c_int
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield env
    finally:
        if kernel is not None:
            kernel.SetDllDirectoryW(sys._MEIPASS)


def verify_installed_core(python):
    """Import the installed management API without authentication or AI calls."""
    code = ('import json,sys; from agent_bridge import management; '
            'print(json.dumps(dict(protocol=management.PROTOCOL, isolated=sys.flags.isolated)))')
    with external_environment() as env:
        result = subprocess.run([str(python), '-B', '-c', code], cwd=python.parent,
                                env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or json.loads(result.stdout).get('protocol') != 1:
        raise RuntimeError('Installed core verification failed; preserve this incomplete installation')


def create_windows_shortcut(path, target):
    """Create only the explicitly named installer-owned shortcut; never run it."""
    if os.name != 'nt':
        raise RuntimeError('Windows shortcuts require Windows')
    script = ("$ErrorActionPreference='Stop'; "
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PATCHPORT_LINK_PATH); "
              "$s.TargetPath=$env:PATCHPORT_LINK_TARGET; "
              "$s.WorkingDirectory=[IO.Path]::GetDirectoryName($env:PATCHPORT_LINK_TARGET); $s.Save()")
    with external_environment() as env:
        env.update(PATCHPORT_LINK_PATH=str(path), PATCHPORT_LINK_TARGET=str(target))
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not path.is_file():
        raise RuntimeError('Shortcut creation failed; inspect the partial installation')


def start_background(argv, cwd, log, env):
    """Detach a runner from its caller without changing provider ownership."""
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True})
    with open(log, 'xb') as stream:
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stream, stderr=stream, shell=False, **options)

def windows_job():
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64), ("Flags", w.DWORD), ("Min", ctypes.c_size_t), ("Max", ctypes.c_size_t), ("Active", w.DWORD), ("Affinity", ctypes.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [("v" + str(i), ctypes.c_uint64) for i in range(6)]
    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t), ("PeakProcess", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]; k.SetInformationJobObject.restype = w.BOOL
    k.GetCurrentProcess.restype = w.HANDLE
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]; k.AssignProcessToJobObject.restype = w.BOOL
    k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    job = k.CreateJobObjectW(None, None)
    info = Extended(); info.Basic.Flags = 0x2000
    if not job or not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return k, job

def stop():
    if os.name == "nt":
        if _JOB: _JOB[0].TerminateJobObject(_JOB[1], 124)
    else:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    os._exit(124)

def main():
    global _JOB
    # Establish ownership before any provider process can be created.
    if os.name == "nt": _JOB = windows_job()
    else: os.setsid()
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if sys.stdin.buffer.readline() != b"GO\n": return 124
    def watch():
        sys.stdin.buffer.read()
        stop()
    threading.Thread(target=watch, daemon=True).start()
    try:
        with open(spec["prompt_file"], "rb") as prompt:
            proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdin=prompt, shell=False)
            code = proc.wait()
        # The Windows job closes on exit; POSIX cleanup kills any remaining descendants.
        if os.name != "nt":
            Path(spec["exit_file"]).write_text(str(code), encoding="ascii")
            stop()
        os._exit(code if code >= 0 else 1)
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr, flush=True)
        os._exit(1)

if __name__ == "__main__":
    raise SystemExit(main())
