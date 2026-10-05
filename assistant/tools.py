"""Tools the model can call.

The JSON schemas in ``TOOL_SCHEMAS`` are shared by the runtime and by the
dataset builders, so the model is trained on exactly the tools it gets at
inference time.
"""

import html
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT = 4000
IS_WINDOWS = platform.system() == "Windows"
SHELL_NAME = "PowerShell" if IS_WINDOWS else "bash"


def _truncate(text, limit=MAX_OUTPUT):
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} chars truncated] ...\n{text[-half:]}"


def _fn(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS = [
    _fn(
        "run_python",
        "Run a Python 3 script and return its stdout/stderr. Use print() to show results. "
        "Good for calculations, data processing, and testing code.",
        {"code": {"type": "string", "description": "Complete Python source to execute"}},
        ["code"],
    ),
    _fn(
        "run_shell",
        f"Run a {SHELL_NAME} command in the workspace directory and return its output.",
        {"command": {"type": "string", "description": f"The {SHELL_NAME} command line"}},
        ["command"],
    ),
    _fn(
        "read_file",
        "Read a text file. Relative paths are resolved against the workspace.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _fn(
        "write_file",
        "Create or overwrite a text file with the given content.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _fn(
        "list_dir",
        "List the files and folders in a directory.",
        {"path": {"type": "string", "description": "Directory, default is the workspace"}},
        [],
    ),
    _fn(
        "web_search",
        "Search the web. Returns titles, URLs and snippets of the top results.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _fn(
        "fetch_url",
        "Download a web page and return its readable text.",
        {"url": {"type": "string"}},
        ["url"],
    ),
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]

# tools that change the machine; the CLI asks before running these
NEEDS_CONFIRMATION = {"run_python", "run_shell", "write_file"}


class Toolbox:
    def __init__(self, workspace="workspace", python_timeout=60, shell_timeout=120):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.python_timeout = python_timeout
        self.shell_timeout = shell_timeout

    def _resolve(self, path):
        p = Path(os.path.expanduser(path or "."))
        return p if p.is_absolute() else self.workspace / p

    def call(self, name, args):
        """Run a tool and always return a string (errors are reported, not raised)."""
        fn = getattr(self, name, None)
        if name not in TOOL_NAMES or fn is None:
            return f"Error: unknown tool {name!r}. Available: {', '.join(TOOL_NAMES)}"
        try:
            return _truncate(fn(**(args or {})))
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - the model should see any failure
            return f"Error: {type(e).__name__}: {e}"

    # ---------------------------------------------------------------- tools
    def run_python(self, code):
        with tempfile.NamedTemporaryFile("w", suffix=".py", dir=self.workspace,
                                         delete=False, encoding="utf-8") as f:
            f.write(code)
            script = f.name
        try:
            proc = subprocess.run(
                [sys.executable, script], cwd=self.workspace, capture_output=True,
                text=True, timeout=self.python_timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {self.python_timeout}s"
        finally:
            os.unlink(script)
        return _format_process(proc)

    def run_shell(self, command):
        if IS_WINDOWS:
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            argv = ["bash", "-lc", command]
        try:
            proc = subprocess.run(
                argv, cwd=self.workspace, capture_output=True, text=True,
                timeout=self.shell_timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {self.shell_timeout}s"
        return _format_process(proc)

    def read_file(self, path):
        return self._resolve(path).read_text(encoding="utf-8", errors="replace")

    def write_file(self, path, content):
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {target}"

    def list_dir(self, path="."):
        target = self._resolve(path)
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} bytes)" for e in entries]
        return "\n".join(lines) if lines else "(empty directory)"

    def web_search(self, query):
        from ddgs import DDGS

        results = DDGS().text(query, max_results=5)
        if not results:
            return "No results."
        return "\n\n".join(f"{r['title']}\n{r['href']}\n{r['body']}" for r in results)

    def fetch_url(self, url):
        import httpx

        resp = httpx.get(url, follow_redirects=True, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (local-assistant)"})
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", "html"):
            return resp.text
        return html_to_text(resp.text)


def _format_process(proc):
    parts = []
    if proc.stdout.strip():
        parts.append(proc.stdout.rstrip())
    if proc.stderr.strip():
        parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
    if proc.returncode != 0:
        parts.append(f"[exit code {proc.returncode}]")
    return "\n".join(parts) if parts else "(no output)"


def html_to_text(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()
