"""The whole listening loop with the real wake-word and speech models, fed Windows' own synthetic speech in real time.

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
        import openwakeword
        from faster_whisper.utils import download_model
        res = Path(openwakeword.__file__).parent / "resources" / "models"
        download_model("small.en", local_files_only=True)
        return any(res.glob("hey_jarvis*.onnx"))
    except Exception:  # noqa: BLE001
        return False


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


def hear(tmp_path, monkeypatch, gain, spoken="play next song", gap=0.5, noise_s=6):
    rng = np.random.default_rng(1)
    noise = lambda s: (rng.standard_normal(int(s * 16000)) * 8).astype(np.int16)
    wake, cmd = synth("Hey Jarvis", tmp_path / "w.wav"), synth(spoken, tmp_path / "c.wav")
    stream = np.concatenate([noise(1.5), (wake * gain).astype(np.int16), noise(gap), (cmd * gain).astype(np.int16), noise(noise_s)])
    ran = []
    monkeypatch.setattr(commands, "run", lambda c, toolbox=None: (ran.append(c), "ok")[1])
    ui = FakeUI()
    v = voice.Voice(ui, beeps=False, log=lambda m: None, source=FakeMic(stream))
    v.duck.down = v.duck.up = lambda: None
    t = threading.Thread(target=v.run, daemon=True)
    t.start()
    t.join(len(stream) / 16000 + 6)
    v.stop.set()
    return ran, ui.speaker.said


@pytest.mark.skipif(not models_present(), reason="wake-word / speech models not downloaded")
@pytest.mark.parametrize("gain", [1.0, 0.03])        # a normal microphone, and one about 30 times quieter
def test_hey_jarvis_then_a_command_runs_it(tmp_path, monkeypatch, gain):
    ran, said = hear(tmp_path, monkeypatch, gain)
    assert ran == [{"action": "next"}] and said == ["ok"]


@pytest.mark.skipif(not models_present(), reason="wake-word / speech models not downloaded")
def test_speech_without_the_wake_word_does_nothing(tmp_path, monkeypatch):
    rng = np.random.default_rng(1)
    stream = np.concatenate([(rng.standard_normal(24000) * 8).astype(np.int16), synth("play next song", tmp_path / "c.wav"),
                             (rng.standard_normal(16000 * 4) * 8).astype(np.int16)])
    ran = []
    monkeypatch.setattr(commands, "run", lambda c, toolbox=None: ran.append(c))
    v = voice.Voice(FakeUI(), beeps=False, log=lambda m: None, source=FakeMic(stream))
    t = threading.Thread(target=v.run, daemon=True)
    t.start()
    t.join(len(stream) / 16000 + 2)
    v.stop.set()
    assert ran == []
