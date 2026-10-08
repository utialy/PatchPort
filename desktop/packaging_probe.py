"""Diagnostic GUI for packaging validation; not the product management UI."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    bundle = Path(sys.executable).resolve().parent.parent
    core = bundle / 'core/python.exe'
    mailbox = queue.Queue()
    root = tk.Tk()
    root.title('PatchPort packaging check')
    root.geometry('540x220')
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='PatchPort', font=('Segoe UI', 20, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Checking the isolated runtime. No AI calls.').pack(anchor='w', pady=12)
    status = tk.StringVar(value='Starting local check...')
    ttk.Label(frame, textvariable=status, wraplength=480).pack(anchor='w')

    def worker():
        try:
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(('PYTHON', '_PYI')) or key == '_MEIPASS2': env.pop(key, None)
            env['PYTHONUTF8'] = '1'
            # A frozen GUI must not pass its private DLL search directory to core.
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            if not kernel.SetDllDirectoryW(None): raise ctypes.WinError(ctypes.get_last_error())
            try:
                result = subprocess.run([str(core), '-c',
                    'import json,sys,sqlite3,agent_bridge; print(json.dumps(dict(python=sys.executable, package=agent_bridge.__file__, isolated=sys.flags.isolated, sqlite=sqlite3.sqlite_version)))'],
                    cwd=bundle, env=env, capture_output=True, text=True, encoding='utf-8',
                    timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                if getattr(sys, 'frozen', False): kernel.SetDllDirectoryW(sys._MEIPASS)
            if result.returncode: raise RuntimeError(result.stderr)
            mailbox.put(dict(ok=True, core=json.loads(result.stdout)))
        except Exception as exc:
            mailbox.put(dict(ok=False, error=str(exc)))

    def poll():
        try: report = mailbox.get_nowait()
        except queue.Empty:
            root.after(50, poll)
            return
        report.update(gui_frozen=bool(getattr(sys, 'frozen', False)), gui_executable=sys.executable,
                      tkinter_version=root.tk.call('info', 'patchlevel'), mapped=bool(root.winfo_ismapped()),
                      width=root.winfo_width(), height=root.winfo_height(),
                      elapsed_seconds=round(time.perf_counter() - started, 3))
        status.set('Local runtime check passed.' if report['ok'] else report['error'])
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
        root.after(500, root.destroy)

    def begin():
        threading.Thread(target=worker, daemon=True).start()
        poll()
    root.after(200, begin)
    root.mainloop()


if __name__ == '__main__':
    main()
