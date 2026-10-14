"""Talk to jarvis like Alexa: say "Hey Jarvis", then a command.

    mic -> wake word ("hey jarvis", a tiny always-on model) -> record until you stop talking
        -> Whisper (speech to text, on the CPU) -> checks that the words are trustworthy
        -> simple commands (next song, volume 40%, play <song>, open <app/folder>) run at once
        -> everything else goes to the model, and its answer is spoken.

Not listening wrong words is handled in layers: the wake word gate (no command without it), a vocabulary hint for
Whisper, a confidence check (a doubtful transcript is dropped with "I didn't catch that" instead of being acted on),
a filter for the phrases Whisper invents from silence, and a spoken yes/no before anything that sends mail.
The music is turned down while jarvis listens so it does not drown your voice.

Runs hidden at login with ``jarvis --voice-autostart on``. No administrator rights. Needs the microphone allowed for
desktop apps (Windows Settings > Privacy > Microphone).
"""

import gc
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import commands, media, tasks
from .session import forge_home

RATE = 16000
CHUNK = 1280                       # 80 ms: what the wake-word model expects
PROMPT = ("Jarvis. Play next song. Previous song. Pause. Resume. Volume up. Volume down. Set volume to sixty percent. "
          "Play Mere Rashke Qamar on YouTube Music. Open Outlook. Open the Documents folder. Open Chrome.")
HALLUCINATIONS = {"thank you", "thanks", "thank you.", "thanks for watching", "thank you for watching", "you", "bye", "bye.",
                  "subtitles by the amara.org community", "please subscribe", "like and subscribe", "the end", "okay", "ok",
                  "hmm", "uh", "um", "mm-hmm", "so"}
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|send it|send|confirm|go ahead|do it|correct|affirmative|ok|okay)\b", re.I)
NO = re.compile(r"\b(no|nope|nah|cancel|stop|don'?t|do not|never ?mind|negative|abort)\b", re.I)


# ------------------------------------------------------------------------------------- trust the words
@dataclass
class Heard:
    text: str
    ok: bool
    reason: str = ""
    confidence: float = 0.0


def judge(segments, prompt=PROMPT):
    """Decide whether what Whisper wrote is trustworthy. ``segments`` are dicts with text, avg_logprob, no_speech_prob,
    compression_ratio. A doubtful result is rejected so jarvis asks you to repeat instead of doing the wrong thing."""
    kept = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -0.7:
            continue                                           # silence or noise that Whisper turned into words
        if s.get("compression_ratio", 1) > 2.4:
            return Heard("", False, "garbled (repetitive)")
        kept.append((text, s.get("avg_logprob", 0.0)))
    if not kept:
        return Heard("", False, "no speech")
    text = " ".join(t for t, _ in kept).strip()
    mean_lp = sum(lp for _, lp in kept) / len(kept)
    conf = float(np.exp(mean_lp))
    plain = re.sub(r"[^\w\s']", "", text.lower()).strip()
    if plain in HALLUCINATIONS or len(plain) < 2:
        return Heard(text, False, "only a filler phrase", conf)
    hint = re.sub(r"[^\w\s']", "", prompt.lower())
    if plain in hint and len(plain) > 0.5 * len(hint):      # Whisper reading its whole hint back out of noise (a real command is short)
        return Heard(text, False, "repeated my own hint", conf)
    if mean_lp < -1.0:
        return Heard(text, False, f"not sure ({conf:.0%})", conf)
    return Heard(text, True, "", conf)


def yes_no(text):
    """True for yes, False for no, None if unclear (unclear counts as no)."""
    t = text.lower()
    if NO.search(t):
        return False
    return True if YES.search(t) else None


