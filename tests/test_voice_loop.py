"""The whole listening loop with the real speech models, fed Windows' own synthetic speech in real time.

It stands in for a microphone (a synthetic voice is not a person, so accents and room noise are not covered), and it
skips itself when the models or the Windows speech engine are not there."""

import queue
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import commands, voice  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="uses the Windows speech engine")


def models_present():
    try:
        from faster_whisper.utils import download_model
        for name in ("small.en", "base.en"):
            download_model(name, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001
        return False


needs_models = pytest.mark.skipif(not models_present(), reason="speech models not downloaded")


def synth(text, path):
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
          f"[System.Speech.AudioFormat.AudioChannel]::Mono); $s.SetOutputToWaveFile('{path}', $f); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class FakeMic:
    """A recording delivered at real speed (80 ms chunks), then room noise, like a microphone would."""

    def __init__(self, audio):
        self.q, self.audio = queue.Queue(), audio

    def start(self):
        threading.Thread(target=self._feed, daemon=True).start()

    def stop(self):
        pass

    def _feed(self):
        rng, pos = np.random.default_rng(0), 0
        while True:
            if pos < len(self.audio):
                c = self.audio[pos:pos + voice.CHUNK]
                c = np.pad(c, (0, voice.CHUNK - len(c)))
                pos += voice.CHUNK
            else:
                c = (rng.standard_normal(voice.CHUNK) * 8).astype(np.int16)
            self.q.put(c)
            time.sleep(voice.CHUNK / voice.RATE)

    def read(self, timeout=1.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self):
        while not self.q.empty():
            self.q.get_nowait()


class FakeSpeaker:
    enabled, engine_name = True, "windows"

    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)

    def busy(self):
        return False


class FakeUI:
    governor = object()
    args = None

    def __init__(self):
        self.speaker = FakeSpeaker()


def hear(tmp_path, monkeypatch, parts, gain=1.0, tail=7):
    """parts: ("say", text) or ("pause", seconds). Returns the commands that were run."""
    rng = np.random.default_rng(1)
    noise = lambda s: (rng.standard_normal(int(s * 16000)) * 8).astype(np.int16)
    pieces = [noise(1.5)]
    for i, (kind, value) in enumerate(parts):
        pieces.append((synth(value, tmp_path / f"p{i}.wav") * gain).astype(np.int16) if kind == "say" else noise(value))
    pieces.append(noise(tail))
    stream = np.concatenate(pieces)
    ran = []
    monkeypatch.setattr(commands, "run", lambda c, toolbox=None: (ran.append(c["action"]), "ok")[1])
    ui = FakeUI()
    v = voice.Voice(ui, beeps=False, log=lambda m: None, source=FakeMic(stream))
    v.duck.down = v.duck.up = lambda: None
    t = threading.Thread(target=v.run, daemon=True)
    t.start()
    t.join(len(stream) / 16000 + 10)
    v.stop.set()
    return ran, ui.speaker.said


@needs_models
@pytest.mark.parametrize("gain", [1.0, 0.03])        # a normal microphone, and one about 30 times quieter
def test_jarvis_and_the_command_in_one_breath(tmp_path, monkeypatch, gain):
    ran, said = hear(tmp_path, monkeypatch, [("say", "Jarvis play next song")], gain=gain)
    assert ran == ["next"] and said == []             # a chime, not a spoken reply, so nothing blocks the next command


@needs_models
def test_just_jarvis_then_the_command_after_the_beep(tmp_path, monkeypatch):
    ran, _ = hear(tmp_path, monkeypatch, [("say", "Jarvis"), ("pause", 1.2), ("say", "pause the music")])
    assert ran == ["pause"]


@needs_models
def test_a_second_command_right_after_the_first_needs_no_jarvis(tmp_path, monkeypatch):
    ran, _ = hear(tmp_path, monkeypatch, [("say", "Jarvis volume up"), ("pause", 1.0), ("say", "previous song")])
    assert ran == ["volume_up", "prev"]


@needs_models
def test_talking_without_jarvis_does_nothing(tmp_path, monkeypatch):
    ran, _ = hear(tmp_path, monkeypatch, [("say", "play next song"), ("pause", 1.5), ("say", "what is the weather like in London today")])
    assert ran == []


@needs_models
def test_the_follow_up_window_closes(tmp_path, monkeypatch):
    ran, _ = hear(tmp_path, monkeypatch, [("say", "Jarvis next song"), ("pause", 11.0), ("say", "volume down")], tail=4)
    assert ran == ["next"]
