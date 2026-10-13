"""Background tasks that always finish: a queue on disk and a standby worker that works through it.

Each task is one JSON file in ``~/.forge/tasks``. Before every model call the whole conversation is written to that
file, so wherever the worker stops (paused for a game, the laptop shut down, Ollama crashed, the process was killed)
the next start carries on from the last completed step instead of from the beginning.

The worker (``jarvis --worker``, or started by itself by ``/bg``) waits on standby: it picks up the next task, lets
the governor pause it whenever another program needs the memory, retries after server errors, and writes the answer
to ``<jarvis folder>/results``. Only one worker runs at a time (a lock file holds its process id).
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .session import forge_home

REPO = Path(__file__).resolve().parent.parent
MAX_ERRORS = 8           # server/connection failures in a row before a task is given up as failed
MAX_ROUNDS = 4           # "too many tool steps" stops that are continued before giving up
OPEN = ("queued", "running")


def folder():
    d = forge_home() / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(task_id):
    return folder() / f"{task_id}.json"


def save(task):
    """Atomic: a crash while saving leaves the previous version, never half a file."""
    task["updated"] = time.time()
    task["log"] = task.get("log", [])[-200:]
    tmp = _path(task["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _path(task["id"]))
    return task


def load(task_id):
    try:
        return json.loads(_path(task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def all_tasks():
    rows = []
    for p in folder().glob("*.json"):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda t: t["created"])


def add(prompt):
    n = len(list(folder().glob("*.json"))) + 1
    task = {"id": time.strftime("%Y%m%d-%H%M%S") + f"-{n}", "prompt": prompt, "status": "queued", "created": time.time(),
            "messages": [], "answer": "", "errors": 0, "rounds": 0, "pauses": 0, "paused_s": 0.0, "log": []}
    return save(task)


def find(prefix):
    """A task by id, id suffix, or its number in the list (1 = oldest shown)."""
    rows = all_tasks()
    if prefix.isdigit() and 1 <= int(prefix) <= len(rows):
        return rows[int(prefix) - 1]
    return next((t for t in rows if t["id"] == prefix or t["id"].endswith(prefix)), None)


def next_task():
    """An interrupted task first (it was running when the worker stopped), then the oldest queued one."""
    rows = all_tasks()
    return next((t for t in rows if t["status"] == "running"), None) or next((t for t in rows if t["status"] == "queued"), None)


def cancel(task_id):
    task = load(task_id)
    if task and task["status"] in OPEN:
        task["status"] = "cancelled"
        save(task)
        return True
    return False


# ---------------------------------------------------------------- one worker at a time
def pid_alive(pid):
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259                    # STILL_ACTIVE


def _lock():
    return folder() / "worker.lock"


def worker_pid():
    try:
        pid = int(_lock().read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def claim_worker():
    other = worker_pid()
    if other and other not in (os.getpid(), os.getppid()):     # start_worker wrote our pid (or our launcher's) already
        return False
    _lock().write_text(str(os.getpid()))
    return True


def release_worker():
    if worker_pid() == os.getpid():
        try:
            _lock().unlink()
        except OSError:
            pass


def log_path():
    return folder() / "worker.log"


def start_worker(extra_args=()):
    """Start the standby worker in the background (no window) unless one is already running. Returns its pid."""
    pid = worker_pid()
    if pid:
        return pid
    cmd = [_python(), str(REPO / "jarvis.py"), "--worker", *extra_args]
    flags = 0x00000008 | 0x08000000 | 0x00000200 if sys.platform == "win32" else 0   # detached, no window, own group
    out = open(folder() / "worker.out", "a", encoding="utf-8")       # a crash before logging starts still leaves a trace
    proc = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)
    _lock().write_text(str(proc.pid))       # held for the new worker right away, so a second /bg does not start another
    return proc.pid


def _python():
    """python.exe, not pythonw.exe: pythonw has no stdout at all, which breaks console code; the window is hidden anyway."""
    exe = Path(sys.executable)
    return str(exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe)


def startup_file():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "jarvis-standby.vbs"


def set_autostart(on):
    """Start the worker when you log in (a script in your Startup folder; no admin rights needed)."""
    f = startup_file()
    if not on:
        f.unlink(missing_ok=True)
        return None
    # window style 0 = hidden; output goes to worker.out like start_worker's
    log = folder() / "worker.out"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f'CreateObject("WScript.Shell").Run "cmd /c """"{_python()}"" ""{REPO / "jarvis.py"}"" --worker >> ""{log}"" 2>&1""", 0, False\r\n',
                 encoding="utf-8")
    return f


