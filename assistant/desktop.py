"""Desktop actions: open a site or an app, play a song, send an Outlook mail.

These run in jarvis's own process, not in the low-integrity jail: a jailed Chrome or Outlook could not reach its
profile, and it is the jail that stops the model's free-form commands from changing the machine. Each action here takes
a narrow input (a search text, an app name, a URL, a mail) so the model cannot use them to run arbitrary code. None of
them needs administrator rights.
"""

import os
import re
import subprocess
import sys
import urllib.parse
import webbrowser
from pathlib import Path

VIDEO_ID = re.compile(r'"videoRenderer":\{"videoId":"([\w-]{11})"')
APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
START_MENU = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
              Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
# words people say that are not the file name of the program
ALIASES = {"chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe",
           "outlook": "outlook.exe", "excel": "excel.exe", "word": "winword.exe", "powerpoint": "powerpnt.exe",
           "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
           "explorer": "explorer.exe", "file explorer": "explorer.exe", "task manager": "taskmgr.exe",
           "cmd": "cmd.exe", "command prompt": "cmd.exe", "powershell": "powershell.exe"}


def _registered_path(exe):
    """Full path of a program Windows knows by name (the 'App Paths' registry key), or None."""
    if sys.platform != "win32":
        return None
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, rf"{APP_PATHS}\{exe}") as key:
                path = winreg.QueryValue(key, None)
        except OSError:
            continue
        if path and Path(path.strip('"')).exists():
            return path.strip('"')
    return None


def chrome_path():
    return _registered_path("chrome.exe") or next(
        (str(p) for p in (Path(os.environ.get(v, "")) / r"Google\Chrome\Application\chrome.exe"
                          for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")) if p.exists()), None)


def open_url(url):
    """Open a web address in Chrome (or the default browser)."""
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        if re.match(r"[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        else:
            return "Error: open_url needs an http(s) address like https://www.youtube.com"
    chrome = chrome_path()
    if chrome:
        subprocess.Popen([chrome, url], close_fds=True)
        return f"Opened {url} in Chrome."
    webbrowser.open(url)
    return f"Opened {url} in the default browser."


def find_video(query):
    """Id and title-less URL of the top YouTube search hit for ``query`` (no API key; reads the results page)."""
    import httpx
    resp = httpx.get("https://www.youtube.com/results", params={"search_query": query}, timeout=20,
                     follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                     "AppleWebKit/537.36 Chrome/120 Safari/537.36",
                                                     "Accept-Language": "en-US,en;q=0.9"},
                     cookies={"CONSENT": "YES+1", "SOCS": "CAI"})
    resp.raise_for_status()
    ids = VIDEO_ID.findall(resp.text)
    return ids[0] if ids else None


def play_music(query, service="youtube_music"):
    """Find the song on YouTube and open it in Chrome so it starts playing."""
    query = (query or "").strip()
    if not query:
        return "Error: say which song to play."
    try:
        vid = find_video(query)
    except Exception as e:  # noqa: BLE001 - offline, blocked, page changed
        vid = None
        why = f" ({type(e).__name__})"
    else:
        why = ""
    host = "music.youtube.com" if service == "youtube_music" else "www.youtube.com"
    if not vid:
        page = "search?q=" if host.startswith("music") else "results?search_query="
        url = f"https://{host}/{page}{urllib.parse.quote_plus(query)}"
        open_url(url)
        return (f"Could not find the video{why}, so the search page for '{query}' is open instead. "
                "Tell the user to click the first result.")
    url = f"https://{host}/watch?v={vid}"
    open_url(url)
    return f"Playing '{query}': opened {url} in Chrome. It should start by itself."


def _start_menu_match(name):
    wanted = name.lower().strip()
    best = None
    for root in START_MENU:
        if not root.is_dir():
            continue
        for lnk in root.rglob("*.lnk"):
            stem = lnk.stem.lower()
            if stem == wanted:
                return lnk
            if wanted in stem and (best is None or len(stem) < len(best.stem)):
                best = lnk
    return best


def open_app(name):
    """Start an installed program by name (Chrome, Outlook, Excel, Spotify...). Only installed apps, no arguments."""
    name = (name or "").strip()
    if not name or re.search(r"[\\/:;|&<>]", name):
        return "Error: give just the app's name, like 'Outlook' or 'Spotify'."
    exe = ALIASES.get(name.lower())
    target = (_registered_path(exe) or exe) if exe else None
    if target:
        try:
            os.startfile(target)
            return f"Started {name}."
        except OSError:
            pass
    lnk = _start_menu_match(name)
    if lnk:
        os.startfile(str(lnk))
        return f"Started {lnk.stem}."
    return f"Error: no installed app called '{name}' was found in the Start menu."


def send_email(to, subject, body, cc=None):
    """Send a mail through the user's classic Outlook (COM). New Outlook has no COM; then a draft is opened instead."""
    to = (to or "").strip()
    if not re.fullmatch(r"[^@\s;,]+@[^@\s;,]+(\s*[;,]\s*[^@\s;,]+@[^@\s;,]+)*", to):
        return "Error: 'to' must be one or more email addresses."
    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To, mail.Subject, mail.Body = to.replace(",", ";"), subject or "", body or ""
        if cc:
            mail.CC = cc.replace(",", ";")
        mail.Send()
        return f"Sent from Outlook to {to}: '{subject}'."
    except Exception as e:  # noqa: BLE001 - not installed, new Outlook, no pywin32, profile not set up
        q = urllib.parse.quote
        os.startfile(f"mailto:{to}?subject={q(subject or '')}&body={q(body or '')}")
        return (f"Could not send automatically ({type(e).__name__}); a draft is open in the default mail app. "
                "Tell the user to press Send.")