class Endpointer:
    """Finds the end of a spoken command: it has started once loud enough, over once quiet for ``silence_s``."""

    def __init__(self, floor, silence_s=0.9, start_timeout_s=5.0, max_s=12.0, min_speech_s=0.25, level=None):
        """``floor`` is the background loudness; ``level`` how loud you just said "Hey Jarvis" through this microphone,
        so a quiet microphone is not held to the standard of a loud one."""
        self.threshold = max(60.0, floor * 3.0, min(450.0, 0.3 * level) if level else 450.0)
        self.silence_chunks = int(silence_s * RATE / CHUNK)
        self.start_chunks = int(start_timeout_s * RATE / CHUNK)
        self.max_chunks = int(max_s * RATE / CHUNK)
        self.min_chunks = max(1, int(min_speech_s * RATE / CHUNK))
        self.n = self.voiced = self.quiet = 0
        self.started = False

    def feed(self, chunk):
        """Returns None to keep going, or 'done' / 'nothing' / 'long' when recording should stop."""
        self.n += 1
        rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        if rms > self.threshold:
            self.voiced += 1
            self.quiet = 0
            if self.voiced >= 2:
                self.started = True
        else:
            self.quiet += 1
        if not self.started:
            return "nothing" if self.n >= self.start_chunks else None
        if self.quiet >= self.silence_chunks:
            return "done" if self.voiced >= self.min_chunks else "nothing"
        return "long" if self.n >= self.max_chunks else None


# -------------------------------------------------------------------------------------------- audio
def find_mic(name=None):
    """Index of the input device whose name contains ``name`` (default: Windows' default microphone)."""
    import sounddevice as sd
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            return i
    raise ValueError(f"no microphone with '{name}' in its name; `jarvis --list-mics` shows them")


def list_mics():
    import sounddevice as sd
    default = sd.default.device[0]
    return [(i, d["name"], i == default) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


class Mic:
    def __init__(self, device=None):
        import sounddevice as sd
        self.q = queue.Queue(maxsize=400)
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK, device=device,
                                     callback=self._on_audio)

    def _on_audio(self, indata, frames, time_info, status):
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def start(self):
        self.stream.start()

    def read(self, timeout=1.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass


class Transcriber:
    """Whisper on the CPU. Loaded when first needed and freed after a while, so it costs no memory while you are away."""

    def __init__(self, size="small.en", idle_unload_s=600):
        self.size, self.idle_unload_s = size, idle_unload_s
        self.model = None
        self.last_used = 0.0

    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.size, device="cpu", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 4))
        self.last_used = time.time()
        return self.model

    def maybe_unload(self):
        if self.model is not None and time.time() - self.last_used > self.idle_unload_s:
            self.model = None
            gc.collect()

    def transcribe(self, audio, language="en"):
        """audio: int16 or float32 mono 16 kHz. Returns Heard."""
        pcm = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio.astype(np.float32)
        if len(pcm) < RATE * 0.3:
            return Heard("", False, "too short")
        peak = float(np.abs(pcm).max())
        if 0 < peak < 0.5:
            pcm = pcm * min(0.8 / peak, 20.0)           # a quiet microphone: bring the voice up to a normal level first
        segs, _ = self.load().transcribe(
            pcm, language=language, beam_size=1, temperature=0.0, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200}, condition_on_previous_text=False,
            initial_prompt=PROMPT, no_speech_threshold=0.6, log_prob_threshold=-1.0)
        return judge([{"text": s.text, "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
                       "compression_ratio": s.compression_ratio} for s in segs])


class WakeWord:
    def __init__(self, threshold=0.5):
        import openwakeword
        from openwakeword.model import Model
        self.threshold = threshold
        try:
            self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception:  # noqa: BLE001 - models not downloaded yet
            openwakeword.utils.download_models(["hey_jarvis"])
            self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

    def score(self, chunk):
        return max(self.model.predict(chunk).values(), default=0.0)

    def heard(self, chunk):
        return self.score(chunk) >= self.threshold

    def reset(self):
        self.model.reset()


