"""Mode phrases become the slash commands you could type; the terminal bridge routes what you say."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import commands, voice, voicemodes  # noqa: E402


@pytest.mark.parametrize("text,command", [
    ("auto mode", "/mode auto"), ("switch to auto mode", "/mode auto"), ("go autonomous", "/mode auto"),
    ("ask mode", "/mode ask"), ("switch to ask mode", "/mode ask"), ("sandbox mode", "/mode jail"),
    ("what mode am i in", "/mode"), ("reset", "/reset"), ("start over", "/reset"), ("new conversation", "/reset"),
    ("clear the screen", "/clear"), ("compact", "/compact"), ("undo that", "/undo"), ("try again", "/retry"),
    ("show usage", "/usage"), ("list tools", "/tools"), ("help", "/help"), ("speak on", "/speak on"),
    ("start speaking", "/speak on"), ("stop speaking", "/nospeak"), ("voice off", "/nospeak"), ("list jobs", "/jobs"),
    ("standby on", "/standby on"), ("standby off", "/standby off"), ("use the uncensored model", "/model forge-open"),
    ("standard model", "/model forge"), ("learning off", "/learn off"), ("mark that as good", "/good"),
    ("exit", "/exit"), ("goodbye jarvis", "/exit"), ("quit jarvis", "/exit"),
    ("run this in the background write a report on tigers", "/bg write a report on tigers"),
    ("write a report in the background", "/bg write a report"),
])
def test_mode_phrases(text, command):
    got = voicemodes.parse(text)
    assert got is not None and got[0] == command


@pytest.mark.parametrize("text", ["play next song", "what is the capital of france", "open the documents folder", "volume up",
                                  "what is playing in the background", "music in the background", "write me a poem", "mute"])
def test_other_requests_are_not_modes(text):
    assert voicemodes.parse(text) is None


def test_stop_words():
    assert all(voicemodes.is_stop(t) for t in ("stop", "cancel that", "never mind", "abort"))
    assert not voicemodes.is_stop("stop the music please now")


class Ui:
    task_running = False


def test_bridge_routes_what_you_say(monkeypatch):
    ran = []
    monkeypatch.setattr(commands, "run", lambda c, toolbox=None: ran.append(c) or "Volume 40 percent.")
    bridge = voice.TerminalBridge(Ui())
    heard = voice.Heard("x", True)
    assert bridge.handler("auto mode", heard).note == "/mode auto"
    assert bridge.take()[:2] == ("cmd", "/mode auto")
    reply = bridge.handler("volume 40 percent", heard)
    assert reply.chime and ran == [{"action": "volume_set", "value": 40}] and bridge.take() is None   # done at once, not queued
    reply = bridge.handler("write a poem about the sea", heard)
    assert reply.llm and bridge.take()[:2] == ("task", "write a poem about the sea")


def test_voice_stop_interrupts_a_running_task_but_otherwise_pauses_the_music(monkeypatch):
    interrupted = []
    monkeypatch.setattr(voice._thread, "interrupt_main", lambda: interrupted.append(1))
    monkeypatch.setattr(commands, "run", lambda c, toolbox=None: "Paused.")
    ui = Ui()
    bridge = voice.TerminalBridge(ui)
    ui.task_running = True
    bridge.handler("stop", voice.Heard("stop", True))
    assert interrupted == [1]
    ui.task_running = False
    reply = bridge.handler("stop", voice.Heard("stop", True))
    assert interrupted == [1] and reply.chime                 # no task: it is the pause command


def test_poke_is_harmless_when_nothing_is_waiting_for_input():
    bridge = voice.TerminalBridge(Ui())
    bridge.poke()
    bridge.session = object()
    bridge.poke()
    assert threading.active_count() >= 1
