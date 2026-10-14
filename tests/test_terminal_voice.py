"""The terminal chat with live voice: a 'voice' thread speaks into the real input line (prompt_toolkit) while the chat
loop runs. No microphone, no server: the model is scripted."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from assistant import commands, voice  # noqa: E402
from test_tui import FakeLLM, make_ui, say  # noqa: E402


def run_chat(ui, bridge, speak_script, timeout=20):
    """Run the chat loop; ``speak_script(bridge)`` runs on another thread, like the voice thread would."""
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        session = PromptSession(input=pipe, output=DummyOutput())
        threading.Thread(target=speak_script, args=(bridge,), daemon=True).start()
        guard = threading.Timer(timeout, lambda: pipe.send_text("/exit\r"))
        guard.start()
        try:
            ui.repl(session=session, bridge=bridge)
        finally:
            guard.cancel()


def test_what_you_say_reaches_the_chat_and_modes_work_by_voice():
    ui, console, _ = make_ui(FakeLLM(say("The sea is wide and blue.")))
    spoken = []
    ui.speaker.say = lambda text: spoken.append((text, ui.speaker.enabled))
    ui.mode = "ask"
    bridge = voice.TerminalBridge(ui)
    heard = voice.Heard("x", True)

    def talk(b):
        time.sleep(0.6)
        b.handler("auto mode", heard)                  # a mode phrase while the input line is waiting
        time.sleep(0.6)
        b.handler("write a poem about the sea", heard)   # a request
        time.sleep(1.5)
        b.handler("reset", heard)
        time.sleep(0.6)
        b.handler("goodbye jarvis", heard)

    run_chat(ui, bridge, talk)
    text = console.file.getvalue()
    assert ui.mode == "auto"                                         # "auto mode" switched it
    assert "you (voice)" in text and "write a poem about the sea" in text
    assert "The sea is wide and blue" in text
    assert any("sea is wide" in t and enabled for t, enabled in spoken)   # answers to spoken requests are read aloud...
    assert any(t.startswith("Auto mode") for t, _ in spoken)              # ...and mode changes are confirmed aloud
    assert "conversation cleared" in text and "bye." in text
    assert ui.speaker.enabled is False                               # speech was only on for that answer


def test_quick_commands_run_while_the_model_is_busy(monkeypatch):
    ran = []
    monkeypatch.setattr(commands, "run", lambda c, toolbox=None: ran.append(c["action"]) or "Volume 30 percent.")

    class SlowLLM(FakeLLM):
        def chat(self, messages, tools=None, on_token=None, deadline=None):
            time.sleep(2.0)
            return super().chat(messages, tools, on_token, deadline)

    ui, console, _ = make_ui(SlowLLM(say("done thinking")))
    ui.speaker.say = lambda text: None
    bridge = voice.TerminalBridge(ui)
    heard = voice.Heard("x", True)
    during = {}

    def talk(b):
        time.sleep(0.6)
        b.handler("tell me a joke", heard)             # starts a slow model task
        time.sleep(0.8)
        b.handler("volume down", heard)                # must not wait for it
        during["ran_at_once"] = list(ran)
        time.sleep(2.5)
        b.handler("exit", heard)

    run_chat(ui, bridge, talk)
    assert during["ran_at_once"] == ["volume_down"]
    assert "done thinking" in console.file.getvalue()
