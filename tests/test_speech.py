"""Speech: text cleaning, sentence splitting, the speaker queue, and the /speak and /voice commands (no audio played)."""

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402

from assistant import speech, tui  # noqa: E402
from assistant.speech import Speaker, clean_for_speech, normalise_rate, split_sentences  # noqa: E402


def test_cleaning_drops_code_links_and_markdown():
    text = "## Result\n**Done!** See [the docs](http://x.com/a) or http://y.com.\n- one\n- two\n```python\nprint(1)\n```\nThanks"
    out = clean_for_speech(text)
    assert "print" not in out and "http" not in out and "**" not in out and "##" not in out
    assert "the docs" in out and "a link" in out and "one" in out
    assert out.endswith("The code is on your screen.")


def test_plain_text_is_unchanged_apart_from_whitespace():
    assert clean_for_speech("Hello there.  How are you?") == "Hello there. How are you?"


def test_sentences_are_short_and_non_empty():
    parts = split_sentences("Hello there. This is two! And three?")
    assert parts == ["Hello there.", "This is two!", "And three?"]
    long = ("word " * 120).strip() + "."
    assert all(len(p) <= 270 for p in split_sentences(long))
    assert split_sentences("... !!! ???") == []


def test_rate_parsing():
    assert normalise_rate("faster") == "+15%" and normalise_rate("slower") == "-15%" and normalise_rate("normal") == "+0%"
    assert normalise_rate("+20%") == "+20%" and normalise_rate("-10") == "-10%" and normalise_rate("fast-ish") is None
    assert speech.rate_to_sapi("+20%") == 2 and speech.rate_to_sapi("-100%") == -10


class FakeEngine:
    name = "fake"

    def __init__(self):
        self.spoken = []

    def voices(self):
        return [("Ada (test)", "v-ada"), ("Bob (test)", "v-bob")]

    def synth(self, text):
        return text

    def play(self, item):
        self.spoken.append(item)

    def stop(self):
        pass


def speaker_with_fake():
    sp = Speaker("windows")
    sp.engine = FakeEngine()

    def configure(engine=None, voice=None, rate=None):   # never build a real engine in tests
        sp.engine_name = engine or sp.engine_name
        sp.voice = sp.voice if voice is None else voice
        sp.rate = sp.rate if rate is None else rate

    sp.configure = configure
    return sp


def wait_for(cond, seconds=3):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_speaker_speaks_sentences_in_order_when_enabled():
    sp = speaker_with_fake()
    sp.enabled = True
    sp.say("First sentence. Second one!")
    assert wait_for(lambda: len(sp.engine.spoken) == 2)
    assert sp.engine.spoken == ["First sentence.", "Second one!"]


def test_speaker_is_silent_when_off_and_stop_drops_queue():
    sp = speaker_with_fake()
    sp.say("Nothing should be said.")
    time.sleep(0.2)
    assert sp.engine.spoken == []
    sp.enabled = True
    sp.stop()
    sp.say("After a stop it speaks again.")
    assert wait_for(lambda: sp.engine.spoken == ["After a stop it speaks again."])


def make_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "CONFIG_PATH", tmp_path / "config.json")
    args = tui.build_parser().parse_args(["--workspace", str(tmp_path), "--no-verify"])
    console = Console(file=io.StringIO(), force_terminal=False, width=100, highlight=False)
    ui = tui.Jarvis(args, console)
    ui.speaker = speaker_with_fake()
    return ui, console


def test_speak_command_toggles_and_saves(tmp_path, monkeypatch):
    ui, console = make_ui(tmp_path, monkeypatch)
    ui.command("/speak")
    assert ui.speaker.enabled and tui.load_config()["speak"] is True
    ui.command("/speak off")
    assert not ui.speaker.enabled and tui.load_config()["speak"] is False


def test_voice_list_pick_and_rate(tmp_path, monkeypatch):
    ui, console = make_ui(tmp_path, monkeypatch)
    ui.speaker.engine_name = "fake"
    ui.command("/voice")
    out = console.file.getvalue()
    assert "Ada (test)" in out and "Bob (test)" in out
    ui.command("/voice 2")
    assert ui.speaker.voice == "v-bob" and tui.load_config()["voice"] == "v-bob"
    ui.command("/voice rate faster")
    assert tui.load_config()["voice_rate"] == "+15%"
    ui.command("/voice nobody")
    assert "no voice called" in console.file.getvalue()
