"""Talk to jarvis: say "Jarvis" and then what you want, in one breath or after a beep.

    microphone -> cut into utterances (when you start and stop talking, relative to the room's noise level)
        -> a fast Whisper model (base.en) checks that the utterance starts with "Jarvis"; the rest is the command
        -> simple commands (next song, volume 40%, play <song>, open <app/folder>) run at once, answered with a chime
        -> anything else goes to the model (or, in the terminal chat, becomes your next message), and the answer is spoken.

After a command there is a short follow-up window (8 s) in which you can keep talking without saying Jarvis again.

Not acting on misheard words is handled in layers: the "Jarvis" gate, a vocabulary hint for Whisper, a confidence check
(a doubtful transcript is dropped instead of being acted on), a filter for the phrases Whisper invents from silence, and
a spoken yes/no before anything that sends mail. Quiet microphones work because the audio is levelled before Whisper and
the speech detector follows the room's noise rather than a fixed loudness.

The same engine runs in two places: inside the terminal chat (``jarvis``, listening is on by default) and as a hidden
background service that starts at login (``jarvis --voice-autostart on``). No administrator rights. Needs the microphone
allowed for desktop apps (Windows Settings > Privacy > Microphone) and not muted.
"""

import _thread
import collections
import gc
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import commands, media, tasks, voicemodes
from .session import forge_home

RATE = 16000
CHUNK = 1280                       # 80 ms
FOLLOW_S = 8.0                     # how long after a command you can keep talking without saying Jarvis
PROMPT = ("Jarvis. Play next song. Previous song. Pause. Resume. Volume up. Volume down. Set volume to sixty percent. "
          "Play Mere Rashke Qamar on YouTube Music. Open Outlook. Open the Documents folder. Open Chrome.")
HALLUCINATIONS = {"thank you", "thanks", "thank you.", "thanks for watching", "thank you for watching", "you", "bye", "bye.",
                  "subtitles by the amara.org community", "please subscribe", "like and subscribe", "the end", "okay", "ok",
                  "hmm", "uh", "um", "mm-hmm", "so"}
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|send it|send|confirm|go ahead|do it|correct|affirmative|ok|okay)\b", re.I)
NO = re.compile(r"\b(no|nope|nah|cancel|stop|don'?t|do not|never ?mind|negative|abort)\b", re.I)

# "Jarvis" as Whisper tends to write it, with the greeting people put in front
WAKE_TOKEN = r"(?:j[ae]r+[vw][a-z]{1,3}|j[ae]r vis|garvis|charvis|darvis)"
LEAD = r"(?:(?:hey|hi|ok|okay|yo|hello|hay)[\s,]+)?"
WAKE_AT_START = re.compile(rf"^\W*{LEAD}{WAKE_TOKEN}\b[\s,.:;!?-]*", re.I)


# ------------------------------------------------------------------------------------- trust the words
@dataclass
class Heard:
    text: str
    ok: bool
    reason: str = ""
    confidence: float = 0.0


@dataclass
class Reply:
    """What to do after a command: say something, or just chime. ``llm``: it came from the model (so a free-text
    follow-up makes sense). ``note`` is shown in the log."""
    speak: str = ""
    chime: bool = False
    llm: bool = False
    note: str = ""


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


def is_wake(text):
    """Does this transcript start with "Jarvis" (maybe "hey Jarvis")?"""
    return bool(WAKE_AT_START.match(text or ""))


def strip_wake(text):
    """The transcript without the leading "Jarvis"."""
    return WAKE_AT_START.sub("", text or "", count=1).strip()


