"""Jarvis steps aside when other programs need the memory, and background tasks survive pauses, crashes and restarts."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import tasks  # noqa: E402
from assistant.agent import Agent  # noqa: E402
from assistant.governor import GovernedLLM, Governor  # noqa: E402
from assistant.tools import Toolbox  # noqa: E402

STATS = {"tokens": 1, "tokens_per_sec": 1.0, "time_to_first_token": 0.0}
FREE, GAME = (8000, set(), False), (8000, {"game.exe"}, False)


def probe(*states):
    """Readings in order; the last one repeats."""
    left = list(states)
    return lambda: left.pop(0) if len(left) > 1 else left[0]


def governor(*states, **kw):
    return Governor("http://x/v1", "forge", heavy=["game"], settle=0, check_every=0, probe=probe(*states),
                    sleep=lambda s: None, **kw)


def test_reasons_to_pause():
    g = governor(FREE)
    assert g.busy_reason() is None
    g.probe = probe((900, set(), False))
    assert "0.9 GB" in g.busy_reason()
    g.probe = probe((8000, {"game.exe", "chrome.exe"}, False))
    assert g.busy_reason() == "game.exe is running"
    g.probe = probe((8000, set(), True))
    assert g.busy_reason() == "a full-screen app is open"
    g.probe = probe((3000, set(), False))                 # enough to keep going, not enough to load the model again
    assert g.busy_reason() is None and g.busy_reason(need_mb=4800)


class Streamy:
    """A client that streams two pieces and checks the governor between them, like LLMClient does."""
    model = "forge"

    def __init__(self):
        self.on_chunk, self.calls = None, 0

    def chat(self, messages, tools=None, on_token=None, deadline=None):
        self.calls += 1
        for piece in ("Hel", "lo"):
            self.on_chunk()
            if on_token:
                on_token(piece)
        return {"role": "assistant", "content": "Hello"}, dict(STATS)


def test_a_game_starting_mid_reply_pauses_then_the_reply_is_written_again():
    # free when the reply starts, the game starts during it, it is still running once, then it closes
    g = governor(FREE, FREE, GAME, GAME, FREE)
    inner, paused = Streamy(), []
    llm = GovernedLLM(inner, g, on_pause=paused.append)
    msg, _ = llm.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "Hello" and inner.calls == 2
    assert paused == ["game.exe is running"] and g.pauses == 1


def test_nothing_starts_while_the_machine_is_busy():
    g = governor(GAME, GAME, GAME, FREE)
    inner = Streamy()
    GovernedLLM(inner, g).chat([{"role": "user", "content": "hi"}])
    assert inner.calls == 1 and g.pauses == 1


class Script:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, tools=None, on_token=None, deadline=None):
        self.seen.append([dict(m) for m in messages])
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r, dict(STATS)


def say(text):
    return {"role": "assistant", "content": text}


def call(name, cid="c1", **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_resume_carries_on_after_the_last_finished_step():
    saved = []
    a = Agent(Script(call("list_dir", path="."), RuntimeError("process killed")), Toolbox(tempfile.mkdtemp()), confirm=None)
    a.journal = lambda m: saved.append([dict(x) for x in m])
    with pytest.raises(RuntimeError):
        a.run("list the folder")
    last = saved[-1]
    assert [m["role"] for m in last] == ["system", "user", "assistant", "tool"]
    llm = Script(say("The folder is empty."))
    out, _ = Agent(llm, Toolbox(tempfile.mkdtemp()), confirm=None).resume("list the folder", last)
    assert out == "The folder is empty." and llm.seen[0][-1]["role"] == "tool"     # not asked to start over


def test_a_tool_call_without_its_result_is_asked_for_again_not_run_blind():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}, call("list_dir", path=".")]
    llm = Script(say("ok"))
    Agent(llm, Toolbox(tempfile.mkdtemp()), confirm=None).resume("go", msgs)
    assert llm.seen[0][-1] == {"role": "user", "content": "go"}


def make_agents(*scripts):
    left = list(scripts)
    return lambda: Agent(left.pop(0), Toolbox(tempfile.mkdtemp()), confirm=None, verify_code=False)


def test_a_task_survives_the_worker_being_killed_and_finishes_on_the_next_start(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    t = tasks.add("list the folder")
    first = Script(call("list_dir", path="."), KeyboardInterrupt())          # the worker is stopped after one step
    second = Script(say("Nothing in it."))
    with pytest.raises(KeyboardInterrupt):
        tasks.work(make_agents(first), None, lambda m: None, sleep=lambda s: None)
    on_disk = tasks.load(t["id"])
    assert on_disk["status"] == "running" and on_disk["messages"][-1]["role"] == "tool"
    assert tasks.worker_pid() is None                                        # the lock was released
    done = tasks.work(make_agents(second), None, lambda m: None, results_dir=tmp_path / "results", once=True,
                      sleep=lambda s: None)
    assert done[0]["status"] == "done" and done[0]["answer"] == "Nothing in it."
    assert second.seen[0][-1]["role"] == "tool"                              # resumed, not restarted
    assert "Nothing in it." in Path(done[0]["result_file"]).read_text(encoding="utf-8")


def test_server_errors_are_retried_from_the_saved_step(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    tasks.add("list the folder")
    waits = []
    done = tasks.work(make_agents(Script(call("list_dir", path="."), ConnectionError("ollama restarted")), Script(say("Empty."))),
                      None, lambda m: None, once=True, sleep=waits.append)
    assert done[0]["status"] == "done" and waits == [30]


def test_cancelled_tasks_stop_at_the_next_step(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    t = tasks.add("loop forever")

    class Canceller(Script):
        def chat(self, messages, **kw):
            tasks.cancel(t["id"])
            return call("list_dir", path="."), dict(STATS)

    done = tasks.work(make_agents(Canceller()), None, lambda m: None, once=True, sleep=lambda s: None)
    assert done[0]["status"] == "cancelled" and tasks.next_task() is None


def test_only_one_worker_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    assert tasks.pid_alive(os.getpid()) and not tasks.pid_alive(0)
    assert tasks.claim_worker() and tasks.worker_pid() == os.getpid()
    (tmp_path / "tasks" / "worker.lock").write_text(str(os.getppid()))      # some other live process holds it
    said = []
    assert tasks.work(make_agents(), None, said.append, once=True) == [] and "already running" in said[0]
