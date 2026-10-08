"""Sequential desktop transport with bounded frames and no automatic retries."""
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid


class ConnectionLost(RuntimeError):
    pass


def bundled_core(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / 'package.json').read_text(encoding='utf-8-sig'))
    relative = Path(manifest['core_python'])
    if manifest.get('schema') != 1 or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid core manifest')
    core = (bundle / relative).resolve(strict=True)
    if not core.is_relative_to(bundle) or not core.is_file():
        raise ValueError('Core must be a file inside the bundle')
    if core == Path(sys.executable).resolve() and getattr(sys, 'frozen', False):
        raise ValueError('The GUI executable is not a Python runtime')
    return core


class CoreClient:
    def __init__(self, core, *, source=None, timeout=45):
        core = Path(core).resolve(strict=True)
        self.timeout = timeout
        self.closed = False
        self.lock = threading.Lock()
        self.replies = queue.Queue()
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('PYTHON', '_PYI')) or key == '_MEIPASS2':
                env.pop(key)
        env['PYTHONUTF8'] = '1'
        if source is not None:
            if getattr(sys, 'frozen', False):
                raise ValueError('Source overrides are for development only')
            env['PYTHONPATH'] = str(Path(source).resolve(strict=True))
        kernel = None
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            if not kernel.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.process = subprocess.Popen(
                [str(core), '-B', '-m', 'agent_bridge', 'manage'], cwd=core.parent,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        finally:
            if kernel is not None:
                kernel.SetDllDirectoryW(sys._MEIPASS)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line or len(line) > 1024 * 1024 or not line.endswith(b'\n'):
                    break
                self.replies.put(json.loads(line.decode('utf-8')))
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(None)
            self.process.stdout.close()

    def request(self, action, args):
        with self.lock:
            if self.closed:
                raise ConnectionLost('Core session closed. Inspect state before reconnecting.')
            request_id = uuid.uuid4().hex
            frame = (json.dumps(dict(protocol=1, id=request_id, action=action, args=args)) + '\n').encode('utf-8')
            if len(frame) > 64 * 1024:
                raise ValueError('Request is too large')
            try:
                self.process.stdin.write(frame)
                self.process.stdin.flush()
                reply = self.replies.get(timeout=self.timeout)
                if (not isinstance(reply, dict) or type(reply.get('protocol')) is not int
                        or reply['protocol'] != 1 or reply.get('id') != request_id
                        or type(reply.get('ok')) is not bool
                        or not {'result', 'problem'} <= reply.keys()):
                    raise ValueError('Invalid core response')
                return reply
            except (OSError, ValueError, queue.Empty):
                self.close()
                raise ConnectionLost('Response unavailable. Changes may have completed. '
                                     'Inspect state; do not repeat the operation automatically.') from None

    def close(self):
        """Signal EOF only; do not terminate a pending mutation or its runner."""
        if not self.closed:
            self.closed = True
            try:
                self.process.stdin.close()
            except OSError:
                pass
            threading.Thread(target=self.process.wait, daemon=True).start()


class Worker:
    def __init__(self, factory):
        self.factory = factory
        self.client = None
        self.events = queue.Queue()
        self.busy = False
        self.closed = False

    def submit(self, action, args):
        if self.busy or self.closed:
            raise RuntimeError('A request is already pending or the window is closed')
        self.busy = True
        def run():
            try:
                if self.client is None:
                    self.client = self.factory()
                if self.closed:
                    self.client.close()
                    return
                self.events.put((action, self.client.request(action, args), None))
            except Exception as exc:
                self.events.put((action, None, str(exc)))
            finally:
                if self.closed and self.client is not None:
                    self.client.close()
        threading.Thread(target=run, daemon=True).start()

    def reconnect(self):
        if self.busy:
            raise RuntimeError('Wait for the pending request')
        if self.client is not None:
            if self.client.process.poll() is None:
                self.client.close()
                raise RuntimeError('Core is still exiting. Inspect state and try reconnecting later.')
            self.client.close()
        self.client = None

    def close(self):
        self.closed = True
        if self.client is not None:
            threading.Thread(target=self.client.close, daemon=True).start()