# ------------------------------------------------------------------------------------- speech detection
def rms_of(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


class Detector:
    """Cuts the microphone stream into utterances. An utterance starts when the sound rises well above the room's noise
    (``floor``) and ends after ``silence_s`` of quiet. ``feed`` returns None, "started", "early" (1.6 s in, time to check
    whether it begins with Jarvis) or "ended"."""

    def __init__(self, silence_s=0.7, early_s=1.6, max_s=12.0, min_voiced_s=0.3):
        self.silence = int(silence_s * RATE / CHUNK)
        self.early = int(early_s * RATE / CHUNK)
        self.max = int(max_s * RATE / CHUNK)
        self.min_voiced = max(2, int(min_voiced_s * RATE / CHUNK))
        self.pre = collections.deque(maxlen=7)            # a little sound from before the onset, so the first word is not clipped
        self.reset()

    def reset(self):
        self.buf, self.active, self.skipping = [], False, False
        self.voiced = self.voiced_total = self.quiet = 0
        self.early_done = False
        self.peak = 0.0
        self.pre.clear()

    @staticmethod
    def threshold(floor):
        return max(45.0, 2.5 * floor)

    def feed(self, chunk, floor):
        rms = rms_of(chunk)
        thr = self.threshold(floor)
        if self.skipping:                                  # the rest of something that is not for us: wait until it is over
            self.quiet = self.quiet + 1 if rms < thr else 0
            if self.quiet >= self.silence // 2:
                self.reset()
            return None
        if not self.active:
            self.pre.append(chunk)
            self.voiced = self.voiced + 1 if rms > thr else 0
            if self.voiced >= 2:
                self.active, self.buf, self.quiet = True, list(self.pre), 0
                self.voiced_total, self.early_done, self.peak = self.voiced, False, rms
                return "started"
            return None
        self.buf.append(chunk)
        if rms > thr:
            self.voiced_total += 1
            self.quiet = 0
            self.peak = max(self.peak, rms)
        else:
            self.quiet += 1
        if self.quiet >= self.silence or len(self.buf) >= self.max:
            return "ended"
        if not self.early_done and len(self.buf) >= self.early:
            self.early_done = True
            return "early"
        return None

    def audio(self):
        return np.concatenate(self.buf) if self.buf else np.zeros(0, np.int16)

    def long_enough(self):
        return self.voiced_total >= self.min_voiced        # a door slam or a click is not speech

    def skip(self):
        """Drop this utterance and ignore the rest of it."""
        peak = self.peak
        self.reset()
        self.skipping, self.peak = True, peak


class Endpointer:
    """Finds the end of one spoken command when we are waiting for it (after a beep): it has started once loud enough,
    over once quiet for ``silence_s``."""

    def __init__(self, floor, silence_s=0.9, start_timeout_s=5.0, max_s=12.0, min_speech_s=0.25, level=None):
        """``floor`` is the background loudness; ``level`` how loud you just spoke through this microphone, so a quiet
        microphone is not held to the standard of a loud one."""
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
        if rms_of(chunk) > self.threshold:
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

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass

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
    """Whisper on the CPU. Loaded when first needed (or in the background) and freed after a while if ``idle_unload_s``."""

    def __init__(self, size="small.en", idle_unload_s=600, threads=None):
        self.size, self.idle_unload_s = size, idle_unload_s
        self.threads = threads or min(8, os.cpu_count() or 4)
        self.model = None
        self.last_used = 0.0
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self.model is None:
                from faster_whisper import WhisperModel
                self.model = WhisperModel(self.size, device="cpu", compute_type="int8", cpu_threads=self.threads)
            self.last_used = time.time()
            return self.model

    def maybe_unload(self):
        if self.idle_unload_s and self.model is not None and time.time() - self.last_used > self.idle_unload_s:
            with self._lock:
                self.model = None
            gc.collect()

    def run(self, audio, prompt=PROMPT, language="en"):
        """Segments (dicts) for int16 or float32 mono 16 kHz audio."""
        pcm = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio.astype(np.float32)
        if len(pcm) < RATE * 0.3:
            return []
        peak = float(np.abs(pcm).max())
        if 0 < peak < 0.5:
            pcm = pcm * min(0.8 / peak, 20.0)           # a quiet microphone: bring the voice up to a normal level first
        segs, _ = self.load().transcribe(
            pcm, language=language, beam_size=1, temperature=0.0, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200}, condition_on_previous_text=False,
            initial_prompt=prompt, no_speech_threshold=0.6, log_prob_threshold=-1.0)
        return [{"text": s.text, "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
                 "compression_ratio": s.compression_ratio} for s in segs]

    def transcribe(self, audio, language="en"):
        """Run and judge: returns Heard."""
        if len(audio) < RATE * 0.3:
            return Heard("", False, "too short")
        return judge(self.run(audio, language=language))


# ------------------------------------------------------------------------------------------ service
def lock_path():
    return forge_home() / "voice.lock"


def stop_path():
    return forge_home() / "voice.stop"


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


def stop_requested():
    """Another jarvis (the terminal chat) asked this background service to step aside."""
    try:
        return stop_path().read_text().strip() == str(os.getpid())
    except OSError:
        return False


def takeover(wait=8.0):
    """Become the one listener: ask a running background service to quit, then claim the microphone. True if we have it."""
    other = running_pid()
    if other and other != os.getpid():
        stop_path().write_text(str(other))
        end = time.time() + wait
        while time.time() < end and running_pid() == other:
            time.sleep(0.2)
        stop_path().unlink(missing_ok=True)
    return claim()


def startup_file():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/jarvis-voice.vbs"


def autostart_enabled():
    return startup_file().exists()


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


