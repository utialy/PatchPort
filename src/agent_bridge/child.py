"""Owned supervisor. Parent pipe EOF cancels the complete owned process group/job."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

_JOB = None

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
