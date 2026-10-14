"""Voice: the checks that stop jarvis acting on misheard words, the end-of-speech detector, and the login script.
No microphone or speech model is used."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import voice  # noqa: E402
from assistant.voice import CHUNK, Endpointer, judge, yes_no  # noqa: E402


def seg(text, lp=-0.2, nsp=0.01, cr=1.2):
    return {"text": text, "avg_logprob": lp, "no_speech_prob": nsp, "compression_ratio": cr}


def test_clear_speech_is_accepted():
    h = judge([seg(" play next song")])
    assert h.ok and h.text == "play next song" and h.confidence > 0.7


@pytest.mark.parametrize("segments,why", [
    ([], "no speech"),
    ([seg("")], "no speech"),
    ([seg("Thank you.")], "filler"),
    ([seg("thanks for watching")], "filler"),
    ([seg("you")], "filler"),
    ([seg("play something", lp=-1.4)], "not sure"),
    ([seg("blah blah blah blah", cr=3.1)], "garbled"),
    ([seg("music is playing now", lp=-1.0, nsp=0.8)], "no speech"),
    ([seg(voice.PROMPT)], "own hint"),
])
def test_doubtful_transcripts_are_rejected(segments, why):
    h = judge(segments)
    assert not h.ok and h.reason


def test_a_real_command_that_matches_a_hint_example_is_accepted():
    assert judge([seg("Play Mere Rashke Qamar on YouTube Music.")]).ok
    assert judge([seg("Open the Documents folder.")]).ok


def test_a_noisy_tail_does_not_spoil_a_good_command():
    h = judge([seg("volume up"), seg("thanks", lp=-1.5, nsp=0.9)])
    assert h.ok and h.text == "volume up"


@pytest.mark.parametrize("text,answer", [("Yes", True), ("yeah, send it", True), ("go ahead", True), ("No", False),
                                         ("no don't send it", False), ("cancel", False), ("the weather is nice", None), ("", None)])
def test_yes_no(text, answer):
    assert yes_no(text) is answer


def chunks(level, n):
    rng = np.random.default_rng(1)
    return [(rng.standard_normal(CHUNK) * level).astype(np.int16) for _ in range(n)]


def run_endpointer(ep, parts):
    for c in parts:
        verdict = ep.feed(c)
        if verdict:
            return verdict, ep.n
    return None, ep.n


def test_stops_after_the_speaker_goes_quiet():
    ep = Endpointer(floor=100)
    verdict, n = run_endpointer(ep, chunks(3000, 15) + chunks(60, 40))
    assert verdict == "done" and 15 < n <= 15 + 12        # ~0.9 s of quiet after the speech


def test_gives_up_when_nobody_speaks():
    ep = Endpointer(floor=100, start_timeout_s=2.0)
    verdict, n = run_endpointer(ep, chunks(60, 100))
    assert verdict == "nothing" and n <= 26


def test_a_single_click_is_not_speech():
    ep = Endpointer(floor=100, start_timeout_s=2.0)
    verdict, _ = run_endpointer(ep, chunks(60, 3) + chunks(5000, 1) + chunks(60, 60))
    assert verdict == "nothing"


def test_long_monologue_is_cut_off():
    ep = Endpointer(floor=100, max_s=3.0)
    assert run_endpointer(ep, chunks(3000, 100))[0] == "long"


def test_quiet_microphone_is_not_held_to_a_loud_standard():
    quiet = Endpointer(floor=8, level=90)           # you said "Hey Jarvis" at ~90 through this mic
    assert quiet.threshold < 100
    verdict, _ = run_endpointer(quiet, chunks(85, 12) + chunks(8, 40))
    assert verdict == "done"
    assert Endpointer(floor=8, level=3000).threshold == 450      # a loud mic keeps the usual ceiling


def test_loud_room_raises_the_bar():
    assert Endpointer(floor=1000).threshold >= 3000 and Endpointer(floor=10).threshold == 450


def test_autostart_script_is_written_and_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    f = voice.set_autostart(True, '--mic "USB"')
    text = f.read_text(encoding="utf-8")
    assert f.name == "jarvis-voice.vbs" and "--voice" in text and "--mic" in text and "voice.out" in text
    assert voice.set_autostart(False) is None and not f.exists()


def test_only_one_voice_assistant_at_a_time(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_HOME", str(tmp_path))
    assert voice.claim() and voice.running_pid() == voice.os.getpid()
    voice.release()
    assert voice.running_pid() is None
    voice.lock_path().write_text("4")                        # another live process: pid 4 is the Windows System process
    monkeypatch.setattr(voice.tasks, "pid_alive", lambda pid: True)
    assert voice.claim() is False
