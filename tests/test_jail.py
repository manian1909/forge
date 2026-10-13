"""The sandbox is enforced by Windows, so these run real low-integrity processes in temporary folders."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import jail  # noqa: E402
from assistant.tools import Toolbox  # noqa: E402

windows_only = pytest.mark.skipif(not jail.available(), reason="the jail uses Windows integrity levels")


@pytest.fixture
def box(tmp_path):
    inside, outside = tmp_path / "jarvis", tmp_path / "elsewhere"
    outside.mkdir()
    jail.label_folder(inside)
    (outside / "secret.txt").write_text("readable")
    return Toolbox(inside, sandbox=inside), outside


@windows_only
def test_python_writes_inside_but_not_outside(box):
    tb, outside = box
    ok = tb.call("run_python", {"code": "open('a.txt', 'w').write('hi')\nprint('wrote')"})
    assert "wrote" in ok and (tb.workspace / "a.txt").read_text() == "hi"
    bad = tb.call("run_python", {"code": f"open(r'{outside / 'x.txt'}', 'w').write('no')"})
    assert "PermissionError" in bad and "may only write inside" in bad and not (outside / "x.txt").exists()


@windows_only
def test_shell_writes_inside_but_not_outside_and_can_read_anywhere(box):
    tb, outside = box
    assert "readable" in tb.call("run_shell", {"command": f"Get-Content '{outside / 'secret.txt'}'"})
    tb.call("run_shell", {"command": "Set-Content b.txt hello"})
    assert (tb.workspace / "b.txt").exists()
    out = tb.call("run_shell", {"command": f"Set-Content '{outside / 'y.txt'}' no; 'after'"})
    assert "denied" in out.lower() and not (outside / "y.txt").exists()
    redirect = tb.call("run_shell", {"command": f"'no' > '{outside / 'z.txt'}'"})
    assert not (outside / "z.txt").exists() and "denied" in redirect.lower()


@windows_only
def test_child_processes_and_registry_are_jailed_too(box):
    tb, outside = box
    out = tb.call("run_shell", {"command": f"python -B -c \"open(r'{outside / 'c.txt'}', 'w')\""})
    assert "PermissionError" in out and not (outside / "c.txt").exists()
    reg = tb.call("run_shell", {"command": "try { New-Item 'HKCU:\\Software\\jarvis_jail_probe' -ErrorAction Stop; 'wrote' } catch { 'blocked' }"})
    assert "blocked" in reg


@windows_only
def test_write_file_tool_refuses_paths_outside(box):
    tb, outside = box
    assert tb.call("write_file", {"path": "ok.txt", "content": "x"}).startswith("Wrote")
    assert "may only write inside" in tb.call("write_file", {"path": str(outside / "w.txt"), "content": "x"})
    assert "may only write inside" in tb.call("write_file", {"path": "../escape.txt", "content": "x"})
    assert not (outside / "w.txt").exists() and not (tb.workspace.parent / "escape.txt").exists()


@windows_only
def test_office_libraries_work_inside_the_jail(box):
    tb, _ = box
    code = ("import openpyxl, pandas as pd, matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\n"
            "wb = openpyxl.Workbook(); wb.save('t.xlsx')\npd.DataFrame({'a': [1]}).to_excel('p.xlsx')\n"
            "plt.plot([1, 2]); plt.savefig('c.png')\nprint('all saved')")
    assert "all saved" in tb.call("run_python", {"code": code})
    assert all((tb.workspace / f).exists() for f in ("t.xlsx", "p.xlsx", "c.png"))


@windows_only
def test_timeout_kills_the_process(box):
    tb, _ = box
    tb.shell_timeout = 3
    assert "timed out" in tb.call("run_shell", {"command": "Start-Sleep 30"})


def test_elevation_and_installs_are_refused_before_running():
    for cmd in ["Start-Process cmd -Verb RunAs", "runas /user:Administrator cmd", "sudo dir", "pip install requests",
                "python -m pip install x", "winget install foo", "Install-Module Foo", "Set-ExecutionPolicy Bypass"]:
        assert jail.shell_denied(cmd), cmd
    for cmd in ["Get-ChildItem", "python script.py", "pip list", "Get-Process | Sort CPU", "echo runaway"]:
        assert not jail.shell_denied(cmd), cmd
