"""Speak the assistant's replies aloud (one way: it talks, it does not listen).

Two engines:
  edge     natural neural voices (Microsoft Edge's online voices through the `edge-tts` package). The text of each
           reply is sent to Microsoft's service to be turned into audio. pip install edge-tts
  windows  the voices built into Windows (System.Speech). Fully offline, more robotic.

Replies are cleaned for the ear (code blocks are skipped, links and markdown symbols dropped), cut into sentences,
and played while the next sentence is being prepared, so speech starts quickly.
"""

import asyncio
import ctypes
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# a short list of good English voices; `edge-tts --list-voices` has many more (any ShortName works with /voice <name>)
EDGE_VOICES = [
    ("Aria (US, woman)", "en-US-AriaNeural"), ("Jenny (US, woman)", "en-US-JennyNeural"),
    ("Ava (US, woman)", "en-US-AvaMultilingualNeural"), ("Emma (US, woman)", "en-US-EmmaMultilingualNeural"),
    ("Guy (US, man)", "en-US-GuyNeural"), ("Andrew (US, man)", "en-US-AndrewMultilingualNeural"),
    ("Davis (US, man)", "en-US-DavisNeural"), ("Sonia (UK, woman)", "en-GB-SoniaNeural"),
    ("Ryan (UK, man)", "en-GB-RyanNeural"), ("Neerja (India, woman)", "en-IN-NeerjaNeural"),
    ("Prabhat (India, man)", "en-IN-PrabhatNeural"), ("Natasha (Australia, woman)", "en-AU-NatashaNeural"),
]
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"

CODE_BLOCK = re.compile(r"```.*?```", re.S)
URL = re.compile(r"https?://\S+")


def clean_for_speech(text):
    """Markdown reply -> plain sentences a person would say."""
    had_code = bool(CODE_BLOCK.search(text))
    t = CODE_BLOCK.sub(" ", text)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)             # [label](url) -> label
    t = URL.sub("a link", t)
    t = re.sub(r"^\s*\|.*\|\s*$", " ", t, flags=re.M)              # table rows
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)            # headings
    t = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", t, flags=re.M)       # bullets
    t = re.sub(r"[*_`~>#]+", "", t)                                # emphasis, inline code, quotes
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]", "", t)  # emoji
    t = re.sub(r"\s*\n\s*", ". ", t)
    t = re.sub(r"([.!?:;])\s*\.", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if had_code:
        t = (t + " The code is on your screen.").strip()
    return t


def split_sentences(text, max_len=260):
    """Cut at sentence ends; very long sentences are cut again at commas so each chunk is short."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out = []
    for p in parts:
        while len(p) > max_len:
            cut = p.rfind(",", 0, max_len)
            cut = cut if cut > 60 else p.rfind(" ", 0, max_len)
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return [s for s in out if re.search(r"[A-Za-z0-9]", s)]


# --------------------------------------------------------------------------- playback
class Mp3Player:
    """Plays an mp3 through Windows' built-in MCI (no extra packages); can be stopped from another thread."""

    def __init__(self):
        self._mci = ctypes.windll.winmm.mciSendStringW if os.name == "nt" else None
        self.stop_flag = threading.Event()

    def _cmd(self, command):
        buf = ctypes.create_unicode_buffer(128)
        err = self._mci(command, buf, 127, 0)
        return err, buf.value

    def play(self, path):
        if self._mci is None:
            return
        alias = "forge_tts"
        self._cmd(f"close {alias}")
        err, _ = self._cmd(f'open "{path}" type mpegvideo alias {alias}')
        if err:
            return
        try:
            self._cmd(f"play {alias}")
            while not self.stop_flag.is_set():
                _, mode = self._cmd(f"status {alias} mode")
                if mode != "playing":
                    break
                time.sleep(0.05)
            self._cmd(f"stop {alias}")
        finally:
            self._cmd(f"close {alias}")


class EdgeEngine:
    name = "edge"

    def __init__(self, voice=DEFAULT_EDGE_VOICE, rate="+0%"):
        self.voice, self.rate = voice, rate
        self.player = Mp3Player()
        self.dir = Path(tempfile.mkdtemp(prefix="forge-tts-"))
        self.counter = 0

    @staticmethod
    def available():
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def voices(self):
        return list(EDGE_VOICES)

    def synth(self, text):
        import edge_tts

        self.counter += 1
        path = self.dir / f"{self.counter}.mp3"
        asyncio.run(edge_tts.Communicate(text, self.voice, rate=self.rate).save(str(path)))
        return path

    def play(self, path):
        self.player.play(path)
        try:
            Path(path).unlink()
        except OSError:
            pass

    def stop(self):
        self.player.stop_flag.set()


SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$v = $env:FORGE_VOICE; if ($v) { try { $s.SelectVoice($v) } catch {} }; "
    "$s.Rate = [int]$env:FORGE_RATE; $s.Speak([Console]::In.ReadToEnd())"
)


