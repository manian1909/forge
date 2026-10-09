"""The jarvis UI loop, driven with simulated keystrokes and a scripted fake model (no server, no GPU).

    python tests/test_tui.py
"""

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402
from rich.console import Console  # noqa: E402

from assistant.tools import Toolbox  # noqa: E402
from assistant.tui import Jarvis, build_parser  # noqa: E402

STATS = {"tokens": 5, "tokens_per_sec": 5.0, "time_to_first_token": 0.0}


class FakeLLM:
    model = "forge"

    def __init__(self, *replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None, on_token=None, deadline=None):
        msg = self.replies.pop(0)
        if on_token and msg.get("content"):
            for word in msg["content"].split(" "):
                on_token(word + " ")
        return msg, dict(STATS)


def say(text):
    return {"role": "assistant", "content": text}


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def make_ui(llm, *argv):
    ws = tempfile.mkdtemp()
    (Path(ws) / "hello.txt").write_text("hi")
    args = build_parser().parse_args(["--workspace", ws, "--no-verify", *argv])
    console = Console(file=io.StringIO(), force_terminal=True, width=100, highlight=False)
    ui = Jarvis(args, console)
    ui.llm = llm
    ui.agent.llm = llm
    ui.agent.toolbox = Toolbox(ws)
    return ui, console, ws


def drive(ui, keys):
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        session = PromptSession(input=pipe, output=DummyOutput())
        ui.repl(session=session)


def text_of(console):
    return console.file.getvalue()


def test_task_runs_a_tool_and_shows_card_result_and_answer():
    ui, console, _ = make_ui(FakeLLM(call("list_dir"), say("There is one file: hello.txt")))
    drive(ui, "what files are here?\r/exit\r")
    out = text_of(console)
    assert "list_dir" in out and "hello.txt" in out and "bye." in out


def test_risky_tool_asks_and_declining_stops_it():
    ui, console, ws = make_ui(FakeLLM(call("write_file", path="x.txt", content="data"), say("ok, not written")))
    ui.console.input = lambda *a, **k: "n"        # the user answers "no" at the permission prompt
    drive(ui, "write x.txt\r/exit\r")
    assert not (Path(ws) / "x.txt").exists()


def test_always_allow_runs_the_tool_and_remembers_the_choice():
    ui, console, ws = make_ui(FakeLLM(call("write_file", path="x.txt", content="data"), say("written")))
    ui.console.input = lambda *a, **k: "a"
    drive(ui, "write x.txt\r/exit\r")
    assert (Path(ws) / "x.txt").read_text() == "data" and "write_file" in ui.always


def test_auto_mode_never_asks():
    ui, console, ws = make_ui(FakeLLM(call("write_file", path="y.txt", content="z"), say("done")), "--yes")
    ui.console.input = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not prompt"))
    drive(ui, "write y.txt\r/exit\r")
    assert (Path(ws) / "y.txt").exists()


def test_slash_commands_mode_tools_reset_and_unknown():
    ui, console, _ = make_ui(FakeLLM())
    drive(ui, "/mode auto\r/tools\r/reset\r/nope\r/exit\r")
    out = text_of(console)
    assert ui.mode == "auto" and "run_python" in out and "unknown command" in out


def test_ctrl_c_style_failure_drops_the_half_finished_turn():
    class Boom(FakeLLM):
        def chat(self, *a, **k):
            raise KeyboardInterrupt()

    ui, console, _ = make_ui(Boom())
    drive(ui, "do a thing\r/exit\r")
    assert [m["role"] for m in ui.agent.messages] == ["system"] and "stopped" in text_of(console)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
