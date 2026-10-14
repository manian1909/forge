"""Desktop tools: nothing is launched; browsers, the network and Outlook are faked."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import desktop, media, skills  # noqa: E402
from assistant.tools import TOOL_NAMES, Toolbox, needs_confirmation  # noqa: E402


def fake_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda argv, **kw: opened.append(argv))
    monkeypatch.setattr(desktop, "reuse_tab", lambda url, service: False)       # no real Chrome tabs are touched
    monkeypatch.setattr(media, "audio_active", lambda seconds=0.6: False)
    return opened


def test_play_music_opens_the_watch_page(monkeypatch, tmp_path):
    opened = fake_browser(monkeypatch)
    monkeypatch.setattr(desktop, "find_video", lambda q: "q8gilwzNQEA")
    out = Toolbox(tmp_path).call("play_music", {"query": "Mere Rashke Qamar"})
    assert opened == [["chrome.exe", "https://music.youtube.com/watch?v=q8gilwzNQEA"]]
    assert "Playing" in out


def test_play_music_falls_back_to_search_page(monkeypatch):
    opened = fake_browser(monkeypatch)
    monkeypatch.setattr(desktop, "find_video", lambda q: None)
    out = desktop.play_music("mere rashke qamar", "youtube")
    assert opened[0][1] == "https://www.youtube.com/results?search_query=mere+rashke+qamar"
    assert "Could not find" in out


def test_video_id_is_taken_from_results_not_ads():
    page = '..."adSlotRenderer":{"videoId":"AAAAAAAAAAA"},"videoRenderer":{"videoId":"q8gilwzNQEA","title"...'
    assert desktop.VIDEO_ID.findall(page)[0] == "q8gilwzNQEA"


def test_open_url_only_takes_web_addresses(monkeypatch):
    opened = fake_browser(monkeypatch)
    assert desktop.open_url("youtube.com").startswith("Opened https://youtube.com")
    assert desktop.open_url("file:///C:/Windows/System32/cmd.exe").startswith("Error")
    assert desktop.open_url("javascript:alert(1)").startswith("Error")
    assert len(opened) == 1


def test_open_app_refuses_paths_and_arguments():
    for bad in (r"C:\Windows\System32\cmd.exe", "powershell; calc", "notepad C:/x", ""):
        assert desktop.open_app(bad).startswith("Error")


def test_open_app_starts_by_name(monkeypatch):
    started = []
    monkeypatch.setattr(desktop.os, "startfile", started.append, raising=False)
    monkeypatch.setattr(desktop, "_registered_path", lambda exe: r"C:\Office\OUTLOOK.EXE")
    assert desktop.open_app("Outlook") == "Started Outlook."
    assert started == [r"C:\Office\OUTLOOK.EXE"]


def test_send_email_checks_the_address_and_asks_first(monkeypatch):
    assert desktop.send_email("not an address", "s", "b").startswith("Error")
    assert needs_confirmation("send_email", {}) and not needs_confirmation("play_music", {})


def test_tools_are_registered_and_skill_matches():
    assert {"play_music", "open_url", "open_app", "send_email"} <= set(TOOL_NAMES)
    found = skills.match("play mere rashke qamar on youtube music", skills.load_skills())
    assert "desktop" in [s.name for s in found]


def test_a_second_song_reuses_the_open_tab(monkeypatch):
    opened = fake_browser(monkeypatch)
    seen = []
    monkeypatch.setattr(desktop, "reuse_tab", lambda url, service: seen.append((url, service)) or True)
    monkeypatch.setattr(desktop, "find_video", lambda q: "abcdefghijk")
    out = desktop.play_music("believer")
    assert seen == [("https://music.youtube.com/watch?v=abcdefghijk", "youtube_music")]
    assert opened == [] and "already open" in out


def test_a_new_tab_pauses_whatever_else_is_playing(monkeypatch):
    opened = fake_browser(monkeypatch)
    pressed = []
    monkeypatch.setattr(media, "audio_active", lambda seconds=0.6: True)
    monkeypatch.setattr(media, "press", pressed.append)
    monkeypatch.setattr(desktop, "find_video", lambda q: "abcdefghijk")
    assert "new tab" in desktop.play_music("believer")
    assert pressed == ["play_pause"] and len(opened) == 1


def test_the_tab_switcher_script_ships_and_targets_youtube_tabs():
    text = desktop.Path(desktop.__file__).with_name("reuse_tab.ps1").read_text(encoding="utf-8")
    assert "Address and search bar" in text and "YouTube Music" in text and "Audio playing" in text