def start_service(extra_args=()):
    """Start the background voice service now (hidden, detached) unless one is running. Returns its pid or None."""
    if running_pid():
        return running_pid()
    cmd = [tasks._python(), str(tasks.REPO / "jarvis.py"), "--voice", *extra_args]
    flags = 0x00000008 | 0x08000000 | 0x00000200 if sys.platform == "win32" else 0
    out = open(forge_home() / "voice.out", "a", encoding="utf-8")
    return subprocess.Popen(cmd, cwd=tasks.REPO, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True).pid


def lower_priority():
    """Run below normal priority so a game or video never stutters because jarvis is transcribing."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)


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
    out(f"  say something for {seconds} seconds (try: 'Jarvis, play next song')...")
    peak, end = 0.0, time.time() + seconds
    while time.time() < end:
        c = mic.read(1.0)
        if c is None:
            continue
        rms = rms_of(c)
        peak = max(peak, rms)
        out("  " + "#" * min(60, int(rms / 60)) + f" {rms:.0f}")
    mic.stop()
    if peak < 5:
        out("  RESULT: pure silence. " + ("It is muted." if muted else "Check the mute switch, the input volume, and that Windows allows desktop apps to use the microphone."))
    elif peak < 150:
        out("  RESULT: very quiet. It will work (jarvis levels it up) but raise the input volume in Sound settings if it misses words.")
    else:
        out("  RESULT: I can hear you.")
    return peak >= 5


# ------------------------------------------------------------------------------------------- the listener
class Voice:
    """The listening loop. ``ui`` is a Jarvis (assistant/tui.py): its voice, model and sandbox are used.
    ``handler(text, heard) -> Reply`` decides what a command does; the default (the background service) runs quick
    commands directly and asks the model for the rest."""

    def __init__(self, ui, mic=None, whisper="small.en", fast="base.en", beeps=True, log=print, source=None,
                 handler=None, follow_s=FOLLOW_S):
        """``source`` replaces the microphone (anything with start/read/flush), which is how the loop is tested."""
        self.ui, self.log, self.beeps, self.follow_s = ui, log, beeps, follow_s
        self.mic = source or Mic(find_mic(mic))
        self.stop = threading.Event()
        self.fast = Transcriber(fast, idle_unload_s=0, threads=3)       # always loaded: it checks for "Jarvis"
        self.stt = Transcriber(whisper)                                   # the careful one, for the actual command
        self.duck = media.Duck(15)
        self.det = Detector()
        self.handler = handler or self.service_handler
        self.floor = 20.0                       # background loudness, learned while idle
        self.woke = False                       # the utterance in progress began with "Jarvis"
        self.follow_until = 0.0                 # until then you can talk without saying Jarvis
        self.follow_any = False                 # in a conversation with the model, follow-ups can be free text
        self.deaf_until = 0.0
        self.missed = 0
        self.model_ready = False
        ui.speaker.enabled = True if handler is None else ui.speaker.enabled

    # ----- sounds and speech
    def _tone(self, notes):
        if not self.beeps or sys.platform != "win32":
            return
        import winsound

        def play():
            for hz, ms in notes:
                winsound.Beep(hz, ms)
        threading.Thread(target=play, daemon=True).start()
        self.deaf_until = time.time() + sum(ms for _, ms in notes) / 1000 + 0.25    # the microphone hears our own beep

    def chime(self, kind="ok"):
        self._tone({"ok": [(988, 55), (1319, 75)], "listen": [(1175, 70)], "fail": [(392, 140)]}[kind])

    def speak(self, text, duck=True):
        """Say it and wait until it has been said (so the microphone does not hear jarvis)."""
        spk = self.ui.speaker
        if duck:
            self.duck.down()
        spk.say(text)
        end = time.time() + 40
        while spk.busy() and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.25)
        if duck:
            self.duck.up()
        self.mic.flush()
        self.det.reset()

    def deaf(self):
        """True while jarvis itself is making sound (its voice, a beep): it must not hear itself."""
        if self.ui.speaker.busy() or time.time() < self.deaf_until:
            self.mic.flush()
            self.det.reset()
            if self.ui.speaker.busy():
                self.deaf_until = time.time() + 0.4
            return True
        return False

    # ----- listening for one thing (after a beep, or a yes/no)
    def record(self, start_timeout=5.0):
        """Record one spoken command (stops when you stop talking). Returns int16 audio or None."""
        ep = Endpointer(self.floor, start_timeout_s=start_timeout, level=self.det.peak or None)
        chunks = []
        while not self.stop.is_set():
            c = self.mic.read(2.0)
            if c is None:
                return None
            chunks.append(c)
            verdict = ep.feed(c)
            if verdict in ("done", "long"):
                return np.concatenate(chunks)
            if verdict == "nothing":
                return None
        return None

    def listen(self, start_timeout=5.0):
        audio = self.record(start_timeout)
        return self.stt.transcribe(audio) if audio is not None else Heard("", False, "no speech")

    # ----- the default handler: the background service
    def confirm(self, name, args):
        """Spoken yes/no for tools that change things outside the sandbox (sending mail)."""
        if name != "send_email":
            return False
        self.speak(f"Send an email to {args.get('to', 'them')} saying: {str(args.get('body', ''))[:120]}. Say yes or no.", duck=False)
        self.chime("listen")
        time.sleep(0.2)
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

    def service_handler(self, text, heard):
        cmd = commands.parse(text)
        if cmd is not None:
            self.duck.up()                  # a volume command must start from the real volume, not a lowered one
            try:
                result = commands.run(cmd)
            except Exception as e:  # noqa: BLE001
                return Reply(speak=f"I couldn't do that. {type(e).__name__}")
            bad = result.startswith(("Error", "Could not"))
            return Reply(speak=result if bad else "", chime=not bad, note=result)
        self.log("  not a simple command, asking the model")
        return Reply(speak=self.ask_model(text), llm=True)

    # ----- one finished utterance
    def dispatch(self, text, heard):
        self.log(f"heard: {text!r} ({heard.confidence:.0%})")
        try:
            reply = self.handler(text, heard)
        except Exception as e:  # noqa: BLE001 - one bad command must not stop the assistant
            self.log(f"  error: {type(e).__name__}: {e}")
            reply = Reply(speak="Something went wrong with that.")
        if reply.note:
            self.log(f"  {reply.note}")
        if reply.speak:
            self.speak(reply.speak)
        elif reply.chime:
            self.chime("ok")
        self.missed = 0
        self.follow_until = time.time() + self.follow_s
        self.follow_any = reply.llm

    def prompt_mode(self):
        """You said only "Jarvis": beep and wait for the command."""
        self.log("  yes? (listening for the command)")
        self.chime("listen")
        time.sleep(0.15)
        self.mic.flush()
        self.duck.down()
        try:
            heard = self.listen(start_timeout=6.0)
        finally:
            self.duck.up()
        if not heard.ok:
            self.log(f"  no command ({heard.reason})")
            self.chime("fail")
            return
        text = strip_wake(heard.text) if is_wake(heard.text) else heard.text
        if text:
            self.dispatch(text, heard)

    def process(self, audio, woke):
        """A finished utterance: is it for jarvis, and what does it say?"""
        following = time.time() < self.follow_until
        segs = self.fast.run(audio)
        fast_text = " ".join(s["text"].strip() for s in segs).strip()
        addressed = woke or is_wake(fast_text)
        if not addressed and not following:
            self.log(f"  (not for me: {fast_text!r})") if fast_text else None
            return
        rest = (strip_wake(fast_text) if is_wake(fast_text) else fast_text).strip()
        if addressed and not rest:
            return self.prompt_mode()
        fast_heard = judge(segs)
        if fast_heard.ok and commands.parse(rest) is not None:
            return self.dispatch(rest, fast_heard)               # a known command, heard well: no need for the slower model
        if not addressed and not self.follow_any:
            self.log(f"  (ignored, no Jarvis: {fast_text!r})")      # chatter in the room right after a command
            return
        heard = self.stt.transcribe(audio)
        if not heard.ok:
            self.log(f"  did not catch it ({heard.reason}): {heard.text!r}")
            if addressed and heard.reason not in ("no speech", "too short"):
                self.chime("fail")
            return
        text = (strip_wake(heard.text) if is_wake(heard.text) else heard.text).strip()
        if not text:
            return self.prompt_mode() if addressed else None
        self.dispatch(text, heard)

    # ----- the loop
    def run(self):
        self.mic.start()
        threading.Thread(target=self.fast.load, daemon=True).start()
        self.log("listening: say 'Jarvis' and then what you want. Ctrl+C stops.")
        if media.mic_muted():
            self.log("WARNING: your microphone is muted in Windows (Settings > System > Sound > Input), so I cannot hear you.")
        loud = []
        while not self.stop.is_set():
            if stop_requested():
                self.log("another jarvis is taking over the microphone; stepping aside")
                break
            chunk = self.mic.read(0.5)
            if chunk is None:
                self.stt.maybe_unload()
                continue
            if self.deaf():
                continue
            if not self.det.active and not self.det.skipping:
                loud = (loud + [rms_of(chunk)])[-40:]
                self.floor = float(np.percentile(loud, 30))
            event = self.det.feed(chunk, self.floor)
            if event is None:
                continue
            try:
                if event == "early":
                    if time.time() < self.follow_until:
                        continue                                    # follow-up: no need for the name
                    text = " ".join(s["text"] for s in self.fast.run(self.det.audio()))
                    if is_wake(text):
                        self.woke = True
                    else:
                        self.log(f"  (not for me: {text.strip()!r})") if text.strip() else None
                        self.woke = False
                        self.det.skip()
                elif event == "ended":
                    audio, ok, woke = self.det.audio(), self.det.long_enough(), self.woke
                    self.det.reset()
                    self.woke = False
                    if ok:
                        self.process(audio, woke)
                    self.mic.flush() if self.ui.speaker.busy() else None
            except Exception as e:  # noqa: BLE001 - keep listening whatever happens
                self.log(f"  error: {type(e).__name__}: {e}")
                self.det.reset()
                self.woke = False
            self.stt.maybe_unload()
        self.mic.stop()


# ------------------------------------------------------------------------------- the terminal chat
WAKE_PROMPT = "\x00voice"


class TerminalBridge:
    """Connects the live voice thread to the terminal chat: what you say becomes your next message there, mode phrases
    ("auto mode", "reset", "speak on") become the slash commands, and quick commands (music, volume, open) run at once
    even while the model is busy."""

    def __init__(self, ui):
        self.ui = ui
        self.inbox = queue.Queue()
        self.session = None
        self.voice = None

    def handler(self, text, heard):                      # runs on the voice thread
        ui = self.ui
        if voicemodes.is_stop(text) and getattr(ui, "task_running", False):
            _thread.interrupt_main()                     # same as Ctrl+C: the running task stops
            return Reply(chime=True, note="stopping the task")
        mode = voicemodes.parse(text)
        if mode:
            line, say = mode
            self.inbox.put(("cmd", line, say))
            self.poke()
            return Reply(note=line)
        cmd = commands.parse(text)
        if cmd is not None:
            try:
                result = commands.run(cmd)
            except Exception as e:  # noqa: BLE001
                result = f"Could not do that: {type(e).__name__}"
            bad = result.startswith(("Error", "Could not"))
            return Reply(speak=result if bad else "", chime=not bad, note=result)
        self.inbox.put(("task", text, ""))
        self.poke()
        return Reply(llm=True, note="sent to the chat")

    def take(self):
        try:
            return self.inbox.get_nowait()
        except queue.Empty:
            return None

    def poke(self):
        """Wake the chat's input line so it picks up what was just said."""
        app = getattr(self.session, "app", None)
        try:
            if app is not None and app.is_running and app.loop is not None:
                app.loop.call_soon_threadsafe(lambda: app.is_running and app.exit(result=WAKE_PROMPT))
        except Exception:  # noqa: BLE001 - the line is not being read right now; take() finds it afterwards
            pass

    def after_task(self):
        """A voice-started answer has been spoken: keep the follow-up window open from now."""
        if self.voice is not None:
            self.voice.follow_until = time.time() + self.voice.follow_s
            self.voice.follow_any = True