def mic_test(device=None, seconds=5, out=print):
    """Show which microphone is used, whether Windows has it muted, and a live level while you speak. Returns True if it hears you."""
    import sounddevice as sd
    info = sd.query_devices(device if device is not None else sd.default.device[0])
    out(f"microphone: {info['name']}")
    muted = media.mic_muted()
    if muted:
        out("  Windows has this microphone MUTED. Unmute it in Settings > System > Sound > Input (or press your mic-mute key).")
    mic = Mic(device)
    mic.start()
    out(f"  say something for {seconds} seconds (try: 'Hey Jarvis, play next song')...")
    peak, end = 0.0, time.time() + seconds
    while time.time() < end:
        c = mic.read(1.0)
        if c is None:
            continue
        rms = float(np.sqrt(np.mean(c.astype(np.float32) ** 2)))
        peak = max(peak, rms)
        out("  " + "#" * min(60, int(rms / 60)) + f" {rms:.0f}")
    mic.stream.stop()
    if peak < 5:
        out("  RESULT: pure silence. " + ("It is muted." if muted else "Check the mute switch, the input volume, and that Windows allows desktop apps to use the microphone."))
    elif peak < 150:
        out("  RESULT: very quiet. It will work (jarvis levels it up) but raise the input volume in Sound settings if it misses words.")
    else:
        out("  RESULT: I can hear you.")
    return peak >= 5


# ------------------------------------------------------------------------------------------ service
def lock_path():
    return forge_home() / "voice.lock"


