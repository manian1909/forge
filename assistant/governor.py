"""Share the laptop politely: jarvis steps aside when another program needs the memory, then carries on.

The model server holds about 3.7 GB of RAM and most of the 4 GB GPU while it is loaded. When free RAM drops below
``pause_mb``, a program from the ``heavy`` list (your games) is running, another program keeps the GPU busy (a game
not on the list), or a full-screen app is in front, the governor:

1. stops the reply being generated (the conversation so far is already saved, see tasks.py),
2. tells Ollama to unload the model, which hands its RAM and VRAM back to the other program,
3. waits until there is ``resume_mb`` free and nothing heavy is running, steadily for ``settle`` seconds,
4. loads the model again and repeats the reply that was interrupted.

So a task only loses the one reply that was being written when it paused; everything before it is kept.
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time

import httpx

from .serve import is_ollama, loaded_models, root_url


class Paused(Exception):
    """Raised inside a streaming reply when the governor wants the memory back."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def free_mb():
    """Physical RAM free right now, in MB (None when it cannot be read)."""
    if sys.platform != "win32":
        try:
            import os
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 2**20
        except (ValueError, OSError, AttributeError):
            return None
    st = _MemoryStatus()
    st.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return st.ullAvailPhys // 2**20


def running_programs():
    """Lower-case image names of running processes (tasklist needs no admin rights)."""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="replace", creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def fullscreen_app():
    """True when the window in front covers the whole screen (a game, a full-screen video), not the desktop itself."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):     # the desktop and the taskbar
        return False
    r = _Rect()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left <= 0 and r.top <= 0 and r.right >= user32.GetSystemMetrics(0) and r.bottom >= user32.GetSystemMetrics(1)


def program_name(pid):
    """The exe name of a process id, or '' (works for other users' games too, without admin rights)."""
    if sys.platform != "win32":
        return ""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(520), ctypes.c_ulong(520)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    k32.CloseHandle(h)
    return buf.value.rsplit("\\", 1)[-1].lower() if ok else ""


class GpuWatch:
    """How hard each program is driving the NVIDIA GPU, as the driver reports it (`nvidia-smi pmon`, about 0.1 s).

    A game keeps the GPU busy the whole time it is played. Windows' own GPU-engine counters were tried first and
    under-report on this laptop (a test program at 21% in the driver showed 0.4% there). The model's server is left out.
    """
    IGNORE = {"ollama.exe", "ollama app.exe", "llama-server.exe", "ollama_llama_server.exe", "dwm.exe"}

    def __init__(self, exe=None):
        self.exe = exe or shutil.which("nvidia-smi")
        self.ok = bool(self.exe)

    def read(self):
        """{program: GPU % (SM use)} right now; {} when there is no NVIDIA driver."""
        if not self.ok:
            return {}
        try:
            out = subprocess.run([self.exe, "pmon", "-c", "1", "-s", "u"], capture_output=True, text=True, timeout=10,
                                 encoding="utf-8", errors="replace", creationflags=0x08000000 if sys.platform == "win32" else 0).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        return parse_pmon(out, self.IGNORE)


def parse_pmon(text, ignore=(), name_of=None):
    """`nvidia-smi pmon -s u` rows -> {program: sm %}. Names come from the pid (pmon cuts them to 16 characters)."""
    name_of = name_of or program_name
    out = {}
    for line in text.splitlines():
        cols = line.split()
        if line.startswith("#") or len(cols) < 5 or not cols[1].isdigit() or not cols[3].isdigit():
            continue
        pid, sm = int(cols[1]), int(cols[3])
        if pid == os.getpid():
            continue
        name = name_of(pid) or cols[-1].lower()
        if name not in ignore:
            out[name] = out.get(name, 0) + sm
    return out


class Governor:
    """Decides when jarvis may use the model, and frees the model's memory while it may not."""

    def __init__(self, url, model, pause_mb=1500, resume_mb=5000, heavy=(), fullscreen=True, settle=30,
                 check_every=2.0, say=lambda m: None, probe=None, sleep=time.sleep, gpu_pct=15):
        self.url, self.model = url, model
        self.pause_mb, self.resume_mb = pause_mb, resume_mb
        self.heavy = {h.lower() if h.lower().endswith(".exe") else h.lower() + ".exe" for h in heavy}
        self.fullscreen, self.settle, self.check_every = fullscreen, settle, check_every
        self.say = say
        self.probe = probe          # tests pass a fake: () -> (free_mb, programs, fullscreen[, {program: gpu %}])
        self.sleep = sleep
        self.gpu_pct = gpu_pct      # a program keeping the GPU this busy on two readings in a row is treated as a game
        self.gpu = GpuWatch() if gpu_pct and not probe else None
        self._gpu_now, self._gpu_before, self._gpu_at = {}, {}, 0.0
        self._last_check, self._last_reason = 0.0, None
        self._programs, self._programs_at = set(), 0.0
        self._resumed_at, self._backoff = 0.0, 1
        self.pauses = 0

    def _readings(self):
        if self.probe:
            r = self.probe()
            self._gpu_before, self._gpu_now = self._gpu_now, (r[3] if len(r) == 4 else {})
            return r[:3]
        if self.heavy and time.time() - self._programs_at > 10:      # tasklist takes ~0.4 s, so not on every check
            self._programs, self._programs_at = running_programs(), time.time()
        if self.gpu and time.time() - self._gpu_at > 5:              # a fresh GPU reading every 5 s is plenty
            self._gpu_before, self._gpu_now, self._gpu_at = self._gpu_now, self.gpu.read(), time.time()
        return free_mb(), (self._programs if self.heavy else set()), (self.fullscreen and fullscreen_app())

    def _gamer(self):
        """A program over gpu_pct on the last two readings, so a short burst (a video starting) does not count."""
        hit = [(p, v) for p, v in self._gpu_now.items()
               if self.gpu_pct and v >= self.gpu_pct and self._gpu_before.get(p, 0) >= self.gpu_pct]
        return max(hit, key=lambda x: x[1]) if hit else None

    def model_loaded(self):
        if self.probe or not is_ollama(self.url):
            return False
        return any(n == self.model or n.split(":")[0] == self.model for n in loaded_models(self.url))

    def busy_reason(self, need_mb=None):
        """Why jarvis should not run now, or None. ``need_mb`` is the free RAM required (pause or resume level)."""
        free, programs, full = self._readings()
        hit = sorted(self.heavy & programs)
        if hit:
            return f"{hit[0]} is running"
        game = self._gamer()
        if game:
            return f"{game[0]} is using the GPU ({game[1]:.0f}%)"
        if full:
            return "a full-screen app is open"
        if free is not None and free < (need_mb or self.pause_mb):
            return f"only {free / 1024:.1f} GB of RAM free"
        return None

    def check(self):
        """Called on every streamed chunk: raises Paused when the memory is needed elsewhere (at most every few seconds)."""
        now = time.time()
        if now - self._last_check < self.check_every:
            return
        self._last_check = now
        reason = self.busy_reason()
        if reason:
            raise Paused(reason)

    # -------------------------------------------------------------- the model's memory
    def unload(self):
        """Ask Ollama to drop the model from RAM and VRAM now (other servers cannot be asked, so nothing happens)."""
        if not is_ollama(self.url):
            return False
        try:
            httpx.post(f"{root_url(self.url)}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=60)
            return True
        except httpx.HTTPError:
            return False

    def wait_for_room(self, reason):
        """Free the model, then block until the machine has been quiet long enough. Returns seconds waited."""
        sleep = self.sleep
        self.pauses += 1
        start = time.time()
        # paused again soon after resuming: the other program is still around, so wait longer each time (no load/unload ping-pong)
        self._backoff = min(self._backoff * 2, 32) if start - self._resumed_at < 300 else 1
        settle = self.settle * self._backoff
        self.unload()
        self.say(f"paused: {reason}. The model is unloaded so your other apps get the memory; "
                 f"I carry on when {self.resume_mb / 1024:.1f} GB is free and nothing heavy has run for {settle:.0f}s.")
        calm_since, last_said = None, reason
        while True:
            # loading the model needs room for it; if something else already loaded it, only the usual margin is needed
            need = self.pause_mb + 800 if self.model_loaded() else self.resume_mb
            why = self.busy_reason(need_mb=need)
            if why:
                calm_since = None
                if why.split(" (")[0] != last_said.split(" (")[0]:     # not again for every new GPU percentage
                    self.say(f"still waiting: {why}")
                    last_said = why
            else:
                calm_since = calm_since or time.time()
                if time.time() - calm_since >= settle:
                    break
            sleep(5)
        self._resumed_at = time.time()
        self.say(f"resuming after {(time.time() - start) / 60:.0f} min")
        return time.time() - start


class GovernedLLM:
    """Wraps a client so every reply can be paused and repeated. Same ``chat`` interface as LLMClient."""

    def __init__(self, llm, governor, on_pause=None):
        self.llm, self.governor, self.on_pause = llm, governor, on_pause
        self.waited = 0.0                      # seconds spent paused; the agent adds them to its time limit
        local = llm.local.client if hasattr(llm, "local") else llm
        local.on_chunk = governor.check        # checked on every streamed chunk, also while a tool call is being written

    def __getattr__(self, name):
        return getattr(self.llm, name)

    def chat(self, messages, tools=None, on_token=None, deadline=None):
        while True:
            reason = self.governor.busy_reason()
            if reason is None:
                try:
                    return self.llm.chat(messages, tools=tools, on_token=on_token,
                                         **({"deadline": deadline} if deadline else {}))
                except Paused as p:
                    reason = p.reason
            if self.on_pause:
                self.on_pause(reason)
            waited = self.governor.wait_for_room(reason)
            self.waited += waited
            if deadline:
                deadline += waited             # time spent waiting for your other apps does not count against the task