class WindowsEngine:
    name = "windows"

    def __init__(self, voice="", rate="+0%"):
        self.voice, self.rate = voice, rate
        self.proc = None

    @staticmethod
    def available():
        return os.name == "nt"

    def voices(self):
        script = ("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                  ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [(n.strip(), n.strip()) for n in out.splitlines() if n.strip()]

    def synth(self, text):
        return text                      # nothing to prepare: speaking happens in play()

    def play(self, text):
        env = {**os.environ, "FORGE_VOICE": self.voice, "FORGE_RATE": str(rate_to_sapi(self.rate))}
        self.proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", SAPI_SCRIPT], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            self.proc.communicate(text.encode("utf-8"), timeout=120)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def rate_to_sapi(rate):
    """'+20%' -> SAPI rate -10..10 (each step is about 10 percent)."""
    m = re.fullmatch(r"([+-]?\d+)%", rate.strip())
    return max(-10, min(10, round(int(m.group(1)) / 10))) if m else 0


def normalise_rate(text):
    """Accepts '+15%', '-10', 'faster', 'slower', 'normal'."""
    t = text.strip().lower()
    named = {"normal": "+0%", "faster": "+15%", "fast": "+15%", "slower": "-15%", "slow": "-15%"}
    if t in named:
        return named[t]
    m = re.fullmatch(r"([+-]?\d{1,3})%?", t)
    if m:
        n = max(-60, min(80, int(m.group(1))))
        return f"{n:+d}%"
    return None


# ----------------------------------------------------------------------------- speaker
class Speaker:
    def __init__(self, engine="edge", voice="", rate="+0%"):
        self.enabled = False
        self.rate = rate
        self.engine_name = engine
        self.voice = voice
        self.engine = None
        self._jobs = queue.Queue()        # sentences waiting to be synthesised
        self._ready = queue.Queue(maxsize=2)   # synthesised, waiting to be played
        self._epoch = 0
        self._lock = threading.Lock()
        self._threads = []
        self.configure(engine, voice, rate)

    # ----- engine selection
    def configure(self, engine=None, voice=None, rate=None):
        self.stop()
        engine = engine or self.engine_name
        if engine == "edge" and not EdgeEngine.available():
            engine = "windows"
        if engine == "windows" and not WindowsEngine.available():
            self.engine = None
            self.engine_name = engine
            return
        self.engine_name = engine
        self.rate = rate if rate is not None else self.rate
        if voice is not None:
            self.voice = voice
        if engine == "edge":
            self.engine = EdgeEngine(self.voice or DEFAULT_EDGE_VOICE, self.rate)
            self.voice = self.engine.voice
        else:
            self.engine = WindowsEngine(self.voice, self.rate)

    def voices(self):
        return self.engine.voices() if self.engine else []

    def current_label(self):
        return f"{self.engine_name}: {self.voice or 'default'} ({self.rate})"

    # ----- speaking
    def _start_threads(self):
        if self._threads:
            return
        for target in (self._synth_loop, self._play_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def _synth_loop(self):
        while True:
            epoch, text = self._jobs.get()
            if epoch != self._epoch or self.engine is None:
                continue
            try:
                item = self.engine.synth(text)
            except Exception:  # noqa: BLE001 - offline, service hiccup: skip this sentence
                continue
            self._ready.put((epoch, item))

    def _play_loop(self):
        while True:
            epoch, item = self._ready.get()
            if epoch != self._epoch or self.engine is None:
                continue
            try:
                if hasattr(self.engine, "player"):
                    self.engine.player.stop_flag.clear()
                self.engine.play(item)
            except Exception:  # noqa: BLE001
                continue

    def say(self, markdown_text):
        """Queue a reply to be spoken; returns immediately."""
        if not self.enabled or self.engine is None:
            return
        sentences = split_sentences(clean_for_speech(markdown_text))
        if not sentences:
            return
        self._start_threads()
        with self._lock:
            epoch = self._epoch
        for s in sentences:
            self._jobs.put((epoch, s))

    def stop(self):
        """Silence now and drop everything queued."""
        with self._lock:
            self._epoch += 1
        for q in (self._jobs, self._ready):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        if self.engine is not None:
            self.engine.stop()

    def busy(self):
        return not (self._jobs.empty() and self._ready.empty())