def start_in_terminal(ui, args):
    """Start listening inside the terminal chat. Returns a TerminalBridge, or None with a note why not."""
    from rich.markup import escape
    c = ui.console
    try:
        import faster_whisper  # noqa: F401
        import sounddevice  # noqa: F401
    except ImportError as e:
        c.print(f"[dim]voice off: {e.name} is not installed (pip install faster-whisper sounddevice pycaw)[/dim]")
        return None
    if not takeover():
        c.print(f"[dim]voice off: another jarvis (pid {running_pid()}) is already listening[/dim]")
        return None
    bridge = TerminalBridge(ui)
    log = lambda m: c.print(f"[magenta]voice[/magenta] [dim]{escape(m)}[/dim]")
    try:
        bridge.voice = Voice(ui, mic=args.mic, whisper=args.whisper, log=log, handler=bridge.handler)
    except Exception as e:  # noqa: BLE001 - no microphone, device busy
        release()
        c.print(f"[dim]voice off: {escape(str(e))}[/dim]")
        return None
    threading.Thread(target=bridge.voice.run, daemon=True, name="jarvis-voice").start()
    return bridge


def stop_in_terminal(bridge):
    """Stop listening; if jarvis starts at login, hand the microphone back to the background service."""
    if bridge is None or bridge.voice is None:
        return
    bridge.voice.stop.set()
    time.sleep(0.3)
    release()
    if autostart_enabled():
        start_service()
