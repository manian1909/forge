"""Desktop actions: open a site or an app, play a song, send an Outlook mail.

These run in jarvis's own process, not in the low-integrity jail: a jailed Chrome or Outlook could not reach its
profile, and it is the jail that stops the model's free-form commands from changing the machine. Each action here takes
a narrow input (a search text, an app name, a URL, a mail) so the model cannot use them to run arbitrary code. None of
them needs administrator rights.
"""

import difflib
import os
import re
import subprocess
import sys
import time
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


def reuse_tab(url, service):
    """Point an already open YouTube / YouTube Music tab at ``url`` (so a new song does not add a second tab and a second
    song playing at the same time). True if a tab was reused. Windows UI Automation through PowerShell; no admin."""
    script = Path(__file__).with_name("reuse_tab.ps1")
    if sys.platform != "win32" or not script.exists() or not chrome_path():
        return False
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                             env={**os.environ, "JARVIS_URL": url, "JARVIS_SERVICE": service}, creationflags=0x08000000).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.strip().startswith("REUSED")


def _show(url, service):
    """Open ``url`` in the tab that is already playing, else in a new tab (pausing whatever else is playing first)."""
    if reuse_tab(url, service):
        return "in the tab that was already open"
    try:
        from . import media
        if media.audio_active(0.3):
            media.press("play_pause")            # something else is playing: stop it, or two songs play at once
            time.sleep(0.3)
    except Exception:  # noqa: BLE001 - no audio device: just open the page
        pass
    open_url(url)
    return "in a new tab"


def play_music(query, service="youtube_music"):
    """Find the song on YouTube and play it in Chrome, in the tab that is already playing if there is one."""
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
        _show(url, service)
        return (f"Could not find the video{why}, so the search page for '{query}' is open instead. "
                "Tell the user to click the first result.")
    url = f"https://{host}/watch?v={vid}"
    where = _show(url, service)
    return f"Playing '{query}' {where}. It should start by itself."


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


_start_apps = {"at": 0.0, "apps": {}}


def start_apps(max_age=600):
    """{name: AppID} of everything in the Start menu, Store apps included (Get-StartApps); cached for a few minutes."""
    if time.time() - _start_apps["at"] > max_age or not _start_apps["apps"]:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                                 creationflags=0x08000000).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        apps = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
        if apps:
            _start_apps.update(at=time.time(), apps=apps)
    return _start_apps["apps"]


def match_app(name, apps):
    """Best Start-menu entry for a spoken or typed name: exact, then whole-word, then substring, then close spelling."""
    wanted = re.sub(r"\s+", " ", name.lower().strip())
    wanted = re.sub(r"^(the|my)\s+|\s+(app|application|program|software)$", "", wanted)
    names = {n.lower(): n for n in apps}
    if wanted in names:
        return names[wanted]
    words = [n for low, n in names.items() if re.search(rf"\b{re.escape(wanted)}\b", low)]
    if not words:
        words = [n for low, n in names.items() if wanted in low]
    skip = re.compile(r"uninstall|help|readme|documentation|license|release notes|manual", re.I)
    words = [n for n in words if not skip.search(n)] or words
    if words:
        return min(words, key=len)
    close = difflib.get_close_matches(wanted, list(names), n=1, cutoff=0.72)
    return names[close[0]] if close else None


def open_app(name):
    """Start any installed program by name (Chrome, Outlook, Excel, Spotify, WhatsApp, Calculator, Settings...)."""
    name = (name or "").strip()
    if not name or re.search(r"[\\/:;|&<>]", name):
        return "Error: give just the app's name, like 'Outlook' or 'Spotify'."
    low = name.lower()
    if re.fullmatch(r"(the )?(file|files|windows)? ?explorer|my computer|this pc", low):
        os.startfile("explorer.exe")
        return "Opened File Explorer."
    if low in ("settings", "windows settings"):
        os.startfile("ms-settings:")
        return "Opened Settings."
    exe = ALIASES.get(low)
    target = (_registered_path(exe) or exe) if exe else None
    if target:
        try:
            os.startfile(target)
            return f"Started {name}."
        except OSError:
            pass
    apps = start_apps()
    hit = match_app(name, apps)
    if hit:
        os.startfile(f"shell:AppsFolder\\{apps[hit]}")
        return f"Started {hit}."
    lnk = _start_menu_match(name)
    if lnk:
        os.startfile(str(lnk))
        return f"Started {lnk.stem}."
    return f"Error: no installed app called '{name}' was found in the Start menu."