# ---------------------------------------------------------------- the worker loop
def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-") or "task"


def write_result(task, results_dir):
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"{task['id']}-{_slug(task['prompt'])}.md"
    out.write_text(f"# Task\n\n{task['prompt']}\n\n# Answer ({task['status']})\n\n{task['answer']}\n", encoding="utf-8")
    return out


def run_one(task, make_agent, hints_for, say, results_dir=None, sleep=time.sleep, ensure=None):
    """Work on one task until it is done, failed or cancelled. Safe to interrupt at any point."""
    log = lambda m: (task["log"].append(f"{time.strftime('%H:%M:%S')} {m}"), say(f"[{task['id']}] {m}"))

    def journal(messages):
        latest = load(task["id"]) or {}
        if latest.get("status") == "cancelled":
            raise Cancelled()
        task["messages"] = messages
        save(task)

    while True:
        agent = make_agent()
        agent.journal = journal
        if hasattr(agent.llm, "on_pause"):           # a GovernedLLM: note pauses in the task's log
            def on_pause(reason):
                task["pauses"] += 1
                log(f"paused: {reason}")
            agent.llm.on_pause = on_pause
        waited_before = getattr(agent.llm, "waited", 0.0)
        task["status"] = "running"
        save(task)
        hooks = dict(on_tool=lambda n, a: log(f"tool {n}"), on_result=lambda n, r: log(f"  -> {r.strip().splitlines()[-1][:100] if r.strip() else '(no output)'}"),
                     on_retry=lambda why: log(f"retrying: {why}"))
        try:
            if ensure:
                ensure()
            if task["messages"]:
                log("resuming from the last saved step")
                answer, _ = agent.resume(task["prompt"], task["messages"], **hooks)
            else:
                log("starting")
                answer, _ = agent.run(task["prompt"], hints=hints_for(task["prompt"]) if hints_for else "", **hooks)
        except Cancelled:
            say(f"[{task['id']}] cancelled")
            return load(task["id"])
        except KeyboardInterrupt:
            log("worker stopped; the task will resume from here next time")
            raise
        except Exception as e:  # noqa: BLE001 - a dead server, a dropped connection: wait and carry on
            task["errors"] += 1
            task["messages"] = agent.messages
            if task["errors"] >= MAX_ERRORS:
                task["status"], task["answer"] = "failed", f"(gave up after {task['errors']} errors; last: {e})"
                log(task["answer"])
                break
            wait = min(600, 15 * 2 ** task["errors"])
            log(f"error ({type(e).__name__}: {str(e)[:120]}); retrying in {wait}s from the last saved step")
            save(task)
            sleep(wait)
            continue
        task["messages"] = agent.messages
        task["paused_s"] += getattr(agent.llm, "waited", 0.0) - waited_before
        if answer.startswith("(stopped: too many tool steps") and task["rounds"] + 1 < MAX_ROUNDS:
            task["rounds"] += 1
            log("used its step budget; continuing")
            continue
        task["errors"] = 0
        task["status"] = "failed" if answer.startswith("(stopped") else "done"
        task["answer"] = answer
        log(f"{task['status']}")
        break
    save(task)
    if results_dir is not None:
        task["result_file"] = str(write_result(task, results_dir))
        save(task)
    return task


class Cancelled(Exception):
    pass


def work(make_agent, hints_for, say, results_dir=None, once=False, idle=None, sleep=time.sleep, ensure=None):
    """The standby loop. ``idle()`` is called once when the queue runs dry (the governor unloads the model there)."""
    if not claim_worker():
        say(f"another worker is already running (pid {worker_pid()})")
        return []
    done, was_busy = [], True
    try:
        while True:
            task = next_task()
            if task is None:
                if once:
                    break
                if was_busy and idle:
                    idle()
                was_busy = False
                sleep(5)
                continue
            was_busy = True
            done.append(run_one(task, make_agent, hints_for, say, results_dir, sleep=sleep, ensure=ensure))
    finally:
        release_worker()
    return done
