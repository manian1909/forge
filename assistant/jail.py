"""Run a command so it can read everything but write only inside one folder (Windows low-integrity process).

Windows blocks a low-integrity process from writing to anything that is not labelled low, and its token carries no
admin rights, so a UAC elevation prompt cannot be approved from inside it either. The sandbox folder is labelled low
once (``icacls ... /setintegritylevel``); every command the agent runs is started with a low-integrity copy of the
user's own token. Reading is unchanged, so the model can still look at any folder.

This is enforced by the operating system, not by checking the command text.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
LOW_SID = "S-1-16-4096"

if IS_WINDOWS:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                              ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                                              wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFO),
                                              ctypes.POINTER(PROCESS_INFORMATION)]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                                    ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD]
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]


def _low_token():
    token = wintypes.HANDLE()
    # TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT | TOKEN_ASSIGN_PRIMARY
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0002 | 0x0008 | 0x0080 | 0x0001, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    low = wintypes.HANDLE()
    if not advapi32.DuplicateTokenEx(token, 0x02000000, None, 2, 1, ctypes.byref(low)):   # MAXIMUM_ALLOWED, primary
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.CloseHandle(token)
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(LOW_SID, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    label = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(sid, 0x20))   # SE_GROUP_INTEGRITY
    size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
    if not advapi32.SetTokenInformation(low, 25, ctypes.byref(label), size):   # TokenIntegrityLevel
        raise ctypes.WinError(ctypes.get_last_error())
    return low


def _env_block(env):
    text = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


def run_low(argv, cwd, env=None, timeout=120):
    """Run ``argv`` at low integrity and return (exit_code, output). Raises TimeoutError after ``timeout`` seconds."""
    env = dict(os.environ if env is None else env)
    cmdline = subprocess.list2cmdline(argv)
    token = _low_token()
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    rd, wr = wintypes.HANDLE(), wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(rd), ctypes.byref(wr), ctypes.byref(sa), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.SetHandleInformation(rd, 1, 0)                       # our read end is not inherited
    nul = kernel32.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(sa), 3, 0, None)   # stdin: nothing to read
    si = STARTUPINFO()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = 0x100 | 0x1                                       # STARTF_USESTDHANDLES | USESHOWWINDOW
    si.wShowWindow = 0
    si.hStdInput, si.hStdOutput, si.hStdError = nul, wr, wr
    pi = PROCESS_INFORMATION()
    flags = 0x08000000 | 0x00000400                                # CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT
    ok = advapi32.CreateProcessAsUserW(token, None, ctypes.create_unicode_buffer(cmdline), None, None, True, flags,
                                       _env_block(env), str(cwd), ctypes.byref(si), ctypes.byref(pi))
    err = ctypes.get_last_error()
    kernel32.CloseHandle(wr)
    kernel32.CloseHandle(nul)
    kernel32.CloseHandle(token)
    if not ok:
        kernel32.CloseHandle(rd)
        raise ctypes.WinError(err)
    chunks = []

    def pump():
        buf = ctypes.create_string_buffer(65536)
        got = wintypes.DWORD()
        while kernel32.ReadFile(rd, buf, 65536, ctypes.byref(got), None) and got.value:
            chunks.append(buf.raw[:got.value])

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    wait_ms = int(timeout * 1000)
    if kernel32.WaitForSingleObject(pi.hProcess, wait_ms) != 0:    # WAIT_OBJECT_0
        kernel32.TerminateProcess(pi.hProcess, 1)
        _kill_tree(pi.dwProcessId)
        kernel32.CloseHandle(pi.hProcess)
        kernel32.CloseHandle(pi.hThread)
        t.join(2)
        kernel32.CloseHandle(rd)
        raise TimeoutError(f"timed out after {timeout}s")
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    t.join(5)
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(rd)
    return code.value, b"".join(chunks).decode("utf-8", errors="replace")


def _kill_tree(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def label_folder(folder):
    """Mark a folder (and everything created in it from now on) as writable by low-integrity processes. Needs no admin."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["icacls", str(folder), "/setintegritylevel", "(OI)(CI)L"], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(done.stdout + done.stderr)


def is_labelled(folder):
    done = subprocess.run(["icacls", str(folder)], capture_output=True, text=True)
    return "Low Mandatory Level" in done.stdout


def available():
    return IS_WINDOWS


import re

# Things that would ask Windows for administrator rights (a UAC prompt) or install software outside the folder. The
# low-integrity token already stops them from doing any damage, but a hung prompt or a half-run install is no use to anyone.
ELEVATE_RE = re.compile(
    r"-Verb\s+['\"]?RunAs|\brunas(\.exe)?\b|\bsudo\b|\bgsudo\b|\bpsexec\b|Set-ExecutionPolicy|"
    r"\bwinget\b|\bchoco(latey)?\b|\bscoop\b|\bmsiexec\b|Install-(Module|Package|Script)|"
    r"\bpip3?(\.exe)?\s+(install|uninstall)\b|-m\s+pip\s+(install|uninstall)\b|\bnpm\s+(i|install)\s+(-g|--global)", re.I)

DENIED_MSG = ("Blocked: that needs administrator rights or installs software outside the jarvis folder, which jarvis "
              "is not allowed to do. Do the job with what is installed, or tell the user what to install themselves.")


def shell_denied(command):
    return DENIED_MSG if ELEVATE_RE.search(command or "") else None


GUARD_DIR = Path(__file__).resolve().parent / "jail_site"
PYLIB = Path(__file__).resolve().parent / "pylib"   # forge_office and friends


def sandbox_env(root, base=None):
    """Environment for a jailed process: scratch files go inside the folder (the rest of the disk is read-only to it)."""
    root = Path(root)
    scratch = root / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ if base is None else base)
    env.update(TEMP=str(scratch), TMP=str(scratch), MPLCONFIGDIR=str(scratch / "mpl"), PYTHONDONTWRITEBYTECODE="1",
               PYTHONUTF8="1", PIP_NO_INPUT="1", PYTHONPATH=os.pathsep.join(p for p in (str(GUARD_DIR), str(PYLIB), env.get("PYTHONPATH", "")) if p))
    return env
