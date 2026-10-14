"""Opening apps and folders: the jailed shell must refuse GUI launches (they fail there), the tools must do it instead."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import commands, desktop  # noqa: E402
from assistant.tools import Toolbox, launches_window  # noqa: E402


@pytest.mark.parametrize("cmd", [
    "Start-Process -FilePath 'explorer.exe' -ArgumentList '/select, C:\\Users\\Public\\Documents'",
    "explorer.exe C:\\Users", "start chrome", "Invoke-Item .", "ii C:\\Users", "Start-Process notepad; Get-Date",
    "cmd /c start https://youtube.com", "& explorer .",
])
def test_gui_launches_are_caught(cmd):
    assert launches_window(cmd)


@pytest.mark.parametrize("cmd", ["Get-ChildItem", "Start-Sleep 2", "python script.py", "Get-Process explorer", "echo start"])
def test_normal_commands_are_not(cmd):
    assert not launches_window(cmd)


def test_jailed_shell_says_which_tool_to_use(tmp_path):
    out = Toolbox(tmp_path, sandbox=tmp_path).call("run_shell", {"command": "Start-Process explorer.exe"})
    assert out.startswith("Not run") and "open_folder" in out and "open_app" in out


@pytest.mark.parametrize("text,expected", [
    ("open files explorer for me in that open the documents folder", {"action": "open_folder", "path": "documents"}),
    ("open the documents folder", {"action": "open_folder", "path": "documents"}),
    ("open downloads", {"action": "open_folder", "path": "downloads"}),
    ("open my pictures folder", {"action": "open_folder", "path": "pictures"}),
    ("open d drive", {"action": "open_folder", "path": "d drive"}),
    ("open the project reports folder", {"action": "open_folder", "path": "project reports"}),
    ("open file explorer", {"action": "open_app", "name": "file explorer"}),
    ("open the jarvis folder", {"action": "open_folder", "path": "jarvis"}),
    ("open outlook", {"action": "open_app", "name": "outlook"}),
    ("launch whatsapp", {"action": "open_app", "name": "whatsapp"}),
])
def test_folder_and_app_commands(text, expected):
    assert commands.parse(text) == expected


@pytest.mark.parametrize("text", ["open the file notes.txt", "open my resume document", "open a website about cats"])
def test_files_and_pages_still_go_to_the_model(text):
    assert commands.parse(text) is None


APPS = {"Microsoft Outlook": "Outlook.1", "Excel": "Excel.1", "Calculator": "Calc.1", "Google Chrome": "Chrome.1",
        "WhatsApp": "Whats.1", "7-Zip File Manager": "7z.1", "7-Zip Help": "7zh.1", "Uninstall Foo": "u.1"}


@pytest.mark.parametrize("said,found", [("outlook", "Microsoft Outlook"), ("chrome", "Google Chrome"), ("calculator", "Calculator"),
                                        ("the whatsapp app", "WhatsApp"), ("exel", "Excel"), ("7-zip", "7-Zip File Manager")])
def test_app_matching(said, found):
    assert desktop.match_app(said, APPS) == found


def test_unknown_app_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setattr(desktop, "start_apps", lambda: APPS)
    monkeypatch.setattr(desktop, "_start_menu_match", lambda n: None)
    assert desktop.open_app("zzqqxx").startswith("Error")


def test_store_apps_are_started_by_app_id(monkeypatch):
    started = []
    monkeypatch.setattr(desktop, "start_apps", lambda: APPS)
    monkeypatch.setattr(desktop.os, "startfile", started.append, raising=False)
    assert desktop.open_app("whatsapp") == "Started WhatsApp."
    assert started == ["shell:AppsFolder\\Whats.1"]


def test_open_folder_resolves_names_and_paths(monkeypatch, tmp_path):
    (tmp_path / "Work" / "Reports 2026").mkdir(parents=True)
    opened = []
    monkeypatch.setattr(desktop.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(desktop, "_search_roots", lambda: [tmp_path])
    assert desktop.open_folder(str(tmp_path / "Work")).startswith("Opened")
    assert desktop.open_folder("the reports 2026 folder").startswith("Opened")
    assert opened == [str(tmp_path / "Work"), str(tmp_path / "Work" / "Reports 2026")]
    assert desktop.open_folder("no such folder anywhere").startswith("Error")
    assert desktop.open_folder("").startswith("Error")


def test_documents_is_the_real_windows_folder():
    docs = desktop.known_folder("documents")
    assert docs is not None and docs.is_dir()
    assert desktop.resolve_folder("my documents folder") == docs
