"""Bring the local model server up from a cold start, so `jarvis` works without any manual step.

Ollama is started in the background if it is not running, the `forge` model is registered from the Modelfile if
Ollama does not know it yet, and the model is pre-loaded into memory so the first message does not wait for it.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent


def root_url(url):
    url = url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def is_ollama(url):
    return ":11434" in url


def server_up(url, timeout=2.0):
    try:
        r = httpx.get(f"{root_url(url)}/api/tags" if is_ollama(url) else f"{url.rstrip('/')}/models", timeout=timeout)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def installed_models(url):
    try:
        data = httpx.get(f"{root_url(url)}/api/tags", timeout=3).json()
        return [m["name"] for m in data.get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def loaded_models(url):
    """Names of models currently held in memory by Ollama."""
    try:
        return [m["name"] for m in httpx.get(f"{root_url(url)}/api/ps", timeout=3).json().get("models", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        return []


def ollama_exe():
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "Programs" / "Ollama" / "ollama.exe").exists():
        return str(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    return None


def start_ollama(exe):
    flags = 0x00000008 | 0x08000000 if sys.platform == "win32" else 0  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def ensure_ready(url, model, say=lambda m: None, start=True, wait=60):
    """Make sure the server answers and knows ``model``. Returns (ok, problem)."""
    if not server_up(url):
        if not (start and is_ollama(url)):
            return False, f"No model server answers at {url}. Start it first (scripts\\serve.ps1 or `ollama serve`)."
        exe = ollama_exe()
        if not exe:
            return False, "Ollama is not installed. Get it from https://ollama.com and run this again."
        say("starting Ollama...")
        start_ollama(exe)
        deadline = time.time() + wait
        while time.time() < deadline and not server_up(url):
            time.sleep(0.5)
        if not server_up(url):
            return False, f"Ollama did not come up within {wait}s. Try `ollama serve` in another terminal to see why."
    if is_ollama(url):
        names = installed_models(url)
        if not any(n == model or n.split(":")[0] == model for n in names):
            modelfile, weights = REPO / "Modelfile", REPO / "models" / "forge-8b-Q4_K_M.gguf"
            exe = ollama_exe()
            if model == "forge" and exe and modelfile.exists() and weights.exists():
                say("registering forge in Ollama (first run only)...")
                proc = subprocess.run([exe, "create", "forge", "-f", str(modelfile)], cwd=REPO, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace")
                if proc.returncode != 0:
                    return False, f"`ollama create forge` failed: {proc.stderr.strip()[-300:]}"
            else:
                return False, f"Ollama has no model called '{model}'. Installed: {', '.join(names) or 'none'}."
    return True, ""


def warm_up(url, model, keep_alive="30m"):
    """Load the model into memory in the background so the first reply is not slow."""
    if not is_ollama(url) or any(n == model or n.split(":")[0] == model for n in loaded_models(url)):
        return None

    def _go():
        try:
            httpx.post(f"{root_url(url)}/api/generate", json={"model": model, "prompt": "", "keep_alive": keep_alive}, timeout=600)
        except httpx.HTTPError:
            pass

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t
