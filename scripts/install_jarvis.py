"""Create the `jarvis` command.

    python scripts/install_jarvis.py             install
    python scripts/install_jarvis.py --uninstall remove it

On Windows this writes jarvis.cmd into the Python Scripts folder when that folder is on PATH (the same place pip puts
commands, so no PATH edit is needed). Otherwise it uses ~/.forge/bin and adds that folder to your user PATH.
On Linux/macOS it writes an executable `jarvis` script into ~/.local/bin.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def on_path(folder):
    return any(Path(p).resolve() == folder.resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p and Path(p).exists())


def target_dir():
    if sys.platform != "win32":
        return Path.home() / ".local" / "bin", False
    scripts = Path(sys.executable).parent / "Scripts"
    if scripts.exists() and on_path(scripts):
        return scripts, False
    return Path.home() / ".forge" / "bin", True


def add_to_user_path(folder):
    ps = ("$f='%s'; $p=[Environment]::GetEnvironmentVariable('Path','User'); "
          "if (($p -split ';') -notcontains $f) { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $f), 'User') }" % folder)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    folder, needs_path = target_dir()
    name = "jarvis.cmd" if sys.platform == "win32" else "jarvis"
    dest = folder / name
    if a.uninstall:
        dest.unlink(missing_ok=True)
        print(f"removed {dest}")
        return
    folder.mkdir(parents=True, exist_ok=True)
    py, script = sys.executable, REPO / "jarvis.py"
    if sys.platform == "win32":
        dest.write_text(f'@echo off\r\n"{py}" "{script}" %*\r\n', encoding="ascii")
    else:
        dest.write_text(f'#!/bin/sh\nexec "{py}" "{script}" "$@"\n', encoding="utf-8")
        dest.chmod(0o755)
    print(f"installed {dest}")
    if needs_path:
        add_to_user_path(folder)
        print(f"added {folder} to your user PATH")
    print("Open a NEW terminal and type: jarvis")


if __name__ == "__main__":
    main()
