"""Control what is playing and the volume of the PC: next/previous song, pause, volume up/down/set, mute.

Media keys go to whatever player owns the Windows media session (YouTube Music in Chrome, Spotify...), the same as the
keys on a keyboard. Volume uses the Windows master volume (pycaw) and falls back to the volume keys. No admin rights.
"""

import atexit
import ctypes
import functools
import gc
import queue
import sys
import threading
import time

VK = {"next": 0xB0, "prev": 0xB1, "stop": 0xB2, "play_pause": 0xB3, "mute": 0xAD, "vol_down": 0xAE, "vol_up": 0xAF}
KEYEVENTF_KEYUP = 0x0002


def press(key):
    """Send one media key, as if pressed on the keyboard."""
    if sys.platform != "win32":
        raise OSError("media keys are only supported on Windows")
    code = VK[key]
    ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)


_jobs = None


def _com_thread():
    """All Windows audio (COM) calls run on this one thread, which initialises COM once and frees every object it makes.
    COM objects released later on some other thread (by the garbage collector) crash the whole process."""
    import comtypes
    comtypes.CoInitialize()
    while True:
        job = _jobs.get()
        if job is None:                           # shutting down: release everything while COM is still alive
            gc.collect()
            comtypes.CoUninitialize()
            return
        fn, args, kwargs, box, done = job
        try:
            box.append((True, fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001 - handed back to the caller
            box.append((False, e))
        gc.collect()                  # break reference cycles here, on the thread that made the objects
        done.set()


_worker = None


def _stop_com_thread():
    if _jobs is not None and _worker is not None and _worker.is_alive():
        _jobs.put(None)
        _worker.join(3)


def _com(fn):
    """Run a Windows audio call on the COM thread and return its result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _jobs, _worker
        if threading.current_thread().name == "jarvis-com":
            return fn(*args, **kwargs)                   # already there (one audio function calling another)
        if _jobs is None:
            _jobs = queue.Queue()
            _worker = threading.Thread(target=_com_thread, name="jarvis-com", daemon=True)
            _worker.start()
            atexit.register(_stop_com_thread)
        box, done = [], threading.Event()
        _jobs.put((fn, args, kwargs, box, done))
        if not done.wait(15):
            raise TimeoutError("the Windows audio service did not answer")
        ok, value = box[0]
        if not ok:
            raise value
        return value
    return wrapper


def _endpoint():
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    speakers = AudioUtilities.GetSpeakers()
    iface = getattr(speakers, "Activate", None)
    if iface is None:                                   # newer pycaw wraps the device
        return speakers.EndpointVolume
    return cast(iface(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))


@_com
def audio_active(seconds=0.6):
    """True if something is making sound right now (master output meter), so pause/resume can avoid a wrong toggle."""
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        dev = getattr(AudioUtilities.GetSpeakers(), "_dev", None)
        meter = cast(dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None), POINTER(IAudioMeterInformation))
        end = time.time() + seconds
        while time.time() < end:
            if meter.GetPeakValue() > 0.004:
                return True
            time.sleep(0.05)
        return False
    except Exception:  # noqa: BLE001 - cannot tell: let the caller just press the key
        return None


@_com
def mic_muted():
    """True/False for the default microphone's Windows mute switch, None if it cannot be read."""
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        dev = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(1, 1)      # eCapture, eConsole
        ep = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
        return bool(ep.GetMute())
    except Exception:  # noqa: BLE001
        return None


@_com
def get_volume():
    """Master volume 0-100, or None if it cannot be read."""
    try:
        return round(_endpoint().GetMasterVolumeLevelScalar() * 100)
    except Exception:  # noqa: BLE001 - no audio device, COM not ready
        return None


@_com
def set_volume(percent):
    """Set the master volume to an exact percentage (0-100). Returns the level now set."""
    percent = max(0, min(100, int(round(percent))))
    try:
        ep = _endpoint()
        ep.SetMute(0, None)
        ep.SetMasterVolumeLevelScalar(percent / 100, None)
        return percent
    except Exception:  # noqa: BLE001 - fall back to the keys: 50 presses down, then up (each press is 2%)
        for _ in range(50):
            press("vol_down")
        for _ in range(percent // 2):
            press("vol_up")
        return percent


def change_volume(delta):
    """Raise or lower the volume by ``delta`` points; returns the new level."""
    now = get_volume()
    if now is None:
        for _ in range(abs(delta) // 2 or 1):
            press("vol_up" if delta > 0 else "vol_down")
        return None
    return set_volume(now + delta)


@_com
def set_mute(on):
    try:
        _endpoint().SetMute(1 if on else 0, None)
    except Exception:  # noqa: BLE001
        press("mute")


class Duck:
    """Lower the volume while jarvis listens or talks (so the music does not drown the voice), then put it back."""

    def __init__(self, level=15):
        self.level, self.saved = level, None

    def down(self):
        if self.saved is None:
            self.saved = get_volume()
            if self.saved is not None and self.saved > self.level:
                set_volume(self.level)
            else:
                self.saved = None

    def up(self):
        if self.saved is not None:
            set_volume(self.saved)
            self.saved = None


def do(action, value=None):
    """Run a media action by name and return a short sentence to say back. Used by the voice commands."""
    if action in ("next", "prev", "stop", "play_pause"):
        press(action)
        return {"next": "Next song.", "prev": "Previous song.", "stop": "Stopped.", "play_pause": "Done."}[action]
    if action == "pause":
        if audio_active() is False:
            return "Nothing is playing."
        press("play_pause")
        return "Paused."
    if action == "resume":
        if audio_active():
            return "It is already playing."
        press("play_pause")
        return "Playing."
    if action == "restart":
        press("prev")
        return "Starting it again."
    if action == "volume_up":
        level = change_volume(value or 10)
        return f"Volume {level} percent." if level is not None else "Volume up."
    if action == "volume_down":
        level = change_volume(-(value or 10))
        return f"Volume {level} percent." if level is not None else "Volume down."
    if action == "volume_set":
        return f"Volume {set_volume(value)} percent."
    if action == "mute":
        set_mute(True)
        return "Muted."
    if action == "unmute":
        set_mute(False)
        return "Unmuted."
    raise ValueError(f"unknown media action {action!r}")