def running_pid():
    try:
        pid = int(lock_path().read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if tasks.pid_alive(pid) else None


def claim():
    other = running_pid()
    if other and other not in (os.getpid(), os.getppid()):
        return False
    lock_path().write_text(str(os.getpid()))
    return True


def release():
    if running_pid() == os.getpid():
        lock_path().unlink(missing_ok=True)


def startup_file():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/jarvis-voice.vbs"


def set_autostart(on, extra_args=""):
    """Start the voice assistant when you log in (a script in your Startup folder; no admin rights needed)."""
    f = startup_file()
    if not on:
        f.unlink(missing_ok=True)
        return None
    out = forge_home() / "voice.out"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('CreateObject("WScript.Shell").Run "cmd /c """"' + tasks._python() + '"" ""' + str(tasks.REPO / "jarvis.py")
                 + '"" --voice ' + extra_args + ' >> ""' + str(out) + '"" 2>&1""", 0, False\r\n', encoding="utf-8")
    return f


class Voice:
    """The listening loop. ``ui`` is a Jarvis (assistant/tui.py) used for the model, the voice and the sandbox."""

    def __init__(self, ui, mic=None, whisper="small.en", threshold=0.5, beeps=True, log=print, source=None):
        """``source`` replaces the microphone (anything with start/read/flush), which is how the loop is tested."""
        self.ui, self.log, self.beeps = ui, log, beeps
        self.mic = source or Mic(find_mic(mic))
        self.stop = threading.Event()
        self.wake = WakeWord(threshold)
        self.stt = Transcriber(whisper)
        self.duck = media.Duck(15)
        self.floor = 150.0                      # background loudness, learned while idle
        self.speech_level = None                # how loud "Hey Jarvis" was, set at each wake-up
        self.missed = 0                         # wake-ups in a row where no command followed
        self.model_ready = False
        ui.speaker.enabled = True

    # ----- small helpers
    def beep(self, hz, ms=70):
        if self.beeps and sys.platform == "win32":
            import winsound
            winsound.Beep(hz, ms)

    def speak(self, text, duck=True):
        """Say it and wait until it has been said (so the microphone does not hear jarvis)."""
        spk = self.ui.speaker
        if duck:
            self.duck.down()
        spk.say(text)
        end = time.time() + 40
        while spk.busy() and time.time() < end:
            time.sleep(0.1)
        time.sleep(0.6 if spk.engine_name == "edge" else 0.2)   # the last sentence is still playing when the queue is empty
        if duck:
            self.duck.up()
        self.mic.flush()
        self.wake.reset()

    def record(self, start_timeout=5.0, preroll=None):
        """Record one spoken command (stops when you stop talking). Returns int16 audio or None."""
        ep = Endpointer(self.floor, start_timeout_s=start_timeout, level=self.speech_level)
        chunks = list(preroll or [])
        while True:
            c = self.mic.read(2.0)
            if c is None:
                return None
            chunks.append(c)
            verdict = ep.feed(c)
            if verdict in ("done", "long"):
                return np.concatenate(chunks)
            if verdict == "nothing":
                return None

    def listen(self, start_timeout=5.0):
        audio = self.record(start_timeout)
        return self.stt.transcribe(audio) if audio is not None else Heard("", False, "no speech")

    # ----- doing things
    def confirm(self, name, args):
        """Spoken yes/no for tools that change things outside the sandbox (sending mail)."""
        if name == "send_email":
            ask = f"Send an email to {args.get('to', 'them')} saying: {str(args.get('body', ''))[:120]}. Say yes or no."
        else:
            return False
        self.speak(ask, duck=False)
        self.beep(880)
        heard = self.listen(start_timeout=6.0)
        self.log(f"  confirm heard: {heard.text!r} ok={heard.ok}")
        return bool(heard.ok and yes_no(heard.text) is True)

    def ask_model(self, text):
        """Anything that is not a simple command goes to the model, unless a game needs the memory."""
        gov = self.ui.governor
        why = gov.busy_reason() if hasattr(gov, "busy_reason") else None
        if why:
            return f"I'll leave the thinking until you're done, {why}. Music and volume still work."
        if not self.model_ready:
            from . import serve
            ok, problem = serve.ensure_ready(self.ui.args.url, self.ui.args.model, say=self.log, start=not self.ui.args.no_start)
            if not ok:
                return "The language model isn't running, so I can only do simple commands right now."
            self.model_ready = True
        agent = self.ui.agent
        agent.confirm = lambda name, args=None: (self.ui.auto_runs(name, args) if name != "send_email" else self.confirm(name, args))
        hints, _, _ = self.ui.hints_for(text)
        hints = (hints + "\n\n" if hints else "") + "(This was spoken aloud. Answer in one or two short sentences, no lists or code.)"
        answer, _ = agent.run(text, hints=hints, on_tool=lambda n, a: self.log(f"  tool {n} {a}"))
        return answer

    def handle(self, heard):
        text = re.sub(r"^\W*(?:hey |ok |okay )?jarvis\W*", "", heard.text, flags=re.I).strip()
        if not text:
            return "Yes?", True
        cmd = commands.parse(text)
        self.duck.up()                       # a volume command must start from the real volume, not the lowered one
        if cmd is not None:
            self.log(f"  command: {cmd}")
            try:
                return commands.run(cmd), cmd["action"].startswith("volume")
            except Exception as e:  # noqa: BLE001
                return f"I couldn't do that. {type(e).__name__}", False
        self.log("  not a simple command, asking the model")
        return self.ask_model(text), False

    # ----- the loop
    def run(self):
        self.mic.start()
        self.log("listening for 'Hey Jarvis'. Ctrl+C stops.")
        if media.mic_muted():
            self.log("WARNING: your microphone is muted in Windows (Settings > System > Sound > Input), so I cannot hear you.")
        loud, recent = [], []
        while not self.stop.is_set():
            chunk = self.mic.read(1.0)
            if chunk is None:
                self.stt.maybe_unload()
                continue
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            loud = (loud + [rms])[-40:]
            self.floor = float(np.percentile(loud, 30))
            recent = (recent + [rms])[-15:]
            if not self.wake.heard(chunk):
                continue
            self.log("wake word")
            self.speech_level = max(recent) if recent else None
            threading.Thread(target=self.stt.load, daemon=True).start()      # loads while you speak (first time only)
            self.duck.down()
            self.beep(1100, 60)
            self.mic.flush()
            heard = self.listen()
            try:
                if not heard.ok:
                    self.log(f"  did not catch it ({heard.reason}): {heard.text!r}")
                    self.beep(500, 120)
                    if heard.reason in ("no speech", "too short"):
                        self.missed += 1
                        if self.missed >= 2:
                            self.missed = 0
                            self.speak("I heard my name but not what came after it. Say the command right after Jarvis, "
                                       "or turn your microphone up.")
                    else:
                        self.speak("Sorry, I didn't catch that.")
                    continue
                self.missed = 0
                self.log(f"  heard: {heard.text!r} ({heard.confidence:.0%})")
                reply, quiet = self.handle(heard)
                self.log(f"  reply: {reply}")
                self.speak(reply, duck=not quiet)
            except Exception as e:  # noqa: BLE001 - one bad command must not stop the assistant
                self.log(f"  error: {type(e).__name__}: {e}")
                self.speak("Something went wrong with that.")
            finally:
                self.duck.up()
                self.mic.flush()
                self.wake.reset()
                self.stt.maybe_unload()