# ------------------------------------------------------------------------------------------- folders
KNOWN_FOLDERS = {"documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7", "downloads": "374DE290-123F-4565-9164-39C4925E467B",
                 "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
                 "music": "4BD8D571-6D19-48D3-BE97-422220080E43", "videos": "18989B1D-99B5-43EB-A281-6845E8AC66C0"}
FOLDER_WORDS = {"document": "documents", "docs": "documents", "download": "downloads", "picture": "pictures",
                "photos": "pictures", "photo": "pictures", "images": "pictures", "video": "videos", "movies": "videos",
                "songs": "music", "home": "home", "user": "home", "my pc": "home"}
SEARCH_SKIP = {"appdata", "node_modules", ".git", "__pycache__", "windows", "program files", "program files (x86)", "$recycle.bin"}


def known_folder(name):
    """Real path of Documents, Downloads, Desktop... as Windows has it (follows OneDrive redirection)."""
    guid = KNOWN_FOLDERS.get(name)
    if not guid or sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + guid + "}"), ctypes.byref(g))
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
        return None
    path = out.value
    ctypes.windll.ole32.CoTaskMemFree(out)
    return Path(path) if path else None


def _search_roots():
    roots = [known_folder(n) for n in ("desktop", "documents", "downloads", "pictures", "music", "videos")]
    return [r for r in [Path.home(), *roots] if r and r.is_dir()]


def find_folder(name, max_depth=3):
    """A folder called ``name`` under the user's own folders (exact name first, then names that contain it)."""
    wanted = name.lower().strip()
    partial = None
    for root in _search_roots():
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop(0)
            try:
                kids = [k for k in folder.iterdir() if k.is_dir() and k.name.lower() not in SEARCH_SKIP and not k.name.startswith(".")]
            except OSError:
                continue
            for k in kids:
                if k.name.lower() == wanted:
                    return k
                if partial is None and wanted in k.name.lower():
                    partial = k
                if depth + 1 < max_depth:
                    stack.append((k, depth + 1))
    return partial


def resolve_folder(spec):
    """A folder from what the user said: 'documents', 'd drive', 'C:\\Users\\me\\Work', 'jarvis', 'project reports'."""
    text = (spec or "").strip().strip("\"'")
    low = re.sub(r"\s+", " ", text.lower())
    low = re.sub(r"^(the|my)\s+|\s+(folder|directory|dir)$", "", low).strip()
    low = re.sub(r"^(the|my)\s+", "", low)
    if re.match(r"^[a-z]:[\\/]|^\\\\|^~|%\w+%", text, re.I):
        p = Path(os.path.expandvars(os.path.expanduser(text)))
        return p if p.is_dir() else None
    m = re.fullmatch(r"([a-z]):?(?: ?drive)?", low) or re.fullmatch(r"(?:drive )([a-z])", low)
    if m and Path(f"{m.group(1).upper()}:\\").exists():
        return Path(f"{m.group(1).upper()}:\\")
    low = FOLDER_WORDS.get(low, low)
    if low in KNOWN_FOLDERS:
        return known_folder(low)
    if low == "home":
        return Path.home()
    if low == "jarvis":
        import json
        from .session import forge_home
        try:
            saved = json.loads((forge_home() / "config.json").read_text(encoding="utf-8")).get("folder")
        except (OSError, ValueError):
            saved = None
        return Path(saved or Path.home() / "Downloads" / "jarvis")
    return find_folder(low)


def open_folder(path):
    """Open a folder in File Explorer: Documents, Downloads, a drive, a full path, or a folder name anywhere under the user's folders."""
    spec = (path or "").strip()
    if not spec:
        return "Error: say which folder to open."
    if re.fullmatch(r"(?i)recycle bin|trash|bin", spec):
        os.startfile("shell:RecycleBinFolder")
        return "Opened the Recycle Bin."
    folder = resolve_folder(spec)
    if folder is None or not folder.is_dir():
        return f"Error: could not find a folder called '{spec}'. Ask the user for its full path."
    os.startfile(str(folder))
    return f"Opened {folder} in File Explorer."


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
