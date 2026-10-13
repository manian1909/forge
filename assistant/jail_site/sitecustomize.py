"""Loaded by every Python that jarvis starts inside the jail: refuses to ask Windows for administrator rights."""

import re
import sys

_RE = re.compile(r"-Verb\s+['\"]?RunAs|\brunas(\.exe)?\b|\bsudo\b|\bpsexec\b", re.I)


def _hook(event, args):
    if event in ("subprocess.Popen", "os.system"):
        text = " ".join(str(a) for a in args if not isinstance(a, (int, dict)) and a is not None)
        if _RE.search(text):
            raise PermissionError("jarvis may not ask for administrator rights")
    elif event == "os.startfile" and len(args) > 1 and str(args[1]).lower() == "runas":
        raise PermissionError("jarvis may not ask for administrator rights")


sys.addaudithook(_hook)
